"""编排：读入两份文件 → 校验 → 去重 → 匹配 → 报告。

同输入 + 同策略 ⇒ 规范化 JSON 字节级一致；报告里刻意不出现绝对路径与运行时间。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from . import __version__
from .canonical import canonical_dumps
from .config import Policy
from .dedupe import find_duplicates
from .ingest import RawTable, read_table
from .models import (
    CurrencyTotals,
    Issue,
    ReconItem,
    Report,
    SourceInfo,
    SourceName,
    Status,
    StatusCounts,
)
from .normalize import normalize_table
from .reconcile import reconcile


@dataclass(frozen=True, slots=True)
class RunResult:
    report: Report
    canonical_json: bytes

    @property
    def digest(self) -> str:
        return self.report.digest


def _sum_by_currency(entries) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = {}
    for entry in entries:
        totals[entry.currency] = totals.get(entry.currency, Decimal(0)) + entry.amount
    return totals


def compute_totals(ledger, statement) -> tuple[CurrencyTotals, ...]:
    ledger_totals = _sum_by_currency(ledger)
    statement_totals = _sum_by_currency(statement)
    currencies = sorted(set(ledger_totals) | set(statement_totals))
    return tuple(
        CurrencyTotals(
            currency=currency,
            ledger_total=ledger_totals.get(currency, Decimal(0)),
            statement_total=statement_totals.get(currency, Decimal(0)),
            difference=ledger_totals.get(currency, Decimal(0)) - statement_totals.get(currency, Decimal(0)),
        )
        for currency in currencies
    )


def _count_status(items: list[ReconItem]) -> dict[Status, int]:
    counts = {status: 0 for status in Status}
    for item in items:
        counts[item.status] += 1
    return counts


def _source_info(source: SourceName, table: RawTable, accepted: int, invalid: int, duplicates: int) -> SourceInfo:
    return SourceInfo(
        source=source,
        filename=table.filename,
        sha256=table.sha256,
        total_rows=accepted + invalid + duplicates,
        accepted_rows=accepted,
        rejected_rows=invalid + duplicates,
    )


def _sort_issues(issues: list[Issue]) -> list[Issue]:
    source_order = {SourceName.LEDGER: 0, SourceName.STATEMENT: 1}
    severity_order = {"error": 0, "warning": 1}
    return sorted(
        issues,
        key=lambda issue: (
            source_order[issue.source],
            issue.row_number if issue.row_number is not None else 10**9,
            severity_order[issue.severity.value],
            issue.code,
        ),
    )


def canonical_json(report: Report) -> bytes:
    """规范化 JSON：键排序、无空格、UTF-8、金额为字符串。"""

    return canonical_dumps(report.model_dump(mode="json", exclude={"digest"}))


def run_reconciliation(
    ledger_path: Path | str,
    statement_path: Path | str,
    policy: Policy | None = None,
    *,
    ledger_sheet: str | None = None,
    statement_sheet: str | None = None,
) -> RunResult:
    """跑一次完整对账，返回报告与规范化 JSON。"""

    active_policy = policy or Policy()

    ledger_table = read_table(ledger_path, SourceName.LEDGER, sheet=ledger_sheet)
    statement_table = read_table(statement_path, SourceName.STATEMENT, sheet=statement_sheet)

    ledger_entries, ledger_invalid, ledger_issues = normalize_table(ledger_table, active_policy)
    statement_entries, statement_invalid, statement_issues = normalize_table(statement_table, active_policy)

    ledger_kept, ledger_dups, ledger_dup_issues = find_duplicates(ledger_entries)
    statement_kept, statement_dups, statement_dup_issues = find_duplicates(statement_entries)

    items, match_issues = reconcile(ledger_kept, statement_kept, active_policy)

    issues = _sort_issues(ledger_issues + statement_issues + ledger_dup_issues + statement_dup_issues + match_issues)
    status_counts = _count_status(items)
    duplicates = ledger_dups + statement_dups

    counts = StatusCounts(
        ledger_rows=len(ledger_entries) + len(ledger_invalid),
        statement_rows=len(statement_entries) + len(statement_invalid),
        ledger_accepted=len(ledger_kept),
        statement_accepted=len(statement_kept),
        matched=status_counts[Status.MATCHED],
        amount_mismatch=status_counts[Status.AMOUNT_MISMATCH],
        only_in_ledger=status_counts[Status.ONLY_IN_LEDGER],
        only_in_statement=status_counts[Status.ONLY_IN_STATEMENT],
        duplicates=len(duplicates),
        invalid_rows=len(ledger_invalid) + len(statement_invalid),
        issues_error=sum(1 for issue in issues if issue.severity.value == "error"),
        issues_warning=sum(1 for issue in issues if issue.severity.value == "warning"),
    )

    report = Report(
        tool_version=__version__,
        policy=active_policy.snapshot(),
        sources=(
            _source_info(SourceName.LEDGER, ledger_table, len(ledger_kept), len(ledger_invalid), len(ledger_dups)),
            _source_info(
                SourceName.STATEMENT,
                statement_table,
                len(statement_kept),
                len(statement_invalid),
                len(statement_dups),
            ),
        ),
        counts=counts,
        totals=compute_totals(ledger_kept, statement_kept),
        items=tuple(items),
        duplicates=tuple(duplicates),
        invalid_rows=tuple(ledger_invalid + statement_invalid),
        issues=tuple(issues),
    )

    payload = canonical_json(report)
    digest = hashlib.sha256(payload).hexdigest()
    report = report.model_copy(update={"digest": digest})
    return RunResult(report=report, canonical_json=canonical_json(report))


def summarize(report: Report) -> str:
    """给终端看的中文摘要。"""

    counts = report.counts
    header = (
        f"台账 {counts.ledger_rows} 行（有效 {counts.ledger_accepted}） / "
        f"对账单 {counts.statement_rows} 行（有效 {counts.statement_accepted}）"
    )
    lines = [
        header,
        f"  已匹配        {counts.matched}",
        f"  金额不一致    {counts.amount_mismatch}",
        f"  仅台账有      {counts.only_in_ledger}",
        f"  仅对账单有    {counts.only_in_statement}",
        f"  重复单据      {counts.duplicates}",
        f"  无效行        {counts.invalid_rows}",
    ]
    for totals in report.totals:
        ledger_total = _format_money(totals.ledger_total)
        statement_total = _format_money(totals.statement_total)
        difference = _format_money(totals.difference)
        lines.append(f"合计 {totals.currency}：台账 {ledger_total} / 对账单 {statement_total} / 差额 {difference}")
    lines.append(f"问题：错误 {counts.issues_error} / 告警 {counts.issues_warning}")
    lines.append(f"报告摘要：{report.digest}")
    return "\n".join(lines)


def _format_money(value: Decimal) -> str:
    return f"{value:,.2f}"
