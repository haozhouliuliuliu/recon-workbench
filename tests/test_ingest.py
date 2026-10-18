"""输入读取层测试：编码、表头探测、工作表选择、错误信息。"""

from __future__ import annotations

from pathlib import Path

import pytest

from recon_workbench.errors import InputError
from recon_workbench.ingest import read_table
from recon_workbench.models import SourceName

from .helpers import write_csv, write_xlsx


def test_reads_utf8_bom_csv(tmp_path: Path) -> None:
    path = write_csv(tmp_path / "ledger.csv", [["INV-2099-0001", "辰光金属（虚构）", "2026-08-03", "CNY", "100.00"]])
    table = read_table(path, SourceName.LEDGER)
    assert table.filename == "ledger.csv"
    assert len(table.rows) == 1
    assert table.rows[0].values["invoice_no"] == "INV-2099-0001"
    assert table.header_row == 1


def test_reads_gbk_csv(tmp_path: Path) -> None:
    path = write_csv(
        tmp_path / "ledger_gbk.csv",
        [["INV-2099-0002", "蓝湾精密（虚构）", "2026-08-04", "CNY", "200.00"]],
        encoding="gb18030",
    )
    table = read_table(path, SourceName.LEDGER)
    assert table.rows[0].values["counterparty"] == "蓝湾精密（虚构）"


def test_skips_title_rows_in_xlsx(tmp_path: Path) -> None:
    path = write_xlsx(
        tmp_path / "statement.xlsx",
        [["INV-2099-0001", "辰光金属（虚构）", "2026-08-03", "CNY", 100.0]],
        title="辰光金属（虚构） 2026 年 8 月对账单",
    )
    table = read_table(path, SourceName.STATEMENT)
    assert table.header_row == 3
    assert table.rows[0].row_number == 4
    assert table.rows[0].values["amount"] == "100"


def test_selects_named_sheet(tmp_path: Path) -> None:
    path = write_xlsx(
        tmp_path / "statement.xlsx",
        [["INV-2099-0003", "辰光金属（虚构）", "2026-08-05", "CNY", 300.0]],
        sheet="明细",
    )
    table = read_table(path, SourceName.STATEMENT, sheet="明细")
    assert len(table.rows) == 1


def test_missing_sheet_lists_available_sheets(tmp_path: Path) -> None:
    path = write_xlsx(tmp_path / "statement.xlsx", [], sheet="Sheet1")
    with pytest.raises(InputError) as excinfo:
        read_table(path, SourceName.STATEMENT, sheet="不存在")
    assert "Sheet1" in str(excinfo.value)


def test_missing_required_column_reports_names(tmp_path: Path) -> None:
    path = write_csv(tmp_path / "bad.csv", [["x"]], headers=["发票号", "供应商", "日期"])
    with pytest.raises(InputError) as excinfo:
        read_table(path, SourceName.LEDGER)
    message = str(excinfo.value)
    assert "currency" in message and "amount" in message


def test_unsupported_suffix(tmp_path: Path) -> None:
    path = tmp_path / "data.txt"
    path.write_text("hello", encoding="utf-8")
    with pytest.raises(InputError):
        read_table(path, SourceName.LEDGER)


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(InputError):
        read_table(tmp_path / "nope.csv", SourceName.LEDGER)
