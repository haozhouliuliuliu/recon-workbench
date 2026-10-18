"""重复检测测试。"""

from __future__ import annotations

from datetime import date

from recon_workbench.dedupe import find_duplicates
from recon_workbench.models import Severity

from .helpers import make_entry


def test_exact_duplicate_keeps_first_and_reports_error() -> None:
    first = make_entry(row_number=2, invoice="INV-2099-0004")
    duplicate = make_entry(row_number=12, invoice="INV-2099-0004")

    kept, findings, issues = find_duplicates([first, duplicate])

    assert [entry.row_number for entry in kept] == [2]
    assert len(findings) == 1
    assert findings[0].kind == "exact"
    assert findings[0].related_row_number == 2
    assert findings[0].entry.row_number == 12
    assert issues[0].code == "DUPLICATE_ENTRY"
    assert issues[0].severity is Severity.ERROR


def test_suspected_duplicate_is_kept_with_warning() -> None:
    a = make_entry(row_number=2, invoice="INV-2099-0005", amount="120.00", day=date(2026, 8, 5))
    b = make_entry(row_number=3, invoice="INV-2099-0006", amount="120.00", day=date(2026, 8, 5))

    kept, findings, issues = find_duplicates([a, b])

    assert len(kept) == 2
    assert findings[0].kind == "suspected"
    assert findings[0].related_row_number == 2
    assert issues[0].code == "SUSPECTED_DUPLICATE"
    assert issues[0].severity is Severity.WARNING


def test_same_invoice_in_different_sources_is_not_a_duplicate() -> None:
    from recon_workbench.models import SourceName

    ledger = make_entry(row_number=2, source=SourceName.LEDGER, invoice="INV-2099-0007")
    statement = make_entry(row_number=5, source=SourceName.STATEMENT, invoice="INV-2099-0007")

    kept, findings, issues = find_duplicates([ledger, statement])

    assert len(kept) == 2
    assert not findings
    assert not issues


def test_three_identical_rows_report_two_duplicates() -> None:
    rows = [make_entry(row_number=row, invoice="INV-2099-0008") for row in (2, 5, 9)]
    kept, findings, _ = find_duplicates(rows)
    assert len(kept) == 1
    assert len(findings) == 2
    assert {finding.related_row_number for finding in findings} == {2}
