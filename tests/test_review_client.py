"""LLM 客户端测试：配置校验、请求构造、HTTP 错误映射。全部使用 MockTransport，不发真实请求。"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from recon_workbench.errors import ConfigError
from recon_workbench.review.client import (
    LLMSettings,
    OpenAICompatibleClient,
    StubClient,
)
from recon_workbench.review.errors import LLMError, LLMTransportError

ENV = {
    "LLM_API_BASE": "https://api.example.invalid/v1",
    "LLM_API_KEY": "sk-unit-test",
    "LLM_MODEL": "demo-model",
    "LLM_MAX_RETRIES": "1",
    "LLM_TIMEOUT_SECONDS": "12",
    "LLM_CONFIDENCE_THRESHOLD": "0.7",
}


def _settings(**overrides: Any) -> LLMSettings:
    base = {
        "api_base": "https://api.example.invalid/v1",
        "api_key": "sk-unit-test",
        "model": "demo-model",
    }
    base.update(overrides)
    return LLMSettings(**base)


def _client(handler, **overrides: Any) -> OpenAICompatibleClient:
    return OpenAICompatibleClient(_settings(**overrides), transport=httpx.MockTransport(handler))


def _ok(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def test_settings_from_env() -> None:
    settings = LLMSettings.from_env(ENV)
    assert settings.model == "demo-model"
    assert settings.max_attempts == 2
    assert settings.timeout_seconds == 12
    assert settings.confidence_threshold == 0.7


def test_missing_credentials_rejected() -> None:
    with pytest.raises(ConfigError) as excinfo:
        LLMSettings.from_env({"LLM_API_BASE": "https://x.invalid"})
    assert "LLM_API_KEY" in str(excinfo.value)


def test_invalid_threshold_rejected() -> None:
    with pytest.raises(ConfigError):
        LLMSettings.from_env({**ENV, "LLM_CONFIDENCE_THRESHOLD": "1.5"})


def test_invalid_base_url_rejected() -> None:
    with pytest.raises(ConfigError):
        _settings(api_base="api.example.invalid")


def test_api_key_is_not_in_repr() -> None:
    assert "sk-unit-test" not in repr(_settings())


def test_request_shape() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return _ok("{}")

    with _client(handler) as client:
        assert client.complete([{"role": "user", "content": "hi"}]) == "{}"

    assert captured["url"] == "https://api.example.invalid/v1/chat/completions"
    assert captured["auth"] == "Bearer sk-unit-test"
    assert captured["body"]["model"] == "demo-model"
    assert captured["body"]["response_format"] == {"type": "json_object"}
    assert captured["body"]["messages"] == [{"role": "user", "content": "hi"}]


def test_json_mode_can_be_disabled() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return _ok("{}")

    with _client(handler, json_mode=False) as client:
        client.complete([{"role": "user", "content": "hi"}])
    assert "response_format" not in captured["body"]


@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [
        (500, "HTTP_5XX", True),
        (503, "HTTP_5XX", True),
        (429, "HTTP_429", True),
        (401, "HTTP_401", False),
        (403, "HTTP_401", False),
        (404, "HTTP_404", False),
        (422, "HTTP_4XX", False),
    ],
)
def test_http_status_mapping(status: int, code: str, retryable: bool) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="server says no")

    with _client(handler) as client, pytest.raises(LLMTransportError) as excinfo:
        client.complete([{"role": "user", "content": "hi"}])
    assert excinfo.value.code == code
    assert excinfo.value.retryable is retryable


def test_timeout_is_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    with _client(handler) as client, pytest.raises(LLMTransportError) as excinfo:
        client.complete([{"role": "user", "content": "hi"}])
    assert excinfo.value.code == "TIMEOUT"
    assert excinfo.value.retryable is True


def test_connection_error_is_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with _client(handler) as client, pytest.raises(LLMTransportError) as excinfo:
        client.complete([{"role": "user", "content": "hi"}])
    assert excinfo.value.code == "CONNECTION_ERROR"


def test_non_json_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>oops</html>")

    with _client(handler) as client, pytest.raises(LLMTransportError) as excinfo:
        client.complete([{"role": "user", "content": "hi"}])
    assert excinfo.value.code == "INVALID_RESPONSE_BODY"


def test_missing_choices() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "x"})

    with _client(handler) as client, pytest.raises(LLMTransportError) as excinfo:
        client.complete([{"role": "user", "content": "hi"}])
    assert excinfo.value.code == "INVALID_RESPONSE_BODY"


def test_empty_content() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _ok("   ")

    with _client(handler) as client, pytest.raises(LLMTransportError) as excinfo:
        client.complete([{"role": "user", "content": "hi"}])
    assert excinfo.value.code == "EMPTY_RESPONSE"


def test_stub_client_scripts_responses() -> None:
    stub = StubClient(["first", "second"])
    assert stub.complete([{"role": "user", "content": "a"}]) == "first"
    assert stub.complete([{"role": "user", "content": "b"}]) == "second"
    assert [call[0]["content"] for call in stub.calls] == ["a", "b"]


def test_stub_client_raises_scripted_error() -> None:
    stub = StubClient([LLMTransportError("HTTP_5XX", "boom", retryable=True)])
    with pytest.raises(LLMError) as excinfo:
        stub.complete([{"role": "user", "content": "a"}])
    assert excinfo.value.code == "HTTP_5XX"
