"""工作流编排：阶段状态机、断点恢复、幂等写入、人工决策。

设计要点：
- **任务身份确定**：输入文件指纹 + 策略 + 复核配置 → ``job_id``，同输入重复执行落到同一个任务；
- **阶段幂等**：已成功的阶段不重跑；物品/建议/决策都以 ``(job_id, item_id)`` 为主键，重复写入只会覆盖同一行；
- **中断可定位**：阶段状态 + 事件日志；恢复时把 ``running`` 视为中断并重跑该阶段；
- **人工与 LLM 分离**：``review_suggestions`` 与 ``human_decisions`` 是两张表，互不覆盖；
- **绝不自动通过**：所有差异条目都必须有人工决策，任务才可能 ``completed``。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from ..canonical import canonical_dumps
from ..config import Policy
from ..errors import InputError, ReconError
from ..export import write_excel
from ..ingest import RawTable, read_table
from ..models import Report, SourceName, Status
from ..pipeline import run_from_tables
from ..review import build_batch, build_requests, file_json
from ..review.client import ChatClient
from ..review.schema import OutcomeStatus
from ..review.service import ReviewPolicy, ReviewRequest, review_one
from . import repository as repo
from .db import transaction
from .models import (
    DecisionView,
    EventView,
    HumanDecision,
    ItemView,
    JobStatus,
    JobSummary,
    StageName,
    StageStatus,
    StageView,
    SuggestionView,
    WorkflowStatus,
)

JOB_SCHEMA = "workflow/1"
STAGES = (StageName.RECONCILE.value, StageName.REVIEW.value)

ClientProvider = Callable[[list[ReviewRequest]], ChatClient]


class WorkflowError(ReconError):
    """工作流层错误（任务不存在、无法恢复、阶段失败等）。"""


class JobNotFound(WorkflowError):
    pass


@dataclass(frozen=True, slots=True)
class ReviewSettings:
    enabled: bool = True
    provider: str = "stub"
    model: str = "stub-model"
    threshold: float = 0.85
    max_attempts: int = 3
    limit: int = 20

    def to_json(self) -> str:
        return json.dumps(
            {
                "enabled": self.enabled,
                "provider": self.provider,
                "model": self.model,
                "threshold": self.threshold,
                "max_attempts": self.max_attempts,
                "limit": self.limit,
            },
            sort_keys=True,
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, raw: str) -> ReviewSettings:
        data = json.loads(raw)
        return cls(
            enabled=bool(data["enabled"]),
            provider=str(data["provider"]),
            model=str(data["model"]),
            threshold=float(data["threshold"]),
            max_attempts=int(data["max_attempts"]),
            limit=int(data["limit"]),
        )


def _policy_json(policy: Policy) -> str:
    return json.dumps(policy.snapshot().model_dump(mode="json"), sort_keys=True, ensure_ascii=False)


def _policy_from_json(raw: str) -> Policy:
    data = json.loads(raw)
    data["amount_tolerance"] = Decimal(data["amount_tolerance"])
    data["date_formats"] = tuple(data["date_formats"])
    data["allowed_currencies"] = tuple(data["allowed_currencies"])
    return Policy(**data)


def compute_job_id(
    ledger_sha256: str,
    statement_sha256: str,
    policy: Policy,
    review: ReviewSettings,
) -> str:
    """同输入 + 同配置 ⇒ 同一个 job_id；换文件或换模型都会得到新任务。"""

    identity = {
        "schema": JOB_SCHEMA,
        "ledger_sha256": ledger_sha256,
        "statement_sha256": statement_sha256,
        "policy": json.loads(_policy_json(policy)),
        "review": json.loads(review.to_json()),
    }
    return "J" + hashlib.sha256(canonical_dumps(identity)).hexdigest()[:16]


def _read_sources(job: sqlite3.Row, policy: Policy, review: ReviewSettings) -> tuple[RawTable, RawTable]:
    ledger_path = Path(job["ledger_path"])
    statement_path = Path(job["statement_path"])
    if not ledger_path.is_file() or not statement_path.is_file():
        raise InputError("源文件已不存在，无法恢复任务；请把文件放回原路径或重新创建任务")

    ledger_table = read_table(ledger_path, SourceName.LEDGER)
    statement_table = read_table(statement_path, SourceName.STATEMENT)
    expected = compute_job_id(ledger_table.sha256, statement_table.sha256, policy, review)
    if expected != job["job_id"]:
        raise InputError("源文件内容或配置已变化，任务身份不再匹配；请重新创建任务")
    return ledger_table, statement_table


def _write_artifact(
    connection: sqlite3.Connection,
    job_id: str,
    *,
    kind: str,
    path: Path,
    payload: bytes,
    at: str,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    repo.upsert_artifact(
        connection,
        job_id,
        kind=kind,
        path=str(path),
        sha256=hashlib.sha256(payload).hexdigest(),
        size=len(payload),
        at=at,
    )


def start_job(
    connection: sqlite3.Connection,
    *,
    ledger_path: Path | str,
    statement_path: Path | str,
    storage_dir: Path | str,
    policy: Policy | None = None,
    review: ReviewSettings | None = None,
    client_provider: ClientProvider | None = None,
    force: bool = False,
) -> WorkflowStatus:
    """创建（或复用）任务并推进到"待人工/完成"状态。"""

    active_policy = policy or Policy()
    active_review = review or ReviewSettings()
    ledger = Path(ledger_path).resolve()
    statement = Path(statement_path).resolve()

    ledger_table = read_table(ledger, SourceName.LEDGER)
    statement_table = read_table(statement, SourceName.STATEMENT)
    job_id = compute_job_id(ledger_table.sha256, statement_table.sha256, active_policy, active_review)

    at = repo.now_iso()
    with transaction(connection):
        created = repo.ensure_job(
            connection,
            job_id=job_id,
            ledger_path=str(ledger),
            statement_path=str(statement),
            ledger_filename=ledger.name,
            statement_filename=statement.name,
            ledger_sha256=ledger_table.sha256,
            statement_sha256=statement_table.sha256,
            policy_json=_policy_json(active_policy),
            review_json=active_review.to_json(),
            at=at,
        )
        repo.ensure_stages(connection, job_id, STAGES)
        if created:
            repo.add_event(connection, job_id, "job_created", at=at, detail=f"inputs {ledger.name} / {statement.name}")

    return run_job(
        connection,
        job_id,
        storage_dir=storage_dir,
        client_provider=client_provider,
        force=force,
    )


def run_job(
    connection: sqlite3.Connection,
    job_id: str,
    *,
    storage_dir: Path | str,
    client_provider: ClientProvider | None = None,
    force: bool = False,
) -> WorkflowStatus:
    job = repo.get_job(connection, job_id)
    if job is None:
        raise JobNotFound(f"任务不存在：{job_id}")

    if job["status"] == JobStatus.COMPLETED.value and not force:
        return get_status(connection, job_id)

    policy = _policy_from_json(job["policy_json"])
    review = ReviewSettings.from_json(job["review_json"])
    ledger_table, statement_table = _read_sources(job, policy, review)

    with transaction(connection):
        repo.set_job_status(connection, job_id, JobStatus.RUNNING.value, at=repo.now_iso())
        if force:
            repo.clear_suggestions(connection, job_id)
            repo.clear_decisions(connection, job_id)
            for stage in STAGES:
                connection.execute(
                    "UPDATE job_stages SET status = 'pending', error = NULL WHERE job_id = ? AND stage = ?",
                    (job_id, stage),
                )
            repo.add_event(connection, job_id, "force_rerun", at=repo.now_iso(), detail="清空建议与人工决策后重跑")

    reconcile_stage = repo.get_stage(connection, job_id, StageName.RECONCILE.value)
    if reconcile_stage is None or reconcile_stage["status"] != StageStatus.SUCCEEDED.value:
        _run_reconcile(
            connection,
            job_id,
            ledger_table=ledger_table,
            statement_table=statement_table,
            policy=policy,
            storage_dir=Path(storage_dir),
        )
    if _failed(connection, job_id):
        return get_status(connection, job_id)

    review_stage = repo.get_stage(connection, job_id, StageName.REVIEW.value)
    if not review.enabled:
        if review_stage is None or review_stage["status"] != StageStatus.SKIPPED.value:
            with transaction(connection):
                repo.mark_stage(
                    connection, job_id, StageName.REVIEW.value, StageStatus.SKIPPED.value, at=repo.now_iso()
                )
                repo.add_event(connection, job_id, "stage_skipped", at=repo.now_iso(), detail="review disabled")
    elif review_stage is None or review_stage["status"] != StageStatus.SUCCEEDED.value:
        _run_review(
            connection,
            job_id,
            review=review,
            storage_dir=Path(storage_dir),
            client_provider=client_provider,
        )
    if _failed(connection, job_id):
        return get_status(connection, job_id)

    _finalize(connection, job_id)
    return get_status(connection, job_id)


def _failed(connection: sqlite3.Connection, job_id: str) -> bool:
    job = repo.get_job(connection, job_id)
    return bool(job and job["status"] == JobStatus.FAILED.value)


def _run_reconcile(
    connection: sqlite3.Connection,
    job_id: str,
    *,
    ledger_table: RawTable,
    statement_table: RawTable,
    policy: Policy,
    storage_dir: Path,
) -> None:
    at = repo.now_iso()
    with transaction(connection):
        repo.mark_stage_running(connection, job_id, StageName.RECONCILE.value, at=at)
        repo.add_event(connection, job_id, "stage_started", at=at, detail=StageName.RECONCILE.value)

    try:
        result = run_from_tables(ledger_table, statement_table, policy)
        job_dir = storage_dir / job_id
        report_payload = (
            json.dumps(result.report.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, indent=2).encode(
                "utf-8"
            )
            + b"\n"
        )
        at = repo.now_iso()
        with transaction(connection):
            _write_artifact(
                connection,
                job_id,
                kind="recon_report",
                path=job_dir / "recon_report.json",
                payload=report_payload,
                at=at,
            )
            excel_path = write_excel(result.report, job_dir / "recon_result.xlsx")
            excel_bytes = excel_path.read_bytes()
            _write_artifact(
                connection,
                job_id,
                kind="recon_result",
                path=excel_path,
                payload=excel_bytes,
                at=at,
            )
            repo.replace_items(connection, job_id, list(result.report.items))
            repo.set_job_digest(connection, job_id, "reconcile", result.digest, at=at)
            repo.mark_stage(connection, job_id, StageName.RECONCILE.value, StageStatus.SUCCEEDED.value, at=at)
            repo.add_event(connection, job_id, "stage_succeeded", at=at, detail=StageName.RECONCILE.value)
    except Exception as exc:  # 阶段失败要落库，供恢复与定位
        at = repo.now_iso()
        with transaction(connection):
            repo.mark_stage(
                connection, job_id, StageName.RECONCILE.value, StageStatus.FAILED.value, at=at, error=str(exc)
            )
            repo.set_job_status(connection, job_id, JobStatus.FAILED.value, at=at, error=str(exc))
            repo.add_event(connection, job_id, "stage_failed", at=at, detail=f"reconcile: {exc}")


def _load_report(connection: sqlite3.Connection, job_id: str) -> Report:
    row = repo.get_artifact(connection, job_id, "recon_report")
    if row is None or not Path(row["path"]).is_file():
        raise WorkflowError("找不到对账报告产物，无法复核；请重新执行对账阶段")
    return Report.model_validate_json(Path(row["path"]).read_text(encoding="utf-8"))


def _run_review(
    connection: sqlite3.Connection,
    job_id: str,
    *,
    review: ReviewSettings,
    storage_dir: Path,
    client_provider: ClientProvider | None,
) -> None:
    at = repo.now_iso()
    with transaction(connection):
        repo.mark_stage_running(connection, job_id, StageName.REVIEW.value, at=at)
        repo.add_event(connection, job_id, "stage_started", at=at, detail=StageName.REVIEW.value)

    try:
        report = _load_report(connection, job_id)
        requests = build_requests(report, limit=review.limit)
        done = {
            row["item_id"]
            for row in repo.list_suggestions(connection, job_id)
            if row["status"] != OutcomeStatus.FAILED.value
        }
        policy = ReviewPolicy(confidence_threshold=review.threshold, max_attempts=review.max_attempts)

        pending = [request for request in requests if request.item_id not in done]
        if pending:
            if client_provider is None:
                raise WorkflowError("审核阶段需要 LLM 客户端，但未提供")
            client = client_provider(pending)
            for request in pending:
                outcome = review_one(request, client, policy)
                with transaction(connection):
                    repo.upsert_suggestion(
                        connection,
                        job_id,
                        outcome=outcome,
                        provider=review.provider,
                        model=review.model,
                        at=repo.now_iso(),
                    )

        outcomes = [repo.suggestion_to_outcome(row) for row in repo.list_suggestions(connection, job_id)]
        batch = build_batch(
            outcomes,
            provider=review.provider,
            model=review.model,
            policy=policy,
            generated_at=datetime.now().astimezone(),
        )
        at = repo.now_iso()
        with transaction(connection):
            _write_artifact(
                connection,
                job_id,
                kind="review_report",
                path=storage_dir / job_id / "review_report.json",
                payload=file_json(batch),
                at=at,
            )
            repo.set_job_digest(connection, job_id, "review", batch.digest, at=at)
            repo.mark_stage(connection, job_id, StageName.REVIEW.value, StageStatus.SUCCEEDED.value, at=at)
            repo.add_event(
                connection,
                job_id,
                "stage_succeeded",
                at=at,
                detail=f"review: {len(outcomes)} 条建议",
            )
    except Exception as exc:
        at = repo.now_iso()
        with transaction(connection):
            repo.mark_stage(connection, job_id, StageName.REVIEW.value, StageStatus.FAILED.value, at=at, error=str(exc))
            repo.set_job_status(connection, job_id, JobStatus.FAILED.value, at=at, error=str(exc))
            repo.add_event(connection, job_id, "stage_failed", at=at, detail=f"review: {exc}")


def _finalize(connection: sqlite3.Connection, job_id: str) -> None:
    job = repo.get_job(connection, job_id)
    if job is None or job["status"] == JobStatus.FAILED.value:
        return
    pending = repo.pending_items(connection, job_id)
    status = JobStatus.AWAITING_HUMAN.value if pending else JobStatus.COMPLETED.value
    at = repo.now_iso()
    with transaction(connection):
        repo.set_job_status(connection, job_id, status, at=at)
        if status == JobStatus.COMPLETED.value and job["status"] != JobStatus.COMPLETED.value:
            repo.add_event(connection, job_id, "job_completed", at=at, detail="所有差异均已人工决策")


def record_decision(
    connection: sqlite3.Connection,
    *,
    job_id: str,
    item_id: str,
    decision: str,
    decided_by: str,
    note: str | None = None,
) -> WorkflowStatus:
    job = repo.get_job(connection, job_id)
    if job is None:
        raise JobNotFound(f"任务不存在：{job_id}")
    item = repo.get_item(connection, job_id, item_id)
    if item is None:
        raise WorkflowError(f"条目不存在：{item_id}")
    if item["status"] == Status.MATCHED.value:
        raise WorkflowError("已匹配的条目不需要人工决策")
    if decision not in {value.value for value in HumanDecision}:
        allowed = " / ".join(value.value for value in HumanDecision)
        raise WorkflowError(f"decision 必须是 {allowed}")
    if not decided_by.strip():
        raise WorkflowError("decided_by 不能为空")

    at = repo.now_iso()
    with transaction(connection):
        repo.upsert_decision(
            connection,
            job_id,
            item_id=item_id,
            decision=decision,
            decided_by=decided_by.strip(),
            note=note.strip() if note else None,
            at=at,
        )
        repo.add_event(
            connection,
            job_id,
            "human_decision",
            at=at,
            detail=f"{item_id} -> {decision} by {decided_by}",
        )
    _finalize(connection, job_id)
    return get_status(connection, job_id)


def resume_job(
    connection: sqlite3.Connection,
    job_id: str,
    *,
    storage_dir: Path | str,
    client_provider: ClientProvider | None = None,
) -> WorkflowStatus:
    job = repo.get_job(connection, job_id)
    if job is None:
        raise JobNotFound(f"任务不存在：{job_id}")
    if job["status"] == JobStatus.COMPLETED.value:
        return get_status(connection, job_id)

    at = repo.now_iso()
    for stage in repo.list_stages(connection, job_id):
        if stage["status"] == StageStatus.RUNNING.value:
            with transaction(connection):
                repo.mark_stage(connection, job_id, stage["stage"], StageStatus.INTERRUPTED.value, at=at)
                repo.add_event(connection, job_id, "stage_interrupted", at=at, detail=stage["stage"])

    return run_job(connection, job_id, storage_dir=storage_dir, client_provider=client_provider)


def get_status(connection: sqlite3.Connection, job_id: str) -> WorkflowStatus:
    job = repo.get_job(connection, job_id)
    if job is None:
        raise JobNotFound(f"任务不存在：{job_id}")

    items = [_item_view(row) for row in repo.list_items(connection, job_id)]
    suggestions = [_suggestion_view(row) for row in repo.list_suggestions(connection, job_id)]
    decisions = [_decision_view(row) for row in repo.list_decisions(connection, job_id)]
    stages = [
        StageView(
            stage=row["stage"],
            label=StageName(row["stage"]).label_zh,
            status=row["status"],
            attempts=row["attempts"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            error=row["error"],
        )
        for row in repo.list_stages(connection, job_id)
    ]
    pending = [_item_view(row) for row in repo.pending_items(connection, job_id)]
    events = [
        EventView(at=row["at"], event=row["event"], detail=row["detail"])
        for row in repo.list_events(connection, job_id)
    ]
    review = ReviewSettings.from_json(job["review_json"])

    return WorkflowStatus(
        job_id=job["job_id"],
        status=job["status"],
        created_at=job["created_at"],
        updated_at=job["updated_at"],
        ledger_filename=job["ledger_filename"],
        statement_filename=job["statement_filename"],
        reconcile_digest=job["reconcile_digest"],
        review_digest=job["review_digest"],
        review_enabled=review.enabled,
        review_provider=review.provider,
        review_model=review.model,
        review_threshold=review.threshold,
        error=job["error"],
        stages=tuple(stages),
        counts=repo.counts_by_status(connection, job_id),
        items=tuple(items),
        pending=tuple(pending),
        suggestions=tuple(suggestions),
        decisions=tuple(decisions),
        events=tuple(events),
    )


def list_jobs(connection: sqlite3.Connection, limit: int = 20) -> list[JobSummary]:
    summaries: list[JobSummary] = []
    for job in repo.list_jobs(connection, limit):
        pending = repo.pending_items(connection, job["job_id"])
        summaries.append(
            JobSummary(
                job_id=job["job_id"],
                status=job["status"],
                created_at=job["created_at"],
                updated_at=job["updated_at"],
                ledger_filename=job["ledger_filename"],
                statement_filename=job["statement_filename"],
                pending_count=len(pending),
            )
        )
    return summaries


def _item_view(row: sqlite3.Row) -> ItemView:
    return ItemView(
        item_id=row["item_id"],
        status=row["status"],
        invoice_no=row["invoice_no"],
        counterparty=row["counterparty"],
        entry_date=row["entry_date"],
        currency=row["currency"],
        ledger_amount=row["ledger_amount"],
        statement_amount=row["statement_amount"],
        delta=row["delta"],
        reason=row["reason"],
    )


def _suggestion_view(row: sqlite3.Row) -> SuggestionView:
    return SuggestionView(
        item_id=row["item_id"],
        status=row["status"],
        verdict=row["verdict"],
        category=row["category"],
        confidence=row["confidence"],
        explanation=row["explanation"],
        recommended_action=row["recommended_action"],
        attempts=row["attempts"],
        error_code=row["error_code"],
        error_detail=row["error_detail"],
    )


def _decision_view(row: sqlite3.Row) -> DecisionView:
    return DecisionView(
        item_id=row["item_id"],
        decision=row["decision"],
        decided_by=row["decided_by"],
        note=row["note"],
        decided_at=row["decided_at"],
    )
