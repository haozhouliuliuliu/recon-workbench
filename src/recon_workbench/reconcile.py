"""台账 ↔ 对账单匹配。

匹配顺序刻意保守，宁可留给人工，也不做"猜"：

1. 先按「供应商 + 发票号」精确匹配 —— 同一张票谈金额；
2. 剩下的按「供应商 + 日期 + 金额」唯一匹配 —— 处理对方换开票号的情况；
3. 仍匹配不上的进入单边列表；同日同金额出现多条候选时**不猜**，标记歧义交人工。

匹配层是纯函数：不读文件、不写文件，方便单测与后续状态机复用。
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from decimal import Decimal

from .config import Policy
from .models import Entry, Issue, ReconItem, Severity, Status, money_str

STATUS_ORDER = {
    Status.AMOUNT_MISMATCH: 0,
    Status.ONLY_IN_LEDGER: 1,
    Status.ONLY_IN_STATEMENT: 2,
    Status.MATCHED: 3,
}


def _item_id(status: Status, ledger: Entry | None, statement: Entry | None) -> str:
    payload = "|".join(
        [
            status.value,
            ledger.entry_id if ledger else "-",
            statement.entry_id if statement else "-",
        ]
    )
    return "I" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def _pair(ledger: Entry, statement: Entry, matched_by: str, tolerance: Decimal) -> tuple[ReconItem, list[Issue]]:
    issues: list[Issue] = []
    if ledger.currency != statement.currency:
        status = Status.AMOUNT_MISMATCH
        delta = None
        reason = f"币种不一致（台账 {ledger.currency} / 对账单 {statement.currency}），无法直接比较金额"
        issues.append(
            Issue(
                code="CURRENCY_MISMATCH",
                severity=Severity.ERROR,
                source=statement.source,
                row_number=statement.row_number,
                field="currency",
                message=reason,
            )
        )
    else:
        delta = ledger.amount - statement.amount
        if abs(delta) <= tolerance:
            status = Status.MATCHED
            reason = ""
        else:
            status = Status.AMOUNT_MISMATCH
            reason = (
                f"金额差 {money_str(delta)}（台账 {money_str(ledger.amount)} / 对账单 {money_str(statement.amount)}）"
            )

    return (
        ReconItem(
            item_id=_item_id(status, ledger, statement),
            status=status,
            ledger=ledger,
            statement=statement,
            delta=delta,
            matched_by=matched_by,
            reason=reason,
        ),
        issues,
    )


def reconcile(
    ledger_entries: list[Entry],
    statement_entries: list[Entry],
    policy: Policy,
) -> tuple[list[ReconItem], list[Issue]]:
    items: list[ReconItem] = []
    issues: list[Issue] = []

    ledger_by_invoice: dict[tuple[str, str], Entry] = {
        (e.counterparty.casefold(), e.invoice_no): e for e in ledger_entries
    }
    statement_by_invoice: dict[tuple[str, str], Entry] = {
        (e.counterparty.casefold(), e.invoice_no): e for e in statement_entries
    }

    matched_ledger: set[str] = set()
    matched_statement: set[str] = set()

    for key in sorted(set(ledger_by_invoice) & set(statement_by_invoice)):
        ledger = ledger_by_invoice[key]
        statement = statement_by_invoice[key]
        item, pair_issues = _pair(ledger, statement, "invoice_no", policy.amount_tolerance)
        items.append(item)
        issues.extend(pair_issues)
        matched_ledger.add(ledger.entry_id)
        matched_statement.add(statement.entry_id)

    # 第二轮：日期 + 金额唯一匹配
    remaining_ledger = [e for e in ledger_entries if e.entry_id not in matched_ledger]
    remaining_statement = [e for e in statement_entries if e.entry_id not in matched_statement]

    ledger_buckets: dict[tuple[str, str, str], list[Entry]] = defaultdict(list)
    statement_buckets: dict[tuple[str, str, str], list[Entry]] = defaultdict(list)
    for entry in remaining_ledger:
        ledger_buckets[(entry.counterparty.casefold(), entry.entry_date.isoformat(), money_str(entry.amount))].append(
            entry
        )
    for entry in remaining_statement:
        statement_buckets[
            (entry.counterparty.casefold(), entry.entry_date.isoformat(), money_str(entry.amount))
        ].append(entry)

    ambiguous: set[str] = set()
    for key in sorted(set(ledger_buckets) & set(statement_buckets)):
        ledger_group = sorted(ledger_buckets[key], key=lambda e: e.row_number)
        statement_group = sorted(statement_buckets[key], key=lambda e: e.row_number)
        if len(ledger_group) == 1 and len(statement_group) == 1:
            ledger = ledger_group[0]
            statement = statement_group[0]
            item, pair_issues = _pair(ledger, statement, "date_amount", policy.amount_tolerance)
            items.append(item)
            issues.extend(pair_issues)
            matched_ledger.add(ledger.entry_id)
            matched_statement.add(statement.entry_id)
            continue

        for entry in ledger_group:
            ambiguous.add(entry.entry_id)
            issues.append(
                Issue(
                    code="AMBIGUOUS_CANDIDATES",
                    severity=Severity.WARNING,
                    source=entry.source,
                    row_number=entry.row_number,
                    field="invoice_no",
                    message=(
                        f"同日同金额存在 {len(ledger_group)} 条台账 / {len(statement_group)} 条对账单候选，"
                        "未自动匹配，请人工核对"
                    ),
                )
            )
        for entry in statement_group:
            ambiguous.add(entry.entry_id)
            issues.append(
                Issue(
                    code="AMBIGUOUS_CANDIDATES",
                    severity=Severity.WARNING,
                    source=entry.source,
                    row_number=entry.row_number,
                    field="invoice_no",
                    message=(
                        f"同日同金额存在 {len(ledger_group)} 条台账 / {len(statement_group)} 条对账单候选，"
                        "未自动匹配，请人工核对"
                    ),
                )
            )

    for entry in remaining_ledger:
        if entry.entry_id in matched_ledger:
            continue
        status = Status.ONLY_IN_LEDGER
        reason = "对账单中无对应记录"
        if entry.entry_id in ambiguous:
            reason = "候选不唯一，未自动匹配"
        items.append(
            ReconItem(
                item_id=_item_id(status, entry, None),
                status=status,
                ledger=entry,
                statement=None,
                delta=None,
                matched_by=None,
                reason=reason,
            )
        )

    for entry in remaining_statement:
        if entry.entry_id in matched_statement:
            continue
        status = Status.ONLY_IN_STATEMENT
        reason = "台账中无对应记录"
        if entry.entry_id in ambiguous:
            reason = "候选不唯一，未自动匹配"
        items.append(
            ReconItem(
                item_id=_item_id(status, None, entry),
                status=status,
                ledger=None,
                statement=entry,
                delta=None,
                matched_by=None,
                reason=reason,
            )
        )

    items.sort(
        key=lambda item: (
            STATUS_ORDER[item.status],
            (item.ledger or item.statement).counterparty.casefold(),
            (item.ledger or item.statement).entry_date.isoformat(),
            (item.ledger or item.statement).invoice_no,
            item.item_id,
        )
    )
    return items, issues
