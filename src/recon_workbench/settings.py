"""应用配置：只从环境变量读取，带校验与默认值。

配置分两层：
- 引擎策略（容差、正则等）走可选的 TOML（``RECON_POLICY_FILE``）；
- 运行参数（路径、日志、复核开关、API 端口）走环境变量。

所有值在读入时校验，坏配置在启动阶段就报错，而不是运行到一半才炸。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .config import Policy, load_policy
from .errors import ConfigError
from .review.client import LLMSettings
from .workflow.service import ReviewSettings

DEFAULT_MAX_UPLOAD_BYTES = 20 * 1024 * 1024


def _env_str(env: Mapping[str, str], name: str, default: str) -> str:
    value = (env.get(name) or "").strip()
    return value or default


def _env_bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = (env.get(name) or "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} 必须是布尔值（1/0/true/false），收到 {raw!r}")


def _env_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} 必须是整数，收到 {raw!r}") from exc


def _env_float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} 必须是数字，收到 {raw!r}") from exc


@dataclass(frozen=True, slots=True)
class AppSettings:
    db_path: Path = Path("data/recon.db")
    storage_dir: Path = Path("data/jobs")
    upload_dir: Path = Path("data/uploads")
    max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES
    review_enabled: bool = True
    review_provider: str = "stub"
    review_threshold: float = 0.85
    review_limit: int = 20
    policy_file: Path | None = None
    log_level: str = "INFO"
    log_json: bool = False
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    _llm_configured: bool = field(default=False, repr=False)

    def __post_init__(self) -> None:
        if self.review_provider not in {"stub", "openai"}:
            raise ConfigError("RECON_REVIEW_PROVIDER 只能是 stub 或 openai")
        if not 0 <= self.review_threshold <= 1:
            raise ConfigError("RECON_REVIEW_THRESHOLD 必须在 0 到 1 之间")
        if self.max_upload_bytes <= 0:
            raise ConfigError("RECON_MAX_UPLOAD_BYTES 必须大于 0")
        if not 1 <= self.review_limit <= 500:
            raise ConfigError("RECON_REVIEW_LIMIT 必须在 1..500 之间")
        if self.log_level.upper() not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ConfigError(f"RECON_LOG_LEVEL 非法：{self.log_level}")
        if not 1 <= self.api_port <= 65535:
            raise ConfigError("RECON_PORT 必须在 1..65535 之间")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> AppSettings:
        source = os.environ if env is None else env
        policy_file = (source.get("RECON_POLICY_FILE") or "").strip()
        llm_configured = bool((source.get("LLM_API_KEY") or "").strip())
        return cls(
            db_path=Path(_env_str(source, "RECON_DB_PATH", "data/recon.db")),
            storage_dir=Path(_env_str(source, "RECON_STORAGE_DIR", "data/jobs")),
            upload_dir=Path(_env_str(source, "RECON_UPLOAD_DIR", "data/uploads")),
            max_upload_bytes=_env_int(source, "RECON_MAX_UPLOAD_BYTES", DEFAULT_MAX_UPLOAD_BYTES),
            review_enabled=_env_bool(source, "RECON_REVIEW_ENABLED", True),
            review_provider=_env_str(source, "RECON_REVIEW_PROVIDER", "stub").lower(),
            review_threshold=_env_float(source, "RECON_REVIEW_THRESHOLD", 0.85),
            review_limit=_env_int(source, "RECON_REVIEW_LIMIT", 20),
            policy_file=Path(policy_file) if policy_file else None,
            log_level=_env_str(source, "RECON_LOG_LEVEL", "INFO").upper(),
            log_json=_env_bool(source, "RECON_LOG_JSON", False),
            api_host=_env_str(source, "RECON_HOST", "0.0.0.0"),
            api_port=_env_int(source, "RECON_PORT", 8000),
            _llm_configured=llm_configured,
        )

    def policy(self) -> Policy:
        return load_policy(self.policy_file) if self.policy_file else Policy()

    def review_settings(self) -> ReviewSettings:
        """复核配置也是任务身份的一部分，创建任务时必须确定。"""

        if not self.review_enabled:
            return ReviewSettings(enabled=False)
        if self.review_provider == "stub":
            return ReviewSettings(
                enabled=True,
                provider="stub",
                model="stub-model",
                threshold=self.review_threshold,
                max_attempts=1,
                limit=self.review_limit,
            )
        llm = LLMSettings.from_env(require_credentials=True)
        return ReviewSettings(
            enabled=True,
            provider="openai-compatible",
            model=llm.model,
            threshold=llm.confidence_threshold,
            max_attempts=llm.max_attempts,
            limit=self.review_limit,
        )

    def ensure_dirs(self) -> None:
        for path in (self.db_path.parent, self.storage_dir, self.upload_dir):
            path.mkdir(parents=True, exist_ok=True)

    def describe(self) -> dict[str, object]:
        """给 ``recon config show`` 用的可公开快照（不含密钥本身）。"""

        return {
            "db_path": str(self.db_path),
            "storage_dir": str(self.storage_dir),
            "upload_dir": str(self.upload_dir),
            "max_upload_bytes": self.max_upload_bytes,
            "review_enabled": self.review_enabled,
            "review_provider": self.review_provider,
            "review_threshold": self.review_threshold,
            "review_limit": self.review_limit,
            "policy_file": str(self.policy_file) if self.policy_file else None,
            "log_level": self.log_level,
            "log_json": self.log_json,
            "api_host": self.api_host,
            "api_port": self.api_port,
            "llm_api_key_configured": self._llm_configured,
        }
