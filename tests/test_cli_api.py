"""recon api 与 ``python -m recon_workbench.api`` 的启动逻辑测试。"""

from __future__ import annotations

from pathlib import Path

import pytest
import uvicorn

from recon_workbench import cli
from recon_workbench.api.__main__ import main as api_main


def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("RECON_DB_PATH", str(tmp_path / "recon.db"))
    monkeypatch.setenv("RECON_STORAGE_DIR", str(tmp_path / "jobs"))
    monkeypatch.setenv("RECON_UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("RECON_LOG_LEVEL", "WARNING")


def test_cli_api_starts_uvicorn(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _env(monkeypatch, tmp_path)
    captured: dict[str, object] = {}

    def fake_run(application, **kwargs):
        captured["app"] = application
        captured.update(kwargs)

    monkeypatch.setattr(uvicorn, "run", fake_run)
    assert cli.main(["api", "--host", "127.0.0.1", "--port", "8099"]) == 0
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 8099
    assert captured["app"].title == "对账工作台 API"


def test_cli_api_rejects_bad_port(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys) -> None:
    _env(monkeypatch, tmp_path)
    assert cli.main(["api", "--port", "70000"]) == 3
    assert "--port" in capsys.readouterr().err


def test_module_entrypoint(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _env(monkeypatch, tmp_path)
    captured: dict[str, object] = {}
    monkeypatch.setattr(uvicorn, "run", lambda application, **kwargs: captured.update(kwargs))
    api_main()
    assert captured["port"] == 8000
    assert captured["access_log"] is False
