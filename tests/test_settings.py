"""配置管理测试：默认值、非法值、复核设置、密钥掩码。"""

from __future__ import annotations

from pathlib import Path

import pytest

from recon_workbench.errors import ConfigError
from recon_workbench.settings import AppSettings


def test_defaults() -> None:
    settings = AppSettings.from_env({})
    assert settings.db_path == Path("data/recon.db")
    assert settings.review_provider == "stub"
    assert settings.review_threshold == 0.85
    assert settings.api_port == 8000
    assert settings.log_level == "INFO"


def test_env_overrides() -> None:
    settings = AppSettings.from_env(
        {
            "RECON_DB_PATH": "tmp/x.db",
            "RECON_REVIEW_PROVIDER": "openai",
            "RECON_REVIEW_THRESHOLD": "0.7",
            "RECON_LOG_JSON": "1",
            "RECON_PORT": "9000",
            "LLM_API_KEY": "sk-test",
        }
    )
    assert settings.db_path == Path("tmp/x.db")
    assert settings.review_provider == "openai"
    assert settings.review_threshold == 0.7
    assert settings.log_json is True
    assert settings.api_port == 9000
    assert settings.describe()["llm_api_key_configured"] is True


def test_invalid_provider() -> None:
    with pytest.raises(ConfigError):
        AppSettings.from_env({"RECON_REVIEW_PROVIDER": "magic"})


def test_invalid_threshold() -> None:
    with pytest.raises(ConfigError):
        AppSettings.from_env({"RECON_REVIEW_THRESHOLD": "1.5"})


def test_invalid_bool() -> None:
    with pytest.raises(ConfigError):
        AppSettings.from_env({"RECON_LOG_JSON": "maybe"})


def test_invalid_port() -> None:
    with pytest.raises(ConfigError):
        AppSettings.from_env({"RECON_PORT": "70000"})


def test_review_settings_stub() -> None:
    review = AppSettings.from_env({}).review_settings()
    assert review.enabled is True
    assert review.provider == "stub"
    assert review.max_attempts == 1


def test_review_settings_disabled() -> None:
    review = AppSettings.from_env({"RECON_REVIEW_ENABLED": "0"}).review_settings()
    assert review.enabled is False


def test_review_settings_openai_requires_key(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("LLM_API_BASE", "LLM_API_KEY", "LLM_MODEL"):
        monkeypatch.delenv(name, raising=False)
    settings = AppSettings(review_provider="openai")
    with pytest.raises(ConfigError):
        settings.review_settings()


def test_policy_file(tmp_path: Path) -> None:
    config = tmp_path / "policy.toml"
    config.write_text("[policy]\namount_tolerance = 0.01\n", encoding="utf-8")
    settings = AppSettings(policy_file=config)
    assert str(settings.policy().amount_tolerance) == "0.01"


def test_ensure_dirs(tmp_path: Path) -> None:
    settings = AppSettings(
        db_path=tmp_path / "db" / "recon.db",
        storage_dir=tmp_path / "jobs",
        upload_dir=tmp_path / "uploads",
    )
    settings.ensure_dirs()
    assert (tmp_path / "db").is_dir()
    assert (tmp_path / "jobs").is_dir()
    assert (tmp_path / "uploads").is_dir()


def test_describe_has_no_secret_material() -> None:
    settings = AppSettings.from_env({"LLM_API_KEY": "sk-super-secret"})
    payload = settings.describe()
    assert "sk-super-secret" not in str(payload)
    assert payload["llm_api_key_configured"] is True
