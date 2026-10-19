"""recon workflow 命令测试：任务推进、状态查询、人工决策、错误退出码。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from recon_workbench.cli import main
from recon_workbench.workflow import JobStatus, connect, get_status, list_jobs

SAMPLE_DIR = Path(__file__).resolve().parents[1] / "sample_data"
LEDGER = SAMPLE_DIR / "ledger_2026_08.csv"
STATEMENT = SAMPLE_DIR / "statement_2026_08.xlsx"


def _db(tmp_path: Path) -> Path:
    return tmp_path / "recon.db"


def _run_args(tmp_path: Path, *extra: str) -> list[str]:
    return [
        "workflow",
        "run",
        "--ledger",
        str(LEDGER),
        "--statement",
        str(STATEMENT),
        "--db",
        str(_db(tmp_path)),
        "--storage",
        str(tmp_path / "jobs"),
        *extra,
    ]


def _only_job(tmp_path: Path) -> str:
    connection = connect(_db(tmp_path))
    try:
        return list_jobs(connection)[0].job_id
    finally:
        connection.close()


def test_run_with_stub(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(_run_args(tmp_path, "--stub")) == 0
    output = capsys.readouterr().out
    assert "待人工复核" in output
    assert "复核建议：建议采纳 1 / 转人工 3" in output
    assert "人工决策：0 / 4" in output

    connection = connect(_db(tmp_path))
    try:
        status = get_status(connection, _only_job(tmp_path))
    finally:
        connection.close()
    assert status.status == JobStatus.AWAITING_HUMAN.value
    assert len(status.suggestions) == 4


def test_run_twice_reuses_job(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(_run_args(tmp_path, "--stub")) == 0
    assert main(_run_args(tmp_path, "--stub")) == 0
    capsys.readouterr()

    connection = connect(_db(tmp_path))
    try:
        jobs = list_jobs(connection)
        status = get_status(connection, jobs[0].job_id)
    finally:
        connection.close()
    assert len(jobs) == 1
    assert [stage.attempts for stage in status.stages] == [1, 1]


def test_no_review_skips_stage(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(_run_args(tmp_path, "--no-review")) == 0
    output = capsys.readouterr().out
    assert "已跳过" in output
    connection = connect(_db(tmp_path))
    try:
        status = get_status(connection, _only_job(tmp_path))
    finally:
        connection.close()
    assert status.suggestions == ()
    assert status.pending_count == 4


def test_status_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(_run_args(tmp_path, "--stub", "--quiet")) == 0
    capsys.readouterr()
    job_id = _only_job(tmp_path)

    assert main(["workflow", "status", job_id, "--db", str(_db(tmp_path)), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["job_id"] == job_id
    assert payload["status"] == "awaiting_human"
    assert len(payload["pending"]) == 4


def test_list_shows_job(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(_run_args(tmp_path, "--stub", "--quiet")) == 0
    capsys.readouterr()
    assert main(["workflow", "list", "--db", str(_db(tmp_path))]) == 0
    output = capsys.readouterr().out
    assert _only_job(tmp_path) in output
    assert "ledger_2026_08.csv" in output


def test_decide_completes_job(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(_run_args(tmp_path, "--stub", "--quiet")) == 0
    job_id = _only_job(tmp_path)
    connection = connect(_db(tmp_path))
    try:
        pending = [item.item_id for item in get_status(connection, job_id).pending]
    finally:
        connection.close()

    for item_id in pending:
        assert (
            main(
                [
                    "workflow",
                    "decide",
                    job_id,
                    item_id,
                    "--decision",
                    "accepted",
                    "--by",
                    "张会计",
                    "--db",
                    str(_db(tmp_path)),
                    "--quiet",
                ]
            )
            == 0
        )
    capsys.readouterr()

    assert main(["workflow", "status", job_id, "--db", str(_db(tmp_path)), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "completed"
    assert len(payload["decisions"]) == 4


def test_status_missing_job_returns_three(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["workflow", "status", "J0000000000000000", "--db", str(_db(tmp_path))]) == 3
    assert "不存在" in capsys.readouterr().err


def test_invalid_decision_choice_is_usage_error(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(
            [
                "workflow",
                "decide",
                "J1",
                "I1",
                "--decision",
                "maybe",
                "--by",
                "x",
                "--db",
                str(_db(tmp_path)),
            ]
        )
    assert excinfo.value.code == 2


def test_review_enabled_without_env_returns_three(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in ("LLM_API_BASE", "LLM_API_KEY", "LLM_MODEL"):
        monkeypatch.delenv(name, raising=False)
    assert main(_run_args(tmp_path)) == 3
    assert "LLM_API_KEY" in capsys.readouterr().err


def test_resume_completed_job_is_noop(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(_run_args(tmp_path, "--no-review", "--quiet")) == 0
    job_id = _only_job(tmp_path)
    connection = connect(_db(tmp_path))
    try:
        for item in get_status(connection, job_id).pending:
            connection.execute(
                "INSERT INTO human_decisions (job_id, item_id, decision, decided_by, decided_at) "
                "VALUES (?, ?, 'accepted', '测试', '2026-10-19T00:00:00+08:00')",
                (job_id, item.item_id),
            )
    finally:
        connection.close()

    assert main(["workflow", "resume", job_id, "--db", str(_db(tmp_path)), "--storage", str(tmp_path / "jobs")]) == 0
    output = capsys.readouterr().out
    assert "已完成" in output
