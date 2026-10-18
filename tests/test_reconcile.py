"""匹配算法测试（纯函数，不涉及 IO）。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from recon_workbench.config import Policy
from recon_workbench.models import SourceName, Status
from recon_workbench.reconcile import reconcile

from .helpers import make_entry

POLICY = Policy()
DAY = date(2026, 8, 3)


def _ledger(**kwargs):
    return make_entry(source=SourceName.LEDGER, **kwargs)


def _statement(**kwargs):
    kwargs.setdefault("row_number", 10)
    return make_entry(source=SourceName.STATEMENT, **kwargs)


def test_invoice_exact_match() -> None:
    items, issues = reconcile([_ledger(row_number=2)], [_statement(invoice="INV-2099-0001")], POLICY)
    assert len(items) == 1
    assert items[0].status is Status.MATCHED
    assert items[0].matched_by == "invoice_no"
    assert not issues


def test_amount_mismatch_keeps_delta() -> None:
    items, _ = reconcile(
        [_ledger(row_number=2, amount="9300.00")],
        [_statement(amount="9030.00")],
        POLICY,
    )
    assert items[0].status is Status.AMOUNT_MISMATCH
    assert items[0].delta == Decimal("270.00")
    assert "270.00" in items[0].reason


def test_tolerance_can_absorb_rounding_difference() -> None:
    tolerant = Policy(amount_tolerance=Decimal("0.01"))
    items, _ = reconcile(
        [_ledger(row_number=2, amount="100.00")],
        [_statement(amount="100.01")],
        tolerant,
    )
    assert items[0].status is Status.MATCHED


def test_match_by_date_and_amount_when_invoice_changed() -> None:
    items, _ = reconcile(
        [_ledger(row_number=2, invoice="INV-2099-0009", day=DAY, amount="4200.00")],
        [_statement(invoice="INV-2099-0109", day=DAY, amount="4200.00")],
        POLICY,
    )
    assert items[0].status is Status.MATCHED
    assert items[0].matched_by == "date_amount"


def test_reissue_invoice_does_not_cross_supplier() -> None:
    items, _ = reconcile(
        [_ledger(row_number=2, invoice="INV-2099-0010", counterparty="辰光金属（虚构）")],
        [_statement(invoice="INV-2099-0010", counterparty="蓝湾精密（虚构）")],
        POLICY,
    )
    assert items[0].status is Status.ONLY_IN_LEDGER
    assert items[1].status is Status.ONLY_IN_STATEMENT


def test_only_in_ledger_and_statement() -> None:
    items, _ = reconcile([_ledger(row_number=2, invoice="INV-2099-0011")], [], POLICY)
    assert items[0].status is Status.ONLY_IN_LEDGER

    items, _ = reconcile([], [_statement(invoice="INV-2099-0012")], POLICY)
    assert items[0].status is Status.ONLY_IN_STATEMENT


def test_ambiguous_candidates_are_not_matched() -> None:
    ledger = [
        _ledger(row_number=2, invoice="INV-2099-0020", day=DAY, amount="100.00"),
        _ledger(row_number=3, invoice="INV-2099-0021", day=DAY, amount="100.00"),
    ]
    statement = [_statement(invoice="INV-2099-0100", day=DAY, amount="100.00")]

    items, issues = reconcile(ledger, statement, POLICY)

    assert all(item.status is not Status.MATCHED for item in items)
    assert {issue.code for issue in issues} == {"AMBIGUOUS_CANDIDATES"}
    assert len([item for item in items if item.status is Status.ONLY_IN_LEDGER]) == 2
    assert items[-1].reason == "候选不唯一，未自动匹配"


def test_currency_mismatch_is_reported() -> None:
    items, issues = reconcile(
        [_ledger(row_number=2, currency="CNY")],
        [_statement(currency="USD")],
        POLICY,
    )
    assert items[0].status is Status.AMOUNT_MISMATCH
    assert items[0].delta is None
    assert issues[0].code == "CURRENCY_MISMATCH"


def test_items_are_sorted_with_differences_first() -> None:
    ledger = [
        _ledger(row_number=2, invoice="INV-2099-0030"),
        _ledger(row_number=3, invoice="INV-2099-0031", amount="50.00"),
    ]
    statement = [_statement(invoice="INV-2099-0030", amount="40.00")]
    items, _ = reconcile(ledger, statement, POLICY)
    assert items[0].status is Status.AMOUNT_MISMATCH
    assert items[-1].status is Status.ONLY_IN_LEDGER
