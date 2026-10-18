"""重复单据检测。

两类问题分开处理，避免"看起来像重复"就直接删数据：

- exact：同一来源内发票号完全重复。保留首条，后续记录不参与匹配，计入 duplicates；
- suspected：供应商 + 日期 + 金额相同但发票号不同。仅告警，仍参与匹配，交人工判断是否拆单/重开。
"""

from __future__ import annotations

from collections import defaultdict

from .models import DuplicateFinding, Entry, Issue, Severity, money_str


def find_duplicates(
    entries: list[Entry],
) -> tuple[list[Entry], list[DuplicateFinding], list[Issue]]:
    findings: list[DuplicateFinding] = []
    issues: list[Issue] = []
    kept: list[Entry] = []

    seen_invoice: dict[tuple[str, str], Entry] = {}
    for entry in sorted(entries, key=lambda e: (e.source.value, e.row_number)):
        key = (entry.source.value, entry.invoice_no)
        first = seen_invoice.get(key)
        if first is None:
            seen_invoice[key] = entry
            kept.append(entry)
            continue
        findings.append(
            DuplicateFinding(
                source=entry.source,
                kind="exact",
                entry=entry,
                related_row_number=first.row_number,
                reason=(
                    f"发票号 {entry.invoice_no} 与第 {first.row_number} 行重复，"
                    f"金额 {money_str(entry.amount)} / {money_str(first.amount)}"
                ),
            )
        )
        issues.append(
            Issue(
                code="DUPLICATE_ENTRY",
                severity=Severity.ERROR,
                source=entry.source,
                row_number=entry.row_number,
                field="invoice_no",
                message=f"发票号 {entry.invoice_no} 重复（首次出现在第 {first.row_number} 行），已排除在匹配之外",
            )
        )

    grouped: dict[tuple[str, str, str, str], list[Entry]] = defaultdict(list)
    for entry in kept:
        grouped[
            (entry.source.value, entry.counterparty.casefold(), entry.entry_date.isoformat(), money_str(entry.amount))
        ].append(entry)

    for group in grouped.values():
        if len(group) < 2:
            continue
        distinct_invoices = {entry.invoice_no for entry in group}
        if len(distinct_invoices) < 2:
            continue
        first = min(group, key=lambda e: e.row_number)
        for entry in sorted(group, key=lambda e: e.row_number):
            if entry.row_number == first.row_number:
                continue
            findings.append(
                DuplicateFinding(
                    source=entry.source,
                    kind="suspected",
                    entry=entry,
                    related_row_number=first.row_number,
                    reason=(
                        f"同一供应商 {entry.counterparty} 在 {entry.entry_date.isoformat()} "
                        f"金额同为 {money_str(entry.amount)}，但发票号不同，疑似拆单或重复入账"
                    ),
                )
            )
            issues.append(
                Issue(
                    code="SUSPECTED_DUPLICATE",
                    severity=Severity.WARNING,
                    source=entry.source,
                    row_number=entry.row_number,
                    field="invoice_no",
                    message=f"与第 {first.row_number} 行供应商/日期/金额相同但发票号不同，请人工确认",
                )
            )

    findings.sort(key=lambda f: (f.source.value, f.entry.row_number, f.kind))
    return sorted(kept, key=lambda e: e.row_number), findings, issues
