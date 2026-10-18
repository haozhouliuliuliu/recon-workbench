"""LLM 客户端：OpenAI 兼容协议 + 可脚本化的 Stub。

约束：
- 密钥只从环境变量读，永不写入日志、异常或报告；
- 所有网络错误都收敛成带错误码的 ``LLMTransportError``，由上层决定是否重试；
- Stub 用于测试与无密钥演示，行为完全确定。
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from ..errors import ConfigError
from .errors import LLMError, LLMTransportError


@dataclass(frozen=True)
class LLMSettings:
    api_base: str
    model: str
    api_key: str = field(default="", repr=False)
    timeout_seconds: float = 30.0
    max_attempts: int = 3
    confidence_threshold: float = 0.85
    temperature: float = 0.0
    json_mode: bool = True

    def __post_init__(self) -> None:
        if not self.api_base.startswith(("http://", "https://")):
            raise ConfigError("LLM_API_BASE 必须以 http:// 或 https:// 开头")
        if not self.model:
            raise ConfigError("LLM_MODEL 不能为空")
        if self.timeout_seconds <= 0:
            raise ConfigError("LLM_TIMEOUT_SECONDS 必须大于 0")
        if self.max_attempts < 1:
            raise ConfigError("LLM_MAX_RETRIES 不能小于 0")
        if not 0 <= self.confidence_threshold <= 1:
            raise ConfigError("LLM_CONFIDENCE_THRESHOLD 必须在 0 到 1 之间")

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        require_credentials: bool = True,
    ) -> LLMSettings:
        source = os.environ if env is None else env
        base = (source.get("LLM_API_BASE") or "").strip()
        key = (source.get("LLM_API_KEY") or "").strip()
        model = (source.get("LLM_MODEL") or "").strip()

        if require_credentials:
            missing = [
                name
                for name, value in (("LLM_API_BASE", base), ("LLM_API_KEY", key), ("LLM_MODEL", model))
                if not value
            ]
            if missing:
                raise ConfigError(f"缺少环境变量：{', '.join(missing)}（可参考 .env.example）")

        retries = _env_int(source, "LLM_MAX_RETRIES", 2)
        return cls(
            api_base=base or "http://localhost",
            api_key=key,
            model=model or "unset-model",
            timeout_seconds=_env_float(source, "LLM_TIMEOUT_SECONDS", 30.0),
            max_attempts=retries + 1,
            confidence_threshold=_env_float(source, "LLM_CONFIDENCE_THRESHOLD", 0.85),
            temperature=_env_float(source, "LLM_TEMPERATURE", 0.0),
            json_mode=(source.get("LLM_JSON_MODE", "1").strip().lower() not in {"0", "false", "no"}),
        )


def _env_float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} 必须是数字，收到 {raw!r}") from exc


def _env_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} 必须是整数，收到 {raw!r}") from exc


class ChatClient(Protocol):
    name: str
    model: str

    def complete(self, messages: Sequence[Mapping[str, str]]) -> str: ...


class OpenAICompatibleClient:
    """兼容 ``POST {base}/chat/completions`` 的服务商（OpenAI、DeepSeek 等）。"""

    name = "openai-compatible"

    def __init__(
        self,
        settings: LLMSettings,
        *,
        transport: httpx.BaseTransport | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.model = settings.model
        self._settings = settings
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(settings.timeout_seconds),
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> OpenAICompatibleClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def complete(self, messages: Sequence[Mapping[str, str]]) -> str:
        settings = self._settings
        url = settings.api_base.rstrip("/") + "/chat/completions"
        payload: dict[str, object] = {
            "model": settings.model,
            "messages": [dict(message) for message in messages],
            "temperature": settings.temperature,
            "max_tokens": 800,
        }
        if settings.json_mode:
            payload["response_format"] = {"type": "json_object"}

        try:
            response = self._client.post(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {settings.api_key}"},
            )
        except httpx.TimeoutException as exc:
            raise LLMTransportError(
                "TIMEOUT",
                f"请求超时（{settings.timeout_seconds:g}s）",
                retryable=True,
            ) from exc
        except httpx.HTTPError as exc:
            raise LLMTransportError("CONNECTION_ERROR", f"网络错误：{exc.__class__.__name__}", retryable=True) from exc

        self._raise_for_status(response)
        return self._extract_content(response)

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        status = response.status_code
        if status < 400:
            return
        detail = (response.text or "").strip().replace("\n", " ")[:200]
        if status in (401, 403):
            raise LLMTransportError("HTTP_401", f"鉴权失败（{status}）：{detail}", retryable=False)
        if status == 404:
            raise LLMTransportError("HTTP_404", f"接口不存在（404）：{detail}", retryable=False)
        if status == 429:
            raise LLMTransportError("HTTP_429", f"触发限流（429）：{detail}", retryable=True)
        if status >= 500:
            raise LLMTransportError("HTTP_5XX", f"服务端错误（{status}）：{detail}", retryable=True)
        raise LLMTransportError("HTTP_4XX", f"请求被拒绝（{status}）：{detail}", retryable=False)

    @staticmethod
    def _extract_content(response: httpx.Response) -> str:
        try:
            payload = response.json()
        except ValueError as exc:
            raise LLMTransportError("INVALID_RESPONSE_BODY", "响应不是合法 JSON", retryable=True) from exc

        try:
            choices = payload["choices"]
            content = choices[0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMTransportError(
                "INVALID_RESPONSE_BODY", "响应缺少 choices[0].message.content", retryable=True
            ) from exc

        if not isinstance(content, str) or not content.strip():
            raise LLMTransportError("EMPTY_RESPONSE", "模型返回空内容", retryable=True)
        return content


class StubClient:
    """按脚本返回固定内容，用于测试与 ``--stub`` 演示。"""

    def __init__(self, responses: Sequence[str | Exception], *, name: str = "stub", model: str = "stub-model") -> None:
        self.name = name
        self.model = model
        self._responses = list(responses)
        self.calls: list[list[dict[str, str]]] = []

    def complete(self, messages: Sequence[Mapping[str, str]]) -> str:
        self.calls.append([dict(message) for message in messages])
        if not self._responses:
            raise LLMError("STUB_EXHAUSTED", "Stub 没有更多预置响应", retryable=False) from None
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item
