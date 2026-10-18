"""生成 100% 虚构的示例数据。

固定行、固定金额、固定顺序，重复运行得到同样的业务内容（Excel 文件的 zip 时间戳除外）。

用法：
    python scripts/make_sample_data.py
"""

from __future__ import annotations

import csv
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

ROOT = Path(__file__).resolve().parents[1]
SAMPLE_DIR = ROOT / "sample_data"

LEDGER_HEADERS = ["发票号", "供应商", "日期", "币种", "金额", "单据类型", "备注"]

# 发票号、公司名、金额全部为虚构；公司名统一带「（虚构）」，避免与真实企业混淆。
LEDGER_ROWS = [
    ("INV-2099-0001", "辰光金属（虚构）", "2026-08-03", "CNY", "12800.00", "送货单", "7 月结余转 8 月"),
    ("INV-2099-0002", "辰光金属（虚构）", "2026-08-05", "CNY", "6420.50", "送货单", ""),
    ("INV-2099-0003", "蓝湾精密（虚构）", "2026-08-07", "CNY", "3180.00", "送货单", ""),
    ("INV-2099-0004", "辰光金属（虚构）", "2026-08-11", "CNY", "24500.00", "发票", "项目 D21-C16"),
    ("INV-2099-0005", "蓝湾精密（虚构）", "2026-08-14", "CNY", "980.00", "送货单", ""),
    ("INV-2099-0006", "辰光金属（虚构）", "2026-08-18", "CNY", "15750.25", "发票", ""),
    ("INV-2099-0007", "蓝湾精密（虚构）", "2026-08-21", "CNY", "5600.00", "送货单", ""),
    ("INV-2099-0008", "辰光金属（虚构）", "2026-08-22", "CNY", "9300.00", "发票", "含运费 270.00"),
    ("INV-2099-0009", "蓝湾精密（虚构）", "2026-08-25", "CNY", "4200.00", "送货单", ""),
    ("INV-2099-0010", "辰光金属（虚构）", "2026-08-27", "CNY", "2750.00", "送货单", "待对方确认"),
    # 重复录入：与第 4 行发票号相同
    ("INV-2099-0004", "辰光金属（虚构）", "2026-08-11", "CNY", "24500.00", "发票", "重复录入"),
    # 非法日期：故意让校验层拦截
    ("INV-2099-0012", "辰光金属（虚构）", "2026-13-40", "CNY", "1000.00", "送货单", "日期填错"),
    # 非法金额：故意让校验层拦截
    ("INV-2099-0013", "蓝湾精密（虚构）", "2026-08-28", "CNY", "abc", "送货单", "金额写错"),
]

STATEMENT_ROWS = [
    ("INV-2099-0001", "辰光金属（虚构）", "2026-08-03", "CNY", "12800.00", ""),
    ("INV-2099-0002", "辰光金属（虚构）", "2026-08-05", "CNY", "6420.50", ""),
    ("INV-2099-0003", "蓝湾精密（虚构）", "2026-08-07", "CNY", "3180.00", ""),
    ("INV-2099-0004", "辰光金属（虚构）", "2026-08-11", "CNY", "24500.00", "项目 D21-C16"),
    ("INV-2099-0005", "蓝湾精密（虚构）", "2026-08-14", "CNY", "980.00", ""),
    ("INV-2099-0006", "辰光金属（虚构）", "2026-08-18", "CNY", "15750.25", ""),
    ("INV-2099-0007", "蓝湾精密（虚构）", "2026-08-21", "CNY", "5600.00", ""),
    # 金额与台账不一致：台账 9300.00
    ("INV-2099-0008", "辰光金属（虚构）", "2026-08-22", "CNY", "9030.00", "按 9030 开票"),
    # 换过发票号，但日期与金额一致 → 走第二轮匹配
    ("INV-2099-0109", "蓝湾精密（虚构）", "2026-08-25", "CNY", "4200.00", "原票作废重开"),
    # 只在台账出现
    ("INV-2099-0011", "蓝湾精密（虚构）", "2026-08-29", "CNY", "1860.00", ""),
    ("INV-2099-0012", "辰光金属（虚构）", "2026-08-30", "CNY", "7400.00", ""),
    # 对账单侧重复
    ("INV-2099-0002", "辰光金属（虚构）", "2026-08-05", "CNY", "6420.50", "重复行"),
]


def write_ledger_csv(path: Path) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\r\n")
        writer.writerow(LEDGER_HEADERS)
        writer.writerows(LEDGER_ROWS)


def write_statement_xlsx(path: Path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "2026-08 对账单"

    sheet.merge_cells("A1:G1")
    title = sheet["A1"]
    title.value = "辰光金属（虚构） · 2026 年 8 月 对账单"
    title.font = Font(name="微软雅黑", size=14, bold=True)
    title.alignment = Alignment(horizontal="center", vertical="center")
    sheet.row_dimensions[1].height = 28

    sheet.merge_cells("A2:G2")
    subtitle = sheet["A2"]
    subtitle.value = (
        "供应商：辰光金属（虚构）　对账期间：2026-08-01 至 2026-08-31　金额单位：元　（本文件全部数据为虚构）"
    )
    subtitle.font = Font(name="微软雅黑", size=9, color="666666")

    headers = ["序号", "发票号", "供应商", "日期", "币种", "金额", "备注"]
    header_fill = PatternFill("solid", fgColor="DDEBF7")
    thin = Side(style="thin", color="B4C6E7")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    for column, header in enumerate(headers, start=1):
        cell = sheet.cell(row=4, column=column, value=header)
        cell.font = Font(name="微软雅黑", size=10, bold=True)
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center")
        cell.border = border

    for index, row in enumerate(STATEMENT_ROWS, start=1):
        values = [index, row[0], row[1], row[2], row[3], float(row[4]), row[5]]
        for column, value in enumerate(values, start=1):
            cell = sheet.cell(row=4 + index, column=column, value=value)
            cell.font = Font(name="微软雅黑", size=10)
            cell.border = border
            if column == 6:
                cell.number_format = "#,##0.00"
                cell.alignment = Alignment(horizontal="right")

    widths = [6, 20, 22, 14, 8, 14, 24]
    for column, width in enumerate(widths, start=1):
        sheet.column_dimensions[get_column_letter(column)].width = width
    sheet.freeze_panes = "A5"

    workbook.save(path)


def main() -> None:
    SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
    write_ledger_csv(SAMPLE_DIR / "ledger_2026_08.csv")
    write_statement_xlsx(SAMPLE_DIR / "statement_2026_08.xlsx")
    print(f"已生成：{SAMPLE_DIR / 'ledger_2026_08.csv'}")
    print(f"已生成：{SAMPLE_DIR / 'statement_2026_08.xlsx'}")


if __name__ == "__main__":
    main()
