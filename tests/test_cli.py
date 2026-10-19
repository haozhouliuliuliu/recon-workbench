"""CLI 行为测试：退出码、输出文件、标准输出。"""

from __future__ import annotations

from pathlib import Path

import pytest

from recon_workbench.cli import main

SAMPLE_DIR = Path(__file__).resolve().parents[1] / "sample_data"
LEDGER = SAMPLE_DIR / "ledger_2026_08.csv"
STATEMENT = SAMPLE_DIR / "statement_2026_08.xlsx"


def _args(out_dir: Path, *extra: str) -> list[str]:
    return [
        "run",
        "--ledger",
        str(LEDGER),
        "--statement",
        str(STATEMENT),
        "--out-dir",
        str(out_dir),
        "--now",
        "2026-10-18T12:00:00",
        *extra,
    ]


def test_run_writes_both_outputs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(_args(tmp_path)) == 0
    assert (tmp_path / "recon_report.json").is_file()
    assert (tmp_path / "recon_result.xlsx").is_file()
    output = capsys.readouterr().out
    assert "已匹配" in output
    assert "85,480.75" in output


def test_fail_on_difference_returns_one(tmp_path: Path) -> None:
    assert main(_args(tmp_path, "--fail-on-difference")) == 1


def test_missing_input_returns_three(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(
        [
            "run",
            "--ledger",
            str(tmp_path / "nope.csv"),
            "--statement",
            str(STATEMENT),
            "--out-dir",
            str(tmp_path),
        ]
    )
    assert code == 3
    assert "错误" in capsys.readouterr().err


def test_bad_now_returns_three(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    args = _args(tmp_path)
    args[args.index("--now") + 1] = "not-a-time"
    assert main(args) == 3
    assert "--now" in capsys.readouterr().err


def test_quiet_suppresses_summary(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(_args(tmp_path, "--quiet")) == 0
    assert capsys.readouterr().out.strip() == ""


def test_print_json_emits_canonical_report(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(_args(tmp_path, "--quiet", "--print-json")) == 0
    output = capsys.readouterr().out
    assert output.startswith('{"counts"')
    assert output.count("\n") == 1


def test_version_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
    assert "0.4.0" in capsys.readouterr().out
