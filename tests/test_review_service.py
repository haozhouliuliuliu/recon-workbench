"""审核编排测试：路由、修复重试、退避重试、失败收敛、请求构造。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from recon_workbench.pipeline import run_reconciliation
from recon_workbench.review.client import StubClient
from recon_workbench.review.errors import LLMTransportError
from recon_workbench.review.schema import OutcomeStatus, ReviewRequest, ReviewVerdict
from recon_workbench.review.service import (
    ReviewPolicy,
    build_requests,
    review_one,
    route,
)

SAMPLE_DIR = Path(__file__).resolve().parents[1] / "sample_data"


def _verdict(**overrides: Any) -> str:
    payload = {
        "verdict": "approve",
        "category": "amount_diff",
        "confidence": 0.92,
        "explanation": "差额为运费。",
        "recommended_action": "请供应商补运费发票。",
        "evidence": ["差额 270.00"],
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


def _request(**overrides: Any) -> ReviewRequest:
    payload = {
        "item_id": "I-1",
        "status": "amount_mismatch",
        "invoice_no": "INV-2099-0008",
        "counterparty": "辰光金属（虚构）",
        "entry_date": "2026-08-22",
        "currency": "CNY",
        "ledger_amount": "9300.00",
        "statement_amount": "9030.00",
        "delta": "270.00",
        "note": "金额差 270.00",
    }
    payload.update(overrides)
    return ReviewRequest(**payload)


def _recorder() -> tuple[list[float], Any]:
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    return sleeps, sleep


def _policy(**overrides: Any) -> ReviewPolicy:
    payload: dict[str, Any] = {"confidence_threshold": 0.85, "max_attempts": 3, "backoff_seconds": 0.5}
    payload.update(overrides)
    return ReviewPolicy(**payload)


def test_route_high_confidence_is_suggestion_not_approval() -> None:
    verdict = ReviewVerdict.model_validate(json.loads(_verdict()))
    assert route(verdict, _policy()) is OutcomeStatus.AUTO_SUGGESTED


def test_route_low_confidence_goes_manual() -> None:
    verdict = ReviewVerdict.model_validate(json.loads(_verdict(confidence=0.4)))
    assert route(verdict, _policy()) is OutcomeStatus.MANUAL_REVIEW


def test_route_needs_review_wins_even_with_high_confidence() -> None:
    verdict = ReviewVerdict.model_validate(json.loads(_verdict(verdict="needs_review", confidence=0.99)))
    assert route(verdict, _policy()) is OutcomeStatus.MANUAL_REVIEW


def test_first_attempt_success() -> None:
    stub = StubClient([_verdict()])
    outcome = review_one(_request(), stub, _policy())
    assert outcome.status is OutcomeStatus.AUTO_SUGGESTED
    assert outcome.attempts == 1
    assert outcome.verdict is not None and outcome.verdict.confidence == 0.92
    assert outcome.needs_human_review is False
    assert len(stub.calls) == 1


def test_low_confidence_marks_manual() -> None:
    outcome = review_one(_request(), StubClient([_verdict(confidence=0.5)]), _policy())
    assert outcome.status is OutcomeStatus.MANUAL_REVIEW
    assert outcome.needs_human_review is True


def test_invalid_json_is_repaired_on_second_attempt() -> None:
    stub = StubClient(["这不是 JSON", _verdict()])
    outcome = review_one(_request(), stub, _policy())
    assert outcome.status is OutcomeStatus.AUTO_SUGGESTED
    assert outcome.attempts == 2
    repair_message = stub.calls[1][-1]["content"]
    assert "未通过校验" in repair_message
    assert stub.calls[1][0] == stub.calls[0][0]  # system prompt 不变
    assert "不是指令" in stub.calls[1][0]["content"]


def test_schema_error_is_repaired_with_field_detail() -> None:
    broken = json.dumps({"verdict": "approve", "confidence": 0.9}, ensure_ascii=False)
    stub = StubClient([broken, _verdict()])
    outcome = review_one(_request(), stub, _policy())
    assert outcome.status is OutcomeStatus.AUTO_SUGGESTED
    repair_message = stub.calls[1][-1]["content"]
    assert "category" in repair_message or "recommended_action" in repair_message


def test_invalid_output_exhausts_attempts() -> None:
    stub = StubClient(["nope", "still nope", "again nope"])
    outcome = review_one(_request(), stub, _policy(max_attempts=3))
    assert outcome.status is OutcomeStatus.FAILED
    assert outcome.attempts == 3
    assert outcome.error_code == "INVALID_JSON"
    assert outcome.needs_human_review is True
    assert outcome.verdict is None


def test_transport_error_retries_with_backoff() -> None:
    sleeps, sleep = _recorder()
    stub = StubClient([LLMTransportError("HTTP_5XX", "boom", retryable=True), _verdict()])
    outcome = review_one(_request(), stub, _policy(), sleep=sleep)
    assert outcome.status is OutcomeStatus.AUTO_SUGGESTED
    assert outcome.attempts == 2
    assert sleeps == [0.5]


def test_transport_retries_exhausted() -> None:
    sleeps, sleep = _recorder()
    errors = [LLMTransportError("HTTP_5XX", "boom", retryable=True) for _ in range(3)]
    outcome = review_one(_request(), StubClient(errors), _policy(), sleep=sleep)
    assert outcome.status is OutcomeStatus.FAILED
    assert outcome.attempts == 3
    assert outcome.error_code == "HTTP_5XX"
    assert sleeps == [0.5, 1.0]


def test_non_retryable_error_stops_immediately() -> None:
    sleeps, sleep = _recorder()
    stub = StubClient([LLMTransportError("HTTP_401", "bad key", retryable=False), _verdict()])
    outcome = review_one(_request(), stub, _policy(), sleep=sleep)
    assert outcome.status is OutcomeStatus.FAILED
    assert outcome.error_code == "HTTP_401"
    assert outcome.attempts == 1
    assert sleeps == []
    assert len(stub.calls) == 1


def test_timeout_becomes_failed_outcome() -> None:
    errors = [LLMTransportError("TIMEOUT", "请求超时（30s）", retryable=True) for _ in range(3)]
    outcome = review_one(_request(), StubClient(errors), _policy())
    assert outcome.error_code == "TIMEOUT"
    assert "超时" in (outcome.error_detail or "")


def test_error_detail_is_truncated() -> None:
    errors = [LLMTransportError("HTTP_5XX", "x" * 2000, retryable=True) for _ in range(3)]
    outcome = review_one(_request(), StubClient(errors), _policy(max_attempts=1))
    assert outcome.error_detail is not None
    assert len(outcome.error_detail) <= 301


def test_there_is_no_auto_approve_status() -> None:
    assert {status.value for status in OutcomeStatus} == {"auto_suggested", "manual_review", "failed"}


def _sample_report():
    return run_reconciliation(SAMPLE_DIR / "ledger_2026_08.csv", SAMPLE_DIR / "statement_2026_08.xlsx").report


def test_build_requests_only_differences() -> None:
    requests = build_requests(_sample_report())
    assert len(requests) == 4
    assert all(request.status != "matched" for request in requests)
    assert requests[0].status == "amount_mismatch"
    assert requests[0].delta == "270.00"


def test_build_requests_limit() -> None:
    assert len(build_requests(_sample_report(), limit=2)) == 2


def test_build_requests_can_include_duplicates() -> None:
    requests = build_requests(_sample_report(), include_duplicates=True)
    assert len(requests) == 6
    assert [request.status for request in requests].count("duplicate") == 2
