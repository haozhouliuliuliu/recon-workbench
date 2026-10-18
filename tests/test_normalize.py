"""字段标准化与校验测试。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from recon_workbench.config import Policy
from recon_workbench.models import Severity
from recon_workbench.normalize import normalize_table

from .helpers import raw_table

POLICY = Policy()


def _valid(**overrides: str) -> dict[str, str]:
    row = {
        "invoice_no": "INV-2099-0001",
        "counterparty": "辰光金属（虚构）",
        "entry_date": "2026-08-03",
        "currency": "CNY",
        "amount": "12800.00",
    }
    row.update(overrides)
    return row


def test_valid_row_becomes_entry() -> None:
    entries, invalid, issues = normalize_table(raw_table([_valid()]), POLICY)
    assert not invalid and not issues
    entry = entries[0]
    assert entry.invoice_no == "INV-2099-0001"
    assert entry.entry_date == date(2026, 8, 3)
    assert entry.amount == Decimal("12800.00")
    assert entry.currency == "CNY"
    assert entry.row_number == 2
    assert entry.entry_id.startswith("E")


def test_fullwidth_digits_are_normalized() -> None:
    entries, _, _ = normalize_table(raw_table([_valid(amount="１２８００．５０")]), POLICY)
    assert entries[0].amount == Decimal("12800.50")


def test_missing_invoice_is_rejected() -> None:
    _, invalid, issues = normalize_table(raw_table([_valid(invoice_no="")]), POLICY)
    assert invalid[0].issue_codes == ("MISSING_REQUIRED_FIELD",)
    assert any(issue.code == "MISSING_REQUIRED_FIELD" and issue.severity is Severity.ERROR for issue in issues)


def test_invalid_date_is_rejected() -> None:
    _, invalid, issues = normalize_table(raw_table([_valid(entry_date="2026-13-40")]), POLICY)
    assert invalid and invalid[0].issue_codes == ("INVALID_DATE",)
    assert issues[0].field == "entry_date"


def test_invalid_amount_is_rejected() -> None:
    _, invalid, _ = normalize_table(raw_table([_valid(amount="abc")]), POLICY)
    assert invalid and "INVALID_AMOUNT" in invalid[0].issue_codes


def test_negative_amount_is_rejected() -> None:
    _, invalid, _ = normalize_table(raw_table([_valid(amount="-100.00")]), POLICY)
    assert "AMOUNT_NEGATIVE" in invalid[0].issue_codes


def test_unknown_currency_is_rejected() -> None:
    _, invalid, _ = normalize_table(raw_table([_valid(currency="XYZ")]), POLICY)
    assert "UNKNOWN_CURRENCY" in invalid[0].issue_codes


def test_currency_alias_rmb_maps_to_cny() -> None:
    entries, _, _ = normalize_table(raw_table([_valid(currency="人民币")]), POLICY)
    assert entries[0].currency == "CNY"


def test_invalid_invoice_pattern_is_rejected() -> None:
    _, invalid, _ = normalize_table(raw_table([_valid(invoice_no="12 34")]), POLICY)
    assert "INVALID_INVOICE_NO" in invalid[0].issue_codes


def test_extra_decimals_warn_but_accept() -> None:
    entries, invalid, issues = normalize_table(raw_table([_valid(amount="100.005")]), POLICY)
    assert not invalid
    assert entries[0].amount == Decimal("100.01")
    codes = {issue.code for issue in issues}
    assert "AMOUNT_ROUNDED" in codes
    warning = next(issue for issue in issues if issue.code == "AMOUNT_ROUNDED")
    assert warning.severity is Severity.WARNING


def test_footer_rows_are_skipped_silently() -> None:
    rows = [
        _valid(invoice_no="INV-2099-0001"),
        {"invoice_no": "合计", "counterparty": "", "entry_date": "", "currency": "", "amount": "12800.00"},
    ]
    entries, invalid, issues = normalize_table(raw_table(rows), POLICY)
    assert len(entries) == 1
    assert not invalid
    assert not issues


@pytest.mark.parametrize(
    "raw_date,expected",
    [("2026/8/3", date(2026, 8, 3)), ("2026年8月3日", date(2026, 8, 3)), ("20260803", date(2026, 8, 3))],
)
def test_multiple_date_formats(raw_date: str, expected: date) -> None:
    entries, _, _ = normalize_table(raw_table([_valid(entry_date=raw_date)]), POLICY)
    assert entries[0].entry_date == expected


def test_bad_row_does_not_block_good_rows() -> None:
    rows = [_valid(invoice_no="INV-2099-0001"), _valid(invoice_no="INV-2099-0002", amount="abc")]
    entries, invalid, _ = normalize_table(raw_table(rows), POLICY)
    assert [entry.invoice_no for entry in entries] == ["INV-2099-0001"]
    assert len(invalid) == 1
