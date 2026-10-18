# 操作 SOP（阶段 1）

> 面向「第一次接手这个工作台」的同学。命令均在仓库根目录执行。

## 0. 准备

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
source .venv/bin/activate     # macOS / Linux
pip install -e ".[dev]"
```

## 1. 拿到两份文件

| 文件 | 从哪里来 | 必填列 |
|---|---|---|
| 应付台账 `ledger_*.csv` | 内部系统导出 | 发票号、供应商、日期、币种、金额 |
| 供应商对账单 `statement_*.xlsx` | 供应商提供 | 同上 |

列名支持中英文别名，例如「发票号 / 单据号 / invoice_no」都会被识别。

## 2. 先跑一次，只看汇总

```bash
recon run --ledger sample_data/ledger_2026_08.csv \
          --statement sample_data/statement_2026_08.xlsx \
          --out-dir out/
```

终端会打印各状态数量、双方合计与差额，并给出报告摘要（digest）。**digest 相同 = 输入与策略完全相同**，可用于确认"重跑没有变化"。

## 3. 看差异

打开 `out/recon_result.xlsx`：

1. **汇总**：先看「差额」是否为 0；
2. **差异明细**：逐条看 `matched` 之外的记录；
3. **重复项**：确认是不是重复录入；
4. **无效行**：字段不合法的行，回到源文件修正。

## 4. 处理口径

| 状态 | 含义 | 处理 |
|---|---|---|
| `matched` | 金额一致 | 无需处理 |
| `amount_mismatch` | 发票号对上但金额不同 | 联系供应商确认，以合同/收货单为准 |
| `only_in_ledger` | 台账有、对账单没有 | 确认对方是否漏开，或本期未结算 |
| `only_in_statement` | 对账单有、台账没有 | 确认是否重复开票、是否未入账 |
| `duplicate` | 同源重复发票号 | 保留第一条，其余人工确认后删除 |
| `invalid` | 字段不合法 | 修正源文件后重跑 |

> 工作台只做标记，**不自动改数、不自动平账**。

## 5. 留痕

每次运行都会生成：

- `out/recon_report.json`：机器可读的完整报告（含输入文件 SHA-256）
- `out/recon_result.xlsx`：给人看的结果

把这两个文件和原始两份输入一起归档，事后可完整复现当次结论。

## 6. 退出码

| 退出码 | 含义 |
|---|---|
| 0 | 跑完，无阻断性问题 |
| 1 | 跑完，但存在差异（仅 `--fail-on-difference` 时） |
| 2 | 命令行参数错误 |
| 3 | 输入文件不可用（缺失、表头不对、全部无效） |
