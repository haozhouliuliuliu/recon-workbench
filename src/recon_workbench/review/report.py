"""审核批次报告：可归档、可比较、可追溯到具体模型与阈值。"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from .. import __version__
from ..canonical import canonical_model_bytes
from .schema import OutcomeStatus
from .service import ReviewOutcome, ReviewPolicy


class PolicySnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    confidence_threshold: float
    max_attempts: int


class ReviewSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    total: int
    auto_suggested: int
    manual_review: int
    failed: int


class ReviewBatch(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: str = "review-report/1"
    tool_version: str
    provider: str
    model: str
    policy: PolicySnapshot
    generated_at: datetime
    summary: ReviewSummary
    outcomes: tuple[ReviewOutcome, ...]
    digest: str = ""


def summarize(outcomes: list[ReviewOutcome]) -> ReviewSummary:
    return ReviewSummary(
        total=len(outcomes),
        auto_suggested=sum(1 for outcome in outcomes if outcome.status is OutcomeStatus.AUTO_SUGGESTED),
        manual_review=sum(1 for outcome in outcomes if outcome.status is OutcomeStatus.MANUAL_REVIEW),
        failed=sum(1 for outcome in outcomes if outcome.status is OutcomeStatus.FAILED),
    )


def canonical_json(batch: ReviewBatch) -> bytes:
    """批次摘要只覆盖可比较内容：不含生成时间与摘要自身。"""

    return canonical_model_bytes(batch, exclude={"digest", "generated_at"})


def file_json(batch: ReviewBatch) -> bytes:
    """落盘版本：包含生成时间与摘要，缩进 2 空格便于人工阅读。"""

    payload = batch.model_dump(mode="json")
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False).encode("utf-8") + b"\n"


def build_batch(
    outcomes: list[ReviewOutcome],
    *,
    provider: str,
    model: str,
    policy: ReviewPolicy,
    generated_at: datetime,
) -> ReviewBatch:
    batch = ReviewBatch(
        tool_version=__version__,
        provider=provider,
        model=model,
        policy=PolicySnapshot(
            confidence_threshold=policy.confidence_threshold,
            max_attempts=policy.max_attempts,
        ),
        generated_at=generated_at,
        summary=summarize(outcomes),
        outcomes=tuple(outcomes),
    )
    digest = hashlib.sha256(canonical_json(batch)).hexdigest()
    return batch.model_copy(update={"digest": digest})
