"""结构化输出解析测试：代码块、夹带文字、坏 JSON、缺字段。"""

from __future__ import annotations

import json

import pytest

from recon_workbench.review.errors import LLMStructuredError
from recon_workbench.review.parser import extract_json_object, parse_verdict

VALID = {
    "verdict": "approve",
    "category": "amount_diff",
    "confidence": 0.9,
    "explanation": "差额来自运费，台账未拆分。",
    "recommended_action": "请供应商补充运费发票。",
    "evidence": ["差额 270.00"],
}


def _dump(**overrides: object) -> str:
    payload = dict(VALID)
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


def test_plain_json() -> None:
    verdict = parse_verdict(_dump())
    assert verdict.verdict.value == "approve"
    assert verdict.confidence == 0.9
    assert verdict.evidence == ["差额 270.00"]


def test_fenced_json_block() -> None:
    text = f"```json\n{_dump()}\n```"
    assert parse_verdict(text).category.value == "amount_diff"


def test_json_embedded_in_prose() -> None:
    text = f"结论如下：\n{_dump()}\n以上。"
    assert parse_verdict(text).verdict.value == "approve"


def test_empty_output() -> None:
    with pytest.raises(LLMStructuredError) as excinfo:
        parse_verdict("   ")
    assert excinfo.value.code == "EMPTY_RESPONSE"


def test_broken_json() -> None:
    with pytest.raises(LLMStructuredError) as excinfo:
        parse_verdict('{"verdict": "approve", ')
    assert excinfo.value.code == "INVALID_JSON"


def test_top_level_array_is_rejected() -> None:
    with pytest.raises(LLMStructuredError) as excinfo:
        extract_json_object("[1, 2, 3]")
    assert excinfo.value.code == "INVALID_JSON"


def test_missing_field() -> None:
    payload = dict(VALID)
    del payload["recommended_action"]
    with pytest.raises(LLMStructuredError) as excinfo:
        parse_verdict(json.dumps(payload, ensure_ascii=False))
    assert excinfo.value.code == "SCHEMA_VALIDATION_FAILED"
    assert "recommended_action" in str(excinfo.value)


def test_unknown_verdict_value() -> None:
    with pytest.raises(LLMStructuredError) as excinfo:
        parse_verdict(_dump(verdict="approved"))
    assert excinfo.value.code == "SCHEMA_VALIDATION_FAILED"


def test_confidence_out_of_range() -> None:
    with pytest.raises(LLMStructuredError) as excinfo:
        parse_verdict(_dump(confidence=1.4))
    assert "confidence" in str(excinfo.value)


def test_extra_fields_are_ignored() -> None:
    payload = dict(VALID)
    payload["reasoning"] = "模型多给了一段思考"
    assert parse_verdict(json.dumps(payload, ensure_ascii=False)).verdict.value == "approve"


def test_blank_explanation_is_rejected() -> None:
    with pytest.raises(LLMStructuredError):
        parse_verdict(_dump(explanation="   "))


def test_evidence_is_truncated_to_five() -> None:
    verdict = parse_verdict(_dump(evidence=[f"证据{i}" for i in range(8)]))
    assert len(verdict.evidence) == 5
