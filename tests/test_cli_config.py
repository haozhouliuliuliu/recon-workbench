"""recon config 命令测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from recon_workbench.cli import main


def test_config_show_json(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("RECON_DB_PATH", "data/example.db")
    monkeypatch.setenv("RECON_REVIEW_PROVIDER", "stub")
    assert main(["config", "show", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert Path(payload["db_path"]) == Path("data/example.db")
    assert payload["review_provider"] == "stub"
    assert payload["llm_api_key_configured"] is False


def test_config_show_text(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("RECON_LOG_LEVEL", "DEBUG")
    assert main(["config", "show"]) == 0
    output = capsys.readouterr().out
    assert "db_path:" in output
    assert "log_level: DEBUG" in output


def test_config_show_rejects_bad_config(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("RECON_REVIEW_PROVIDER", "nope")
    assert main(["config", "show"]) == 3
    assert "RECON_REVIEW_PROVIDER" in capsys.readouterr().err


def test_config_never_prints_secret(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("LLM_API_KEY", "sk-do-not-print")
    assert main(["config", "show", "--json"]) == 0
    assert "sk-do-not-print" not in capsys.readouterr().out
