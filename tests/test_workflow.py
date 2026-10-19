"""工作流服务测试：任务身份、阶段推进、中断恢复、幂等、人工决策。"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path

import pytest

from recon_workbench.config import Policy
from recon_workbench.ingest import read_table
from recon_workbench.models import SourceName
from recon_workbench.review.client import StubClient
from recon_workbench.review.demo import demo_verdict
from recon_workbench.workflow import (
    HumanDecision,
    JobNotFound,
    JobStatus,
    ReviewSettings,
    StageStatus,
    WorkflowError,
    connect,
    get_status,
    list_jobs,
    record_decision,
    resume_job,
    start_job,
)
from recon_workbench.workflow.service import compute_job_id

SAMPLE_DIR = Path(__file__).resolve().parents[1] / "sample_data"
LEDGER = SAMPLE_DIR / "ledger_2026_08.csv"
STATEMENT = SAMPLE_DIR / "statement_2026_08.xlsx"


class RecordingProvider:
    """记录每次被要求服务的条目，并可按下标注入一次硬崩溃。"""

    def __init__(self, fail_at: int | None = None) -> None:
        self.fail_at = fail_at
        self.batches: list[list[str]] = []

    def __call__(self, requests):
        self.batches.append([request.item_id for request in requests])
        responses: list[str | Exception] = []
        for index, request in enumerate(requests):
            if self.fail_at is not None and index == self.fail_at:
                responses.append(RuntimeError("模拟进程崩溃"))
            else:
                responses.append(demo_verdict(request))
        return StubClient(responses)


@pytest.fixture
def conn(tmp_path: Path):
    connection = connect(tmp_path / "recon.db")
    yield connection
    connection.close()


def _review(**overrides) -> ReviewSettings:
    payload = {
        "enabled": True,
        "provider": "stub",
        "model": "stub-model",
        "threshold": 0.85,
        "max_attempts": 1,
        "limit": 20,
    }
    payload.update(overrides)
    return ReviewSettings(**payload)


def _start(
    conn, tmp_path: Path, *, review=None, policy=None, provider=None, force=False, ledger=LEDGER, statement=STATEMENT
):
    return start_job(
        conn,
        ledger_path=ledger,
        statement_path=statement,
        storage_dir=tmp_path / "jobs",
        policy=policy or Policy(),
        review=review or _review(),
        client_provider=provider or RecordingProvider(),
        force=force,
    )


def test_job_id_is_stable_for_same_content(conn, tmp_path: Path) -> None:
    ledger = tmp_path / "l.csv"
    statement = tmp_path / "s.xlsx"
    ledger.write_bytes(LEDGER.read_bytes())
    statement.write_bytes(STATEMENT.read_bytes())

    copied = _start(conn, tmp_path, ledger=ledger, statement=statement)
    original = _start(conn, tmp_path)
    assert copied.job_id == original.job_id
    assert len(list_jobs(conn)) == 1


def test_job_id_changes_with_policy_and_review_settings() -> None:
    ledger = read_table(LEDGER, SourceName.LEDGER)
    statement = read_table(STATEMENT, SourceName.STATEMENT)
    base = compute_job_id(ledger.sha256, statement.sha256, Policy(), _review())
    stricter = compute_job_id(ledger.sha256, statement.sha256, Policy(amount_tolerance=Decimal("0.01")), _review())
    other_model = compute_job_id(ledger.sha256, statement.sha256, Policy(), _review(model="another-model"))
    assert base != stricter
    assert base != other_model
    assert base.startswith("J") and len(base) == 17


def test_start_job_persists_stages_and_items(conn, tmp_path: Path) -> None:
    status = _start(conn, tmp_path)
    assert status.status == JobStatus.AWAITING_HUMAN.value
    assert [stage.status for stage in status.stages] == ["succeeded", "succeeded"]
    assert len(status.items) == 12
    assert status.counts["matched"] == 8
    assert len(status.suggestions) == 4
    assert len(status.pending) == 4


def test_same_inputs_reuse_job_without_rerunning(conn, tmp_path: Path) -> None:
    first = _start(conn, tmp_path)
    second = _start(conn, tmp_path)
    assert first.job_id == second.job_id
    assert [stage.attempts for stage in second.stages] == [1, 1]
    jobs = list_jobs(conn)
    assert len(jobs) == 1
    assert jobs[0].pending_count == 4


def test_force_rerun_increments_attempts_and_clears_state(conn, tmp_path: Path) -> None:
    first = _start(conn, tmp_path)
    record_decision(
        conn,
        job_id=first.job_id,
        item_id=first.pending[0].item_id,
        decision=HumanDecision.ACCEPTED.value,
        decided_by="张会计",
    )
    forced = _start(conn, tmp_path, force=True)
    assert [stage.attempts for stage in forced.stages] == [2, 2]
    assert len(forced.decisions) == 0
    assert len(forced.pending) == 4


def test_decisions_advance_job_to_completed(conn, tmp_path: Path) -> None:
    status = _start(conn, tmp_path)
    for item in list(status.pending):
        status = record_decision(
            conn,
            job_id=status.job_id,
            item_id=item.item_id,
            decision=HumanDecision.ACCEPTED.value,
            decided_by="张会计",
            note="演示",
        )
    assert status.status == JobStatus.COMPLETED.value
    assert len(status.decisions) == 4
    assert status.pending == ()
    assert any(event.event == "job_completed" for event in status.events)


def test_human_decision_does_not_overwrite_llm_suggestion(conn, tmp_path: Path) -> None:
    status = _start(conn, tmp_path)
    item = status.pending[0]
    before = next(suggestion for suggestion in status.suggestions if suggestion.item_id == item.item_id)
    record_decision(
        conn,
        job_id=status.job_id,
        item_id=item.item_id,
        decision=HumanDecision.REJECTED.value,
        decided_by="张会计",
        note="不同意",
    )
    after = get_status(conn, status.job_id)
    suggestion_after = next(s for s in after.suggestions if s.item_id == item.item_id)
    decision = next(d for d in after.decisions if d.item_id == item.item_id)
    assert decision.decision == HumanDecision.REJECTED.value
    assert decision.note == "不同意"
    assert (suggestion_after.verdict, suggestion_after.confidence) == (before.verdict, before.confidence)
    assert suggestion_after.status == before.status


def test_decision_on_matched_item_is_rejected(conn, tmp_path: Path) -> None:
    status = _start(conn, tmp_path)
    matched = next(item for item in status.items if item.status == "matched")
    with pytest.raises(WorkflowError):
        record_decision(
            conn,
            job_id=status.job_id,
            item_id=matched.item_id,
            decision=HumanDecision.ACCEPTED.value,
            decided_by="张会计",
        )


def test_unknown_item_is_rejected(conn, tmp_path: Path) -> None:
    status = _start(conn, tmp_path)
    with pytest.raises(WorkflowError):
        record_decision(
            conn,
            job_id=status.job_id,
            item_id="I-does-not-exist",
            decision=HumanDecision.ACCEPTED.value,
            decided_by="张会计",
        )


def test_review_can_be_disabled(conn, tmp_path: Path) -> None:
    status = _start(conn, tmp_path, review=_review(enabled=False), provider=None)
    assert status.stages[1].status == StageStatus.SKIPPED.value
    assert status.suggestions == ()
    assert status.status == JobStatus.AWAITING_HUMAN.value


def test_crash_during_review_is_recorded_and_resumable(conn, tmp_path: Path) -> None:
    failing = RecordingProvider(fail_at=1)
    crashed = _start(conn, tmp_path, provider=failing)
    assert crashed.status == JobStatus.FAILED.value
    assert crashed.stages[1].status == StageStatus.FAILED.value
    assert "模拟进程崩溃" in (crashed.stages[1].error or "")
    assert len(crashed.suggestions) == 1
    assert any(event.event == "stage_failed" for event in crashed.events)

    healthy = RecordingProvider()
    resumed = resume_job(conn, crashed.job_id, storage_dir=tmp_path / "jobs", client_provider=healthy)
    assert resumed.status == JobStatus.AWAITING_HUMAN.value
    assert resumed.stages[1].status == StageStatus.SUCCEEDED.value
    assert len(resumed.suggestions) == 4
    assert len(healthy.batches) == 1 and len(healthy.batches[0]) == 3


def test_resume_marks_stuck_running_stage_as_interrupted(conn, tmp_path: Path) -> None:
    crashed = _start(conn, tmp_path, provider=RecordingProvider(fail_at=0))
    assert crashed.stages[1].status == StageStatus.FAILED.value

    # 模拟进程在阶段执行中被强杀：状态停在 running
    conn.execute("UPDATE job_stages SET status = 'running' WHERE job_id = ? AND stage = 'review'", (crashed.job_id,))
    conn.execute("UPDATE jobs SET status = 'running' WHERE job_id = ?", (crashed.job_id,))

    resumed = resume_job(conn, crashed.job_id, storage_dir=tmp_path / "jobs", client_provider=RecordingProvider())
    assert resumed.stages[1].status == StageStatus.SUCCEEDED.value
    assert any(event.event == "stage_interrupted" for event in resumed.events)
    assert len(resumed.suggestions) == 4


def test_completed_job_resume_is_noop(conn, tmp_path: Path) -> None:
    status = _start(conn, tmp_path, review=_review(enabled=False), provider=None)
    for item in status.pending:
        record_decision(
            conn,
            job_id=status.job_id,
            item_id=item.item_id,
            decision=HumanDecision.ACCEPTED.value,
            decided_by="张会计",
        )
    again = resume_job(conn, status.job_id, storage_dir=tmp_path / "jobs")
    assert again.status == JobStatus.COMPLETED.value
    assert [stage.attempts for stage in again.stages] == [1, 0]


def test_artifacts_are_recorded_with_hashes(conn, tmp_path: Path) -> None:
    status = _start(conn, tmp_path)
    rows = list(
        conn.execute("SELECT kind, path, sha256 FROM artifacts WHERE job_id = ? ORDER BY kind", (status.job_id,))
    )
    assert [row["kind"] for row in rows] == ["recon_report", "recon_result", "review_report"]
    for row in rows:
        path = Path(row["path"])
        assert path.is_file()
        assert hashlib.sha256(path.read_bytes()).hexdigest() == row["sha256"]


def test_report_artifact_contains_digest_and_items(conn, tmp_path: Path) -> None:
    status = _start(conn, tmp_path)
    row = conn.execute(
        "SELECT path FROM artifacts WHERE job_id = ? AND kind = 'recon_report'", (status.job_id,)
    ).fetchone()
    payload = json.loads(Path(row["path"]).read_text(encoding="utf-8"))
    assert payload["digest"] == status.reconcile_digest
    assert len(payload["items"]) == 12


def test_unknown_job_raises(conn) -> None:
    with pytest.raises(JobNotFound):
        get_status(conn, "J0000000000000000")
