"""字段标准化与校验。

设计要点：
- 单行出错不中断整批，问题以 Issue 记录，坏行进 invalid_rows；
- 金额一律 Decimal + ROUND_HALF_UP，禁止 float 参与运算；
- 所有文本先做 NFKC，避免全角/半角、零宽字符导致的"看起来一样但对不上"。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from .config import Policy
from .ingest import RawTable
from .models import Entry, InvalidRow, Issue, Severity, SourceName, money_str

FOOTER_LABELS = {"合计", "总计", "小计", "total", "subtotal", "sum"}

CURRENCY_ALIASES = {
    "RMB": "CNY",
    "人民币": "CNY",
    "元": "CNY",
    "美元": "USD",
    "美金": "USD",
    "欧元": "EUR",
    "港币": "HKD",
}

_MONEY_STRIP = re.compile(r"[\s,，¥￥$€、]")
_PAREN_NEGATIVE = re.compile(r"^\((.*)\)$")
_WHITESPACE = re.compile(r"\s+")


def clean_text(raw: str) -> str:
    """NFKC + 去首尾空白 + 合并连续空白。"""

    return _WHITESPACE.sub(" ", unicodedata.normalize("NFKC", raw)).strip()


def parse_date(raw: str, policy: Policy) -> date | None:
    text = clean_text(raw)
    if not text:
        return None
    if "T" in text:
        text = text.split("T", 1)[0]
    for fmt in policy.date_formats:
        try:
            if fmt == "%Y-%m-%d":
                return date.fromisoformat(text)
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    # Excel 序列号（如 45870）兜底：仅当纯数字且落在合理区间时按 1899-12-30 起算
    if text.isdigit() and 20000 <= int(text) <= 80000:
        return date(1899, 12, 30) + timedelta(days=int(text))
    return None


def parse_amount(raw: str, policy: Policy) -> tuple[Decimal | None, list[str]]:
    """返回 (金额, 告警代码)。无法解析返回 (None, [])。"""

    warnings: list[str] = []
    text = unicodedata.normalize("NFKC", raw).strip()
    if not text:
        return None, warnings

    negative = False
    match = _PAREN_NEGATIVE.match(text)
    if match:
        negative = True
        text = match.group(1)

    text = _MONEY_STRIP.sub("", text).removeprefix("+")
    if text.startswith("-"):
        negative = True
        text = text[1:]
    if not text:
        return None, warnings

    try:
        value = Decimal(text)
    except InvalidOperation:
        return None, warnings
    if not value.is_finite():
        return None, warnings
    if negative:
        value = -value

    quantum = Decimal(1).scaleb(-policy.max_decimal_places)
    rounded = value.quantize(quantum, rounding=ROUND_HALF_UP)
    if rounded != value:
        warnings.append("AMOUNT_ROUNDED")

    if not policy.allow_negative_amount and rounded < 0:
        return rounded, [*warnings, "AMOUNT_NEGATIVE"]
    return rounded, warnings


def parse_currency(raw: str, policy: Policy) -> str | None:
    text = clean_text(raw).upper()
    if not text:
        return None
    text = CURRENCY_ALIASES.get(text, text)
    return text if text in policy.allowed_currencies else None


def normalize_invoice(raw: str, policy: Policy) -> str | None:
    text = clean_text(raw).upper().replace(" ", "")
    if not text:
        return None
    if not policy.invoice_min_length <= len(text) <= policy.invoice_max_length:
        return None
    if not re.fullmatch(policy.invoice_pattern, text):
        return None
    return text


def _entry_id(
    source: SourceName,
    row_number: int,
    invoice: str,
    counterparty: str,
    day: date,
    amount: Decimal,
) -> str:
    payload = "|".join(
        [source.value, str(row_number), invoice, counterparty.casefold(), day.isoformat(), money_str(amount)]
    )
    return "E" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def _issue(
    code: str,
    source: SourceName,
    row_number: int,
    field: str | None,
    message: str,
    severity: Severity = Severity.ERROR,
) -> Issue:
    return Issue(
        code=code,
        severity=severity,
        source=source,
        row_number=row_number,
        field=field,
        message=message,
    )


def _validate_row(
    values: dict[str, str],
    row_number: int,
    source: SourceName,
    policy: Policy,
) -> tuple[Entry | None, list[Issue]]:
    """校验并标准化一行；存在 error 级问题时返回 (None, issues)。"""

    issues: list[Issue] = []

    def fail(
        code: str,
        field: str | None,
        message: str,
        severity: Severity = Severity.ERROR,
    ) -> None:
        issues.append(_issue(code, source, row_number, field, message, severity))

    counterparty = clean_text(values.get("counterparty", ""))
    if not counterparty:
        fail("MISSING_REQUIRED_FIELD", "counterparty", "供应商为空")
    elif len(counterparty) > policy.counterparty_max_length:
        fail(
            "FIELD_TOO_LONG",
            "counterparty",
            f"供应商标称长度 {len(counterparty)} 超过上限 {policy.counterparty_max_length}",
        )

    invoice_raw = clean_text(values.get("invoice_no", ""))
    invoice_no = normalize_invoice(invoice_raw, policy)
    if not invoice_raw:
        fail("MISSING_REQUIRED_FIELD", "invoice_no", "发票号为空")
    elif invoice_no is None:
        fail("INVALID_INVOICE_NO", "invoice_no", f"发票号格式不合法：{invoice_raw!r}")

    entry_date = parse_date(values.get("entry_date", ""), policy)
    if entry_date is None:
        raw_date = clean_text(values.get("entry_date", ""))
        if raw_date:
            fail("INVALID_DATE", "entry_date", f"日期无法解析：{raw_date!r}")
        else:
            fail("MISSING_REQUIRED_FIELD", "entry_date", "日期为空")
    elif not 2000 <= entry_date.year <= 2100:
        fail(
            "DATE_OUT_OF_RANGE",
            "entry_date",
            f"日期 {entry_date.isoformat()} 超出合理范围（2000-2100）",
            Severity.WARNING,
        )

    currency = parse_currency(values.get("currency", ""), policy)
    if currency is None:
        raw_currency = clean_text(values.get("currency", ""))
        if raw_currency:
            fail("UNKNOWN_CURRENCY", "currency", f"不支持的币种：{raw_currency!r}")
        else:
            fail("MISSING_REQUIRED_FIELD", "currency", "币种为空")

    amount, amount_warnings = parse_amount(values.get("amount", ""), policy)
    if amount is None:
        raw_amount = clean_text(values.get("amount", ""))
        if raw_amount:
            fail("INVALID_AMOUNT", "amount", f"金额无法解析：{raw_amount!r}")
        else:
            fail("MISSING_REQUIRED_FIELD", "amount", "金额为空")
    else:
        if "AMOUNT_NEGATIVE" in amount_warnings:
            fail("AMOUNT_NEGATIVE", "amount", f"金额为负：{money_str(amount)}")
        if "AMOUNT_ROUNDED" in amount_warnings:
            fail(
                "AMOUNT_ROUNDED",
                "amount",
                f"金额小数位超过 {policy.max_decimal_places} 位，已四舍五入",
                Severity.WARNING,
            )

    doc_type = clean_text(values.get("doc_type", ""))
    note = clean_text(values.get("note", ""))
    if len(note) > policy.note_max_length:
        note = note[: policy.note_max_length]
        fail(
            "FIELD_TRUNCATED",
            "note",
            f"备注超过 {policy.note_max_length} 字，已截断",
            Severity.WARNING,
        )

    if any(issue.severity is Severity.ERROR for issue in issues):
        return None, issues

    assert invoice_no is not None and entry_date is not None and currency is not None and amount is not None
    return (
        Entry(
            entry_id=_entry_id(source, row_number, invoice_no, counterparty, entry_date, amount),
            source=source,
            row_number=row_number,
            invoice_no=invoice_no,
            counterparty=counterparty,
            entry_date=entry_date,
            currency=currency,
            amount=amount,
            doc_type=doc_type,
            note=note,
        ),
        issues,
    )


def normalize_table(table: RawTable, policy: Policy) -> tuple[list[Entry], list[InvalidRow], list[Issue]]:
    """逐行标准化并校验，返回 (有效记录, 无效行, 问题列表)。"""

    entries: list[Entry] = []
    invalid_rows: list[InvalidRow] = []
    issues: list[Issue] = []
    source = table.source

    for raw in table.rows:
        values = raw.values
        if clean_text(values.get("invoice_no", "")).casefold() in FOOTER_LABELS:
            continue

        entry, row_issues = _validate_row(values, raw.row_number, source, policy)
        issues.extend(row_issues)
        if entry is None:
            error_codes = sorted({issue.code for issue in row_issues if issue.severity is Severity.ERROR})
            invalid_rows.append(
                InvalidRow(
                    source=source,
                    row_number=raw.row_number,
                    raw={key: values.get(key, "") for key in sorted(values)},
                    issue_codes=tuple(error_codes),
                )
            )
            continue
        entries.append(entry)

    return entries, invalid_rows, issues
