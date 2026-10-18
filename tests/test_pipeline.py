"""端到端：用 sample_data 跑完整流程，校验计数、合计、不变式与确定性。"""

from __future__ import annotations

import hashlib
from decimal import Decimal
from pathlib import Path

from recon_workbench.models import Status
from recon_workbench.pipeline import run_reconciliation

SAMPLE_DIR = Path(__file__).resolve().parents[1] / "sample_data"
LEDGER = SAMPLE_DIR / "ledger_2026_08.csv"
STATEMENT = SAMPLE_DIR / "statement_2026_08.xlsx"


def run_sample():
    return run_reconciliation(LEDGER, STATEMENT)


def test_sample_counts() -> None:
    counts = run_sample().report.counts
    assert counts.ledger_rows == 13
    assert counts.statement_rows == 12
    assert counts.ledger_accepted == 10
    assert counts.statement_accepted == 11
    assert counts.matched == 8
    assert counts.amount_mismatch == 1
    assert counts.only_in_ledger == 1
    assert counts.only_in_statement == 2
    assert counts.duplicates == 2
    assert counts.invalid_rows == 2


def test_sample_totals() -> None:
    totals = run_sample().report.totals
    assert len(totals) == 1
    assert totals[0].currency == "CNY"
    assert totals[0].ledger_total == Decimal("85480.75")
    assert totals[0].statement_total == Decimal("91720.75")
    assert totals[0].difference == Decimal("-6240.00")


def test_accounting_identity_holds() -> None:
    report = run_sample().report
    delta_sum = sum((item.delta for item in report.items if item.delta is not None), Decimal(0))
    only_ledger = sum((item.ledger.amount for item in report.items if item.status is Status.ONLY_IN_LEDGER), Decimal(0))
    only_statement = sum(
        (item.statement.amount for item in report.items if item.status is Status.ONLY_IN_STATEMENT), Decimal(0)
    )
    for totals in report.totals:
        assert totals.difference == delta_sum + only_ledger - only_statement


def test_duplicate_rows_do_not_enter_matching() -> None:
    report = run_sample().report
    matched_invoices = {item.ledger.invoice_no for item in report.items if item.ledger}
    assert "INV-2099-0004" in matched_invoices
    duplicate_rows = {finding.entry.row_number for finding in report.duplicates}
    assert duplicate_rows == {12, 16}


def test_invalid_rows_carry_codes() -> None:
    report = run_sample().report
    assert [row.row_number for row in report.invalid_rows] == [13, 14]
    assert report.invalid_rows[0].issue_codes == ("INVALID_DATE",)
    assert report.invalid_rows[1].issue_codes == ("INVALID_AMOUNT",)


def test_same_input_produces_identical_bytes() -> None:
    first = run_reconciliation(LEDGER, STATEMENT)
    second = run_reconciliation(LEDGER, STATEMENT)
    assert first.canonical_json == second.canonical_json
    assert first.digest == second.digest
    assert first.digest == hashlib.sha256(first.canonical_json).hexdigest()


def test_report_has_no_volatile_fields() -> None:
    payload = run_sample().canonical_json.decode("utf-8")
    assert "generated_at" not in payload
    assert str(SAMPLE_DIR) not in payload
    assert "ledger_2026_08.csv" in payload


def test_status_counts_match_items() -> None:
    report = run_sample().report
    matched = sum(1 for item in report.items if item.status is Status.MATCHED)
    assert matched == report.counts.matched
    assert len(report.items) == (
        report.counts.matched
        + report.counts.amount_mismatch
        + report.counts.only_in_ledger
        + report.counts.only_in_statement
    )


def test_canonical_json_is_compact() -> None:
    payload = run_sample().canonical_json.decode("utf-8")
    assert "\n" not in payload
    assert '": ' not in payload and '", "' not in payload
    assert payload.startswith("{") and payload.endswith("}")
