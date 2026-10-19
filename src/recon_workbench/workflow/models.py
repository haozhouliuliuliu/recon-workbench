"""工作流对外视图模型。

这些模型只用于展示/接口输出，不参与数据库写入；数据库行到视图的映射在 service 里完成。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    AWAITING_HUMAN = "awaiting_human"
    COMPLETED = "completed"
    FAILED = "failed"


class StageName(StrEnum):
    RECONCILE = "reconcile"
    REVIEW = "review"

    @property
    def label_zh(self) -> str:
        return {"reconcile": "对账", "review": "LLM 复核"}[self.value]


class StageStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    SKIPPED = "skipped"

    @property
    def label_zh(self) -> str:
        return {
            "pending": "待执行",
            "running": "执行中",
            "succeeded": "已完成",
            "failed": "失败",
            "interrupted": "已中断",
            "skipped": "已跳过",
        }[self.value]


class HumanDecision(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    NEEDS_INFO = "needs_info"

    @property
    def label_zh(self) -> str:
        return {"accepted": "认可", "rejected": "驳回", "needs_info": "退回补充"}[self.value]


class StageView(BaseModel):
    model_config = ConfigDict(frozen=True)

    stage: str
    label: str
    status: str
    attempts: int
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None


class ItemView(BaseModel):
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
    reason: str = ""


class SuggestionView(BaseModel):
    model_config = ConfigDict(frozen=True)

    item_id: str
    status: str
    verdict: str | None = None
    category: str | None = None
    confidence: float | None = None
    explanation: str | None = None
    recommended_action: str | None = None
    attempts: int = 0
    error_code: str | None = None
    error_detail: str | None = None


class DecisionView(BaseModel):
    model_config = ConfigDict(frozen=True)

    item_id: str
    decision: str
    decided_by: str
    note: str | None = None
    decided_at: str


class EventView(BaseModel):
    model_config = ConfigDict(frozen=True)

    at: str
    event: str
    detail: str | None = None


class JobSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    job_id: str
    status: str
    created_at: str
    updated_at: str
    ledger_filename: str
    statement_filename: str
    pending_count: int


class WorkflowStatus(BaseModel):
    model_config = ConfigDict(frozen=True)

    job_id: str
    status: str
    created_at: str
    updated_at: str
    ledger_filename: str
    statement_filename: str
    reconcile_digest: str | None = None
    review_digest: str | None = None
    review_enabled: bool = True
    review_provider: str = ""
    review_model: str = ""
    review_threshold: float = 0.85
    error: str | None = None
    stages: tuple[StageView, ...] = ()
    counts: dict[str, int] = {}
    items: tuple[ItemView, ...] = ()
    pending: tuple[ItemView, ...] = ()
    suggestions: tuple[SuggestionView, ...] = ()
    decisions: tuple[DecisionView, ...] = ()
    events: tuple[EventView, ...] = ()

    @property
    def pending_count(self) -> int:
        return len(self.pending)
