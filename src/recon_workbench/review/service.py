"""审核编排：请求构造、重试、路由。

安全底线：
- 系统不存在"自动通过"状态。高置信度最多得到 ``auto_suggested``（建议可直接采纳），
  且明确要求人工确认后才执行任何业务动作；低置信度、结论为 needs_review、调用失败一律 ``manual_review``/``failed``；
- 重试分两类：传输类错误按指数退避重试；输出不合法则带着具体错误要求模型修复，不盲目重发。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from ..models import Report, SourceName, Status, money_str
from .errors import LLMError, LLMStructuredError
from .parser import parse_verdict
from .prompts import build_messages, build_repair_messages
from .schema import OutcomeStatus, ReviewRequest, ReviewVerdict, Verdict


class _Client(Protocol):
    name: str
    model: str

    def complete(self, messages: Sequence[dict[str, str]]) -> str: ...


@dataclass(frozen=True, slots=True)
class ReviewPolicy:
    confidence_threshold: float = 0.85
    max_attempts: int = 3
    backoff_seconds: float = 0.5

    def __post_init__(self) -> None:
        if not 0 <= self.confidence_threshold <= 1:
            raise ValueError("confidence_threshold 必须在 0 到 1 之间")
        if self.max_attempts < 1:
            raise ValueError("max_attempts 至少为 1")
        if self.backoff_seconds < 0:
            raise ValueError("backoff_seconds 不能为负")


class ReviewOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    item_id: str
    status: OutcomeStatus
    invoice_no: str
    counterparty: str
    attempts: int
    verdict: ReviewVerdict | None = None
    error_code: str | None = None
    error_detail: str | None = None

    @property
    def needs_human_review(self) -> bool:
        return self.status is not OutcomeStatus.AUTO_SUGGESTED


def route(verdict: ReviewVerdict, policy: ReviewPolicy) -> OutcomeStatus:
    """决定结论是"建议可采纳"还是"必须人工"。"""

    if verdict.verdict is Verdict.NEEDS_REVIEW:
        return OutcomeStatus.MANUAL_REVIEW
    if verdict.confidence < policy.confidence_threshold:
        return OutcomeStatus.MANUAL_REVIEW
    return OutcomeStatus.AUTO_SUGGESTED


def build_requests(
    report: Report,
    *,
    include_duplicates: bool = False,
    limit: int | None = None,
) -> list[ReviewRequest]:
    """从对账报告里挑出需要复核的差异，转成 LLM 请求。"""

    requests: list[ReviewRequest] = []
    for item in report.items:
        if item.status is Status.MATCHED:
            continue
        primary = item.ledger or item.statement
        assert primary is not None
        requests.append(
            ReviewRequest(
                item_id=item.item_id,
                status=item.status.value,
                invoice_no=primary.invoice_no,
                counterparty=primary.counterparty,
                entry_date=primary.entry_date.isoformat(),
                currency=primary.currency,
                ledger_amount=money_str(item.ledger.amount) if item.ledger else None,
                statement_amount=money_str(item.statement.amount) if item.statement else None,
                delta=money_str(item.delta) if item.delta is not None else None,
                note=item.reason,
            )
        )

    if include_duplicates:
        for finding in report.duplicates:
            entry = finding.entry
            requests.append(
                ReviewRequest(
                    item_id=f"D-{entry.entry_id}",
                    status="duplicate",
                    invoice_no=entry.invoice_no,
                    counterparty=entry.counterparty,
                    entry_date=entry.entry_date.isoformat(),
                    currency=entry.currency,
                    ledger_amount=money_str(entry.amount) if entry.source is SourceName.LEDGER else None,
                    statement_amount=money_str(entry.amount) if entry.source is SourceName.STATEMENT else None,
                    note=finding.reason,
                )
            )

    if limit is not None:
        requests = requests[: max(0, limit)]
    return requests


def _truncate(text: str, limit: int = 300) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def review_one(
    request: ReviewRequest,
    client: _Client,
    policy: ReviewPolicy,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> ReviewOutcome:
    messages = build_messages(request)
    last_error: LLMError | None = None
    attempt = 0

    for attempt in range(1, policy.max_attempts + 1):
        try:
            raw = client.complete(messages)
        except LLMError as exc:
            last_error = exc
            if not exc.retryable or attempt >= policy.max_attempts:
                break
            sleep(policy.backoff_seconds * (2 ** (attempt - 1)))
            continue

        try:
            verdict = parse_verdict(raw)
        except LLMStructuredError as exc:
            last_error = exc
            if attempt >= policy.max_attempts:
                break
            messages = build_repair_messages(request, raw, exc.message)
            continue

        return ReviewOutcome(
            item_id=request.item_id,
            status=route(verdict, policy),
            invoice_no=request.invoice_no,
            counterparty=request.counterparty,
            attempts=attempt,
            verdict=verdict,
        )

    return ReviewOutcome(
        item_id=request.item_id,
        status=OutcomeStatus.FAILED,
        invoice_no=request.invoice_no,
        counterparty=request.counterparty,
        attempts=attempt,
        error_code=last_error.code if last_error else "UNKNOWN_ERROR",
        error_detail=_truncate(last_error.message) if last_error else "未知错误",
    )


def review_all(
    requests: Sequence[ReviewRequest],
    client: _Client,
    policy: ReviewPolicy,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> list[ReviewOutcome]:
    return [review_one(request, client, policy, sleep=sleep) for request in requests]
