"""报告的数据模型。

这里是报告结构的唯一事实来源：CLI、Excel 导出、后续的 API 与状态机都从这里取字段。
模型冻结（frozen），避免下游随手改报告导致同输入不同结果。
"""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_serializer

MONEY_QUANT = Decimal("0.01")


def money_str(value: Decimal) -> str:
    """金额统一序列化为两位小数字符串，保证跨平台、跨版本字节一致。"""

    return format(value.quantize(MONEY_QUANT, rounding=ROUND_HALF_UP), "f")


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


class SourceName(StrEnum):
    LEDGER = "ledger"
    STATEMENT = "statement"

    @property
    def label_zh(self) -> str:
        return {"ledger": "台账", "statement": "对账单"}[self.value]


class Status(StrEnum):
    MATCHED = "matched"
    AMOUNT_MISMATCH = "amount_mismatch"
    ONLY_IN_LEDGER = "only_in_ledger"
    ONLY_IN_STATEMENT = "only_in_statement"

    @property
    def label_zh(self) -> str:
        return {
            "matched": "已匹配",
            "amount_mismatch": "金额不一致",
            "only_in_ledger": "仅台账有",
            "only_in_statement": "仅对账单有",
        }[self.value]


class Entry(BaseModel):
    """一条标准化后的单据记录。"""

    model_config = ConfigDict(frozen=True)

    entry_id: str
    source: SourceName
    row_number: int
    invoice_no: str
    counterparty: str
    entry_date: date
    currency: str
    amount: Decimal
    doc_type: str = ""
    note: str = ""

    @field_serializer("amount")
    def _serialize_amount(self, value: Decimal) -> str:
        return money_str(value)


class Issue(BaseModel):
    """一条校验/匹配过程中发现的问题。"""

    model_config = ConfigDict(frozen=True)

    code: str
    severity: Severity
    source: SourceName
    row_number: int | None = None
    field: str | None = None
    message: str


class InvalidRow(BaseModel):
    """字段校验未通过、未进入匹配的记录。"""

    model_config = ConfigDict(frozen=True)

    source: SourceName
    row_number: int
    raw: dict[str, str]
    issue_codes: tuple[str, ...]


class DuplicateFinding(BaseModel):
    """重复单据。

    exact：同一来源内发票号完全重复，后出现的记录不参与匹配。
    suspected：供应商、日期、金额相同但发票号不同，仅告警，仍参与匹配。
    """

    model_config = ConfigDict(frozen=True)

    source: SourceName
    kind: str = Field(pattern="^(exact|suspected)$")
    entry: Entry
    related_row_number: int
    reason: str


class ReconItem(BaseModel):
    """一条匹配结果：可能是成对记录，也可能是单边记录。"""

    model_config = ConfigDict(frozen=True)

    item_id: str
    status: Status
    ledger: Entry | None = None
    statement: Entry | None = None
    delta: Decimal | None = None
    matched_by: str | None = None
    reason: str = ""

    @field_serializer("delta")
    def _serialize_delta(self, value: Decimal | None) -> str | None:
        return None if value is None else money_str(value)


class CurrencyTotals(BaseModel):
    model_config = ConfigDict(frozen=True)

    currency: str
    ledger_total: Decimal
    statement_total: Decimal
    difference: Decimal

    @field_serializer("ledger_total", "statement_total", "difference")
    def _serialize_totals(self, value: Decimal) -> str:
        return money_str(value)


class StatusCounts(BaseModel):
    model_config = ConfigDict(frozen=True)

    ledger_rows: int = 0
    statement_rows: int = 0
    ledger_accepted: int = 0
    statement_accepted: int = 0
    matched: int = 0
    amount_mismatch: int = 0
    only_in_ledger: int = 0
    only_in_statement: int = 0
    duplicates: int = 0
    invalid_rows: int = 0
    issues_error: int = 0
    issues_warning: int = 0


class SourceInfo(BaseModel):
    """输入文件的指纹与规模，用于事后复现。"""

    model_config = ConfigDict(frozen=True)

    source: SourceName
    filename: str
    sha256: str
    total_rows: int
    accepted_rows: int
    rejected_rows: int


class PolicySnapshot(BaseModel):
    """当次运行使用的策略快照；报告自带策略，换参数重跑结果不同也说得清。"""

    model_config = ConfigDict(frozen=True)

    amount_tolerance: Decimal
    invoice_pattern: str
    invoice_min_length: int
    invoice_max_length: int
    counterparty_max_length: int
    note_max_length: int
    allow_negative_amount: bool
    max_decimal_places: int
    date_formats: tuple[str, ...]
    allowed_currencies: tuple[str, ...]

    @field_serializer("amount_tolerance")
    def _serialize_tolerance(self, value: Decimal) -> str:
        return money_str(value)


class Report(BaseModel):
    """一次对账的完整、可复现结论。"""

    model_config = ConfigDict(frozen=True)

    schema_version: str = "recon-report/1"
    tool_version: str
    policy: PolicySnapshot
    sources: tuple[SourceInfo, ...]
    counts: StatusCounts
    totals: tuple[CurrencyTotals, ...]
    items: tuple[ReconItem, ...]
    duplicates: tuple[DuplicateFinding, ...]
    invalid_rows: tuple[InvalidRow, ...]
    issues: tuple[Issue, ...]
    digest: str = ""
