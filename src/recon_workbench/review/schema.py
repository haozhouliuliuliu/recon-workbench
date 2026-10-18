"""LLM 审核的数据契约：请求、结构化输出、结果状态。

这里的 Pydantic 模型是"LLM 能说什么"的唯一边界：
模型返回的任何内容都必须先通过 ReviewVerdict 校验，才可能进入业务结论。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Verdict(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    NEEDS_REVIEW = "needs_review"


class Category(StrEnum):
    AMOUNT_DIFF = "amount_diff"
    MISSING_IN_STATEMENT = "missing_in_statement"
    MISSING_IN_LEDGER = "missing_in_ledger"
    DUPLICATE = "duplicate"
    DATA_ERROR = "data_error"
    TIMING = "timing"
    OTHER = "other"


class ReviewVerdict(BaseModel):
    """一次审核结论。字段全部必填（evidence 除外），多给字段忽略。"""

    model_config = ConfigDict(extra="ignore")

    verdict: Verdict
    category: Category
    confidence: float = Field(ge=0.0, le=1.0, description="0-1，模型自评置信度")
    explanation: str = Field(min_length=1, max_length=500)
    recommended_action: str = Field(min_length=1, max_length=200)
    evidence: list[str] = Field(default_factory=list)

    @field_validator("explanation", "recommended_action")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("不能为空白")
        return stripped

    @field_validator("evidence")
    @classmethod
    def _clean_evidence(cls, value: list[str]) -> list[str]:
        return [item.strip() for item in value if item.strip()][:5]


class ReviewRequest(BaseModel):
    """交给 LLM 的单条待复核事实。只允许出现引擎已确认的字段。"""

    model_config = ConfigDict(frozen=True)

    item_id: str
    status: str
    invoice_no: str
    counterparty: str
    entry_date: str
    currency: str
    ledger_amount: str | None = None
    statement_amount: str | None = None
    delta: str | None = None
    note: str = ""

    def to_facts(self) -> dict[str, object]:
        return self.model_dump(exclude={"item_id"})


class OutcomeStatus(StrEnum):
    AUTO_SUGGESTED = "auto_suggested"
    MANUAL_REVIEW = "manual_review"
    FAILED = "failed"

    @property
    def label_zh(self) -> str:
        return {
            "auto_suggested": "建议可直接采纳",
            "manual_review": "转人工复核",
            "failed": "审核失败",
        }[self.value]
