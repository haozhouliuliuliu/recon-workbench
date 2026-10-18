"""审核批次报告测试：摘要、可复现摘要、落盘内容。"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

from recon_workbench.review.report import build_batch, canonical_json, file_json
from recon_workbench.review.schema import OutcomeStatus, ReviewVerdict
from recon_workbench.review.service import ReviewOutcome, ReviewPolicy

VERDICT = ReviewVerdict(
    verdict="approve",
    category="amount_diff",
    confidence=0.9,
    explanation="差额为运费。",
    recommended_action="补运费发票。",
)


def _outcome(status: OutcomeStatus, item_id: str = "I-1") -> ReviewOutcome:
    return ReviewOutcome(
        item_id=item_id,
        status=status,
        invoice_no="INV-2099-0008",
        counterparty="辰光金属（虚构）",
        attempts=1,
        verdict=VERDICT if status is not OutcomeStatus.FAILED else None,
        error_code="TIMEOUT" if status is OutcomeStatus.FAILED else None,
    )


def _batch(outcomes=None, generated_at: datetime | None = None):
    return build_batch(
        outcomes or [_outcome(OutcomeStatus.AUTO_SUGGESTED), _outcome(OutcomeStatus.MANUAL_REVIEW)],
        provider="stub",
        model="stub-model",
        policy=ReviewPolicy(),
        generated_at=generated_at or datetime(2026, 10, 18, 12, 0, 0),
    )


def test_summary_counts() -> None:
    batch = build_batch(
        [
            _outcome(OutcomeStatus.AUTO_SUGGESTED, "I-1"),
            _outcome(OutcomeStatus.MANUAL_REVIEW, "I-2"),
            _outcome(OutcomeStatus.MANUAL_REVIEW, "I-3"),
            _outcome(OutcomeStatus.FAILED, "I-4"),
        ],
        provider="stub",
        model="stub-model",
        policy=ReviewPolicy(),
        generated_at=datetime(2026, 10, 18, 12, 0, 0),
    )
    assert batch.summary.total == 4
    assert batch.summary.auto_suggested == 1
    assert batch.summary.manual_review == 2
    assert batch.summary.failed == 1


def test_digest_matches_canonical_payload() -> None:
    batch = _batch()
    assert len(batch.digest) == 64
    assert batch.digest == hashlib.sha256(canonical_json(batch)).hexdigest()


def test_canonical_payload_excludes_time_and_digest() -> None:
    payload = canonical_json(_batch()).decode("utf-8")
    assert "2026-10-18" not in payload
    assert '"digest"' not in payload


def test_digest_is_stable_across_generation_times() -> None:
    first = _batch(generated_at=datetime(2026, 10, 18, 12, 0, 0))
    second = _batch(generated_at=datetime(2026, 11, 1, 8, 30, 0))
    assert first.digest == second.digest


def test_file_json_contains_summary_and_digest() -> None:
    batch = _batch()
    payload = json.loads(file_json(batch))
    assert payload["digest"] == batch.digest
    assert payload["generated_at"].startswith("2026-10-18T12:00:00")
    assert payload["summary"]["total"] == 2
    assert payload["policy"]["confidence_threshold"] == 0.85
