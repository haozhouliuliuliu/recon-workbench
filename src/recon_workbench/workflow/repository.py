"""工作流的数据访问层。

只做 SQL，不放业务判断：状态推进、幂等规则由 service 决定。
所有主键都是 (job_id, ...)，从数据库层面挡住重复写入。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from decimal import Decimal

from ..models import ReconItem, Status, money_str
from ..review.schema import ReviewVerdict
from ..review.service import ReviewOutcome


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------- jobs


def ensure_job(
    connection: sqlite3.Connection,
    *,
    job_id: str,
    ledger_path: str,
    statement_path: str,
    ledger_filename: str,
    statement_filename: str,
    ledger_sha256: str,
    statement_sha256: str,
    policy_json: str,
    review_json: str,
    at: str,
) -> bool:
    cursor = connection.execute(
        """
        INSERT INTO jobs (
            job_id, status, ledger_path, statement_path, ledger_filename, statement_filename,
            ledger_sha256, statement_sha256, policy_json, review_json, created_at, updated_at
        ) VALUES (?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(job_id) DO NOTHING
        """,
        (
            job_id,
            ledger_path,
            statement_path,
            ledger_filename,
            statement_filename,
            ledger_sha256,
            statement_sha256,
            policy_json,
            review_json,
            at,
            at,
        ),
    )
    return cursor.rowcount == 1


def get_job(connection: sqlite3.Connection, job_id: str) -> sqlite3.Row | None:
    return connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()


def list_jobs(connection: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    return list(connection.execute("SELECT * FROM jobs ORDER BY created_at DESC, job_id LIMIT ?", (limit,)))


def set_job_status(
    connection: sqlite3.Connection,
    job_id: str,
    status: str,
    *,
    at: str,
    error: str | None = None,
) -> None:
    connection.execute(
        "UPDATE jobs SET status = ?, error = ?, updated_at = ? WHERE job_id = ?",
        (status, error, at, job_id),
    )


def set_job_digest(connection: sqlite3.Connection, job_id: str, kind: str, digest: str, *, at: str) -> None:
    column = {"reconcile": "reconcile_digest", "review": "review_digest"}[kind]
    connection.execute(
        f"UPDATE jobs SET {column} = ?, updated_at = ? WHERE job_id = ?",
        (digest, at, job_id),
    )


# ---------------------------------------------------------------- stages


def ensure_stages(connection: sqlite3.Connection, job_id: str, stages: tuple[str, ...]) -> None:
    for stage in stages:
        connection.execute(
            "INSERT INTO job_stages (job_id, stage, status, attempts) VALUES (?, ?, 'pending', 0) "
            "ON CONFLICT(job_id, stage) DO NOTHING",
            (job_id, stage),
        )


def get_stage(connection: sqlite3.Connection, job_id: str, stage: str) -> sqlite3.Row | None:
    return connection.execute("SELECT * FROM job_stages WHERE job_id = ? AND stage = ?", (job_id, stage)).fetchone()


def list_stages(connection: sqlite3.Connection, job_id: str) -> list[sqlite3.Row]:
    return list(connection.execute("SELECT * FROM job_stages WHERE job_id = ? ORDER BY rowid", (job_id,)))


def mark_stage_running(connection: sqlite3.Connection, job_id: str, stage: str, *, at: str) -> None:
    connection.execute(
        """
        UPDATE job_stages
           SET status = 'running', attempts = attempts + 1, started_at = ?, finished_at = NULL, error = NULL
         WHERE job_id = ? AND stage = ?
        """,
        (at, job_id, stage),
    )


def mark_stage(
    connection: sqlite3.Connection,
    job_id: str,
    stage: str,
    status: str,
    *,
    at: str,
    error: str | None = None,
) -> None:
    connection.execute(
        "UPDATE job_stages SET status = ?, finished_at = ?, error = ? WHERE job_id = ? AND stage = ?",
        (status, at, error, job_id, stage),
    )


# ---------------------------------------------------------------- items


def replace_items(connection: sqlite3.Connection, job_id: str, items: list[ReconItem]) -> None:
    connection.execute("DELETE FROM recon_items WHERE job_id = ?", (job_id,))
    for item in items:
        primary = item.ledger or item.statement
        assert primary is not None
        connection.execute(
            """
            INSERT INTO recon_items (
                job_id, item_id, status, invoice_no, counterparty, entry_date, currency,
                ledger_amount, statement_amount, delta, matched_by, reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                item.item_id,
                item.status.value,
                primary.invoice_no,
                primary.counterparty,
                primary.entry_date.isoformat(),
                primary.currency,
                money_str(item.ledger.amount) if item.ledger else None,
                money_str(item.statement.amount) if item.statement else None,
                money_str(item.delta) if item.delta is not None else None,
                item.matched_by,
                item.reason,
            ),
        )


def list_items(connection: sqlite3.Connection, job_id: str) -> list[sqlite3.Row]:
    return list(connection.execute("SELECT * FROM recon_items WHERE job_id = ? ORDER BY rowid", (job_id,)))


def get_item(connection: sqlite3.Connection, job_id: str, item_id: str) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM recon_items WHERE job_id = ? AND item_id = ?", (job_id, item_id)
    ).fetchone()


# ---------------------------------------------------------------- review suggestions


def upsert_suggestion(
    connection: sqlite3.Connection,
    job_id: str,
    *,
    outcome: ReviewOutcome,
    provider: str,
    model: str,
    at: str,
) -> None:
    verdict = outcome.verdict
    connection.execute(
        """
        INSERT INTO review_suggestions (
            job_id, item_id, invoice_no, counterparty, status, verdict, category, confidence, explanation,
            recommended_action, evidence, attempts, provider, model, error_code, error_detail, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(job_id, item_id) DO UPDATE SET
            invoice_no = excluded.invoice_no,
            counterparty = excluded.counterparty,
            status = excluded.status,
            verdict = excluded.verdict,
            category = excluded.category,
            confidence = excluded.confidence,
            explanation = excluded.explanation,
            recommended_action = excluded.recommended_action,
            evidence = excluded.evidence,
            attempts = excluded.attempts,
            provider = excluded.provider,
            model = excluded.model,
            error_code = excluded.error_code,
            error_detail = excluded.error_detail,
            created_at = excluded.created_at
        """,
        (
            job_id,
            outcome.item_id,
            outcome.invoice_no,
            outcome.counterparty,
            outcome.status.value,
            verdict.verdict.value if verdict else None,
            verdict.category.value if verdict else None,
            verdict.confidence if verdict else None,
            verdict.explanation if verdict else None,
            verdict.recommended_action if verdict else None,
            json.dumps(verdict.evidence, ensure_ascii=False) if verdict else None,
            outcome.attempts,
            provider,
            model,
            outcome.error_code,
            outcome.error_detail,
            at,
        ),
    )


def list_suggestions(connection: sqlite3.Connection, job_id: str) -> list[sqlite3.Row]:
    return list(connection.execute("SELECT * FROM review_suggestions WHERE job_id = ? ORDER BY rowid", (job_id,)))


def clear_suggestions(connection: sqlite3.Connection, job_id: str) -> None:
    connection.execute("DELETE FROM review_suggestions WHERE job_id = ?", (job_id,))


def suggestion_to_outcome(row: sqlite3.Row) -> ReviewOutcome:
    """把库里的建议还原成 ReviewOutcome，供恢复时跳过已完成的条目。"""

    verdict: ReviewVerdict | None = None
    if row["verdict"] is not None:
        verdict = ReviewVerdict(
            verdict=row["verdict"],
            category=row["category"],
            confidence=row["confidence"],
            explanation=row["explanation"],
            recommended_action=row["recommended_action"],
            evidence=json.loads(row["evidence"] or "[]"),
        )
    return ReviewOutcome(
        item_id=row["item_id"],
        status=row["status"],
        invoice_no=row["invoice_no"],
        counterparty=row["counterparty"],
        attempts=row["attempts"],
        verdict=verdict,
        error_code=row["error_code"],
        error_detail=row["error_detail"],
    )


# ---------------------------------------------------------------- human decisions


def upsert_decision(
    connection: sqlite3.Connection,
    job_id: str,
    *,
    item_id: str,
    decision: str,
    decided_by: str,
    note: str | None,
    at: str,
) -> None:
    connection.execute(
        """
        INSERT INTO human_decisions (job_id, item_id, decision, decided_by, note, decided_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(job_id, item_id) DO UPDATE SET
            decision = excluded.decision,
            decided_by = excluded.decided_by,
            note = excluded.note,
            decided_at = excluded.decided_at
        """,
        (job_id, item_id, decision, decided_by, note, at),
    )


def list_decisions(connection: sqlite3.Connection, job_id: str) -> list[sqlite3.Row]:
    return list(connection.execute("SELECT * FROM human_decisions WHERE job_id = ? ORDER BY rowid", (job_id,)))


def clear_decisions(connection: sqlite3.Connection, job_id: str) -> None:
    connection.execute("DELETE FROM human_decisions WHERE job_id = ?", (job_id,))


# ---------------------------------------------------------------- events & artifacts


def add_event(connection: sqlite3.Connection, job_id: str, event: str, *, at: str, detail: str | None = None) -> None:
    connection.execute(
        "INSERT INTO job_events (job_id, at, event, detail) VALUES (?, ?, ?, ?)",
        (job_id, at, event, detail),
    )


def list_events(connection: sqlite3.Connection, job_id: str, limit: int = 50) -> list[sqlite3.Row]:
    return list(
        connection.execute("SELECT * FROM job_events WHERE job_id = ? ORDER BY id DESC LIMIT ?", (job_id, limit))
    )


def upsert_artifact(
    connection: sqlite3.Connection,
    job_id: str,
    *,
    kind: str,
    path: str,
    sha256: str,
    size: int,
    at: str,
) -> None:
    connection.execute(
        """
        INSERT INTO artifacts (job_id, kind, path, sha256, bytes, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(job_id, kind) DO UPDATE SET
            path = excluded.path,
            sha256 = excluded.sha256,
            bytes = excluded.bytes,
            created_at = excluded.created_at
        """,
        (job_id, kind, path, sha256, size, at),
    )


def get_artifact(connection: sqlite3.Connection, job_id: str, kind: str) -> sqlite3.Row | None:
    return connection.execute("SELECT * FROM artifacts WHERE job_id = ? AND kind = ?", (job_id, kind)).fetchone()


# ---------------------------------------------------------------- helpers


def pending_items(connection: sqlite3.Connection, job_id: str) -> list[sqlite3.Row]:
    """需要人工决策但还没有决策的差异条目。"""

    return list(
        connection.execute(
            """
            SELECT i.* FROM recon_items i
            LEFT JOIN human_decisions d ON d.job_id = i.job_id AND d.item_id = i.item_id
            WHERE i.job_id = ? AND i.status != ? AND d.item_id IS NULL
            ORDER BY i.rowid
            """,
            (job_id, Status.MATCHED.value),
        )
    )


def counts_by_status(connection: sqlite3.Connection, job_id: str) -> dict[str, int]:
    rows = connection.execute(
        "SELECT status, COUNT(*) AS n FROM recon_items WHERE job_id = ? GROUP BY status", (job_id,)
    )
    return {row["status"]: row["n"] for row in rows}


def decimal_or_none(value: object) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))
