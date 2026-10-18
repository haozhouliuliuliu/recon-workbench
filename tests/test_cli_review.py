"""recon review 命令测试：stub 演示、dry-run、错误输入、失败退出码。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from recon_workbench.cli import main
from recon_workbench.export import write_report_json
from recon_workbench.pipeline import run_reconciliation
from recon_workbench.review.client import LLMSettings, StubClient
from recon_workbench.review.errors import LLMTransportError

SAMPLE_DIR = Path(__file__).resolve().parents[1] / "sample_data"


@pytest.fixture
def report_path(tmp_path: Path) -> Path:
    result = run_reconciliation(SAMPLE_DIR / "ledger_2026_08.csv", SAMPLE_DIR / "statement_2026_08.xlsx")
    return write_report_json(result.canonical_json, tmp_path / "recon_report.json")


def _args(report: Path, tmp_path: Path, *extra: str) -> list[str]:
    return ["review", "--report", str(report), "--out-dir", str(tmp_path), "--now", "2026-10-18T12:00:00", *extra]


def test_stub_review_writes_report(report_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(_args(report_path, tmp_path, "--stub")) == 0
    payload = json.loads((tmp_path / "review_report.json").read_text(encoding="utf-8"))
    assert payload["summary"] == {"total": 4, "auto_suggested": 1, "manual_review": 3, "failed": 0}
    assert payload["provider"] == "stub"
    assert len(payload["digest"]) == 64
    output = capsys.readouterr().out
    assert "复核 4 条差异" in output
    assert "转人工复核      3" in output


def test_stub_include_duplicates(report_path: Path, tmp_path: Path) -> None:
    assert main(_args(report_path, tmp_path, "--stub", "--include-duplicates")) == 0
    payload = json.loads((tmp_path / "review_report.json").read_text(encoding="utf-8"))
    assert payload["summary"]["total"] == 6


def test_stub_limit(report_path: Path, tmp_path: Path) -> None:
    assert main(_args(report_path, tmp_path, "--stub", "--limit", "2")) == 0
    payload = json.loads((tmp_path / "review_report.json").read_text(encoding="utf-8"))
    assert payload["summary"]["total"] == 2


def test_threshold_routes_suggestion_to_manual(report_path: Path, tmp_path: Path) -> None:
    assert main(_args(report_path, tmp_path, "--stub", "--threshold", "0.95")) == 0
    payload = json.loads((tmp_path / "review_report.json").read_text(encoding="utf-8"))
    assert payload["summary"]["auto_suggested"] == 0
    assert payload["summary"]["manual_review"] == 4


def test_dry_run_does_not_call_or_write(report_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(_args(report_path, tmp_path, "--dry-run")) == 0
    assert not (tmp_path / "review_report.json").exists()
    output = capsys.readouterr().out
    assert "dry-run" in output
    assert "amount_mismatch" in output


def test_missing_report_returns_three(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["review", "--report", str(tmp_path / "nope.json"), "--out-dir", str(tmp_path)]) == 3
    assert "不存在" in capsys.readouterr().err


def test_invalid_report_returns_three(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{}", encoding="utf-8")
    assert main(["review", "--report", str(bad), "--out-dir", str(tmp_path)]) == 3
    assert "不是有效的对账报告" in capsys.readouterr().err


def test_real_mode_without_env_returns_three(
    report_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in ("LLM_API_BASE", "LLM_API_KEY", "LLM_MODEL"):
        monkeypatch.delenv(name, raising=False)
    assert main(_args(report_path, tmp_path)) == 3
    assert "LLM_API_KEY" in capsys.readouterr().err


def test_all_failures_return_one(report_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_settings() -> LLMSettings:
        return LLMSettings(api_base="https://api.example.invalid/v1", model="demo-model")

    errors = [LLMTransportError("HTTP_5XX", "boom", retryable=True) for _ in range(40)]
    monkeypatch.setattr("recon_workbench.cli.LLMSettings.from_env", staticmethod(fake_settings))
    monkeypatch.setattr(
        "recon_workbench.cli.OpenAICompatibleClient",
        lambda settings: StubClient(errors, name="openai-compatible", model=settings.model),
    )

    assert main(_args(report_path, tmp_path, "--threshold", "0.85")) == 1
    payload = json.loads((tmp_path / "review_report.json").read_text(encoding="utf-8"))
    assert payload["summary"]["failed"] == 4


def test_no_differences_writes_empty_batch(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    same = tmp_path / "same.csv"
    same.write_text(
        "发票号,供应商,日期,币种,金额\nINV-2099-0001,辰光金属（虚构）,2026-08-03,CNY,100.00\n",
        encoding="utf-8",
    )
    result = run_reconciliation(same, same)
    report = write_report_json(result.canonical_json, tmp_path / "recon_report.json")

    assert main(["review", "--report", str(report), "--out-dir", str(tmp_path), "--stub"]) == 0
    payload = json.loads((tmp_path / "review_report.json").read_text(encoding="utf-8"))
    assert payload["summary"]["total"] == 0
    assert "复核 0 条差异" in capsys.readouterr().out


def test_invalid_threshold_is_rejected(report_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(_args(report_path, tmp_path, "--stub", "--threshold", "2")) == 3
    assert "--threshold" in capsys.readouterr().err
