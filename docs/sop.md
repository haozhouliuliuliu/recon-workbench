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

## 5. 用 LLM 复核差异（阶段 2）

### 5.1 先干跑，确认会发什么

```bash
recon review --report out/recon_report.json --dry-run
```

只打印待复核条数与首条请求事实，不联网、不产生文件。

### 5.2 无密钥演示

```bash
recon review --report out/recon_report.json --stub
```

stub 响应是写死的，仅用于走通流程；结论不可当作审核意见。

### 5.3 真实调用

```bash
export LLM_API_BASE=https://api.example.invalid/v1
export LLM_API_KEY=sk-...          # 只放在环境变量里
export LLM_MODEL=your-model
recon review --report out/recon_report.json --threshold 0.85
```

程序不会自动读取 `.env` 文件，请用 shell/部署平台注入环境变量。

### 5.4 怎么读结论

打开 `out/review_report.json`，逐条看 `outcomes`：

| 状态 | 含义 | 你要做什么 |
|---|---|---|
| `auto_suggested` | 置信度达标，建议可直接采纳 | 仍然人工确认后再执行；系统不会自动通过 |
| `manual_review` | 置信度不足或模型要求人工 | 按 `explanation` / `recommended_action` 核对 |
| `failed` | 调用失败或输出不可用 | 看 `error_code`：鉴权/超时/限流/格式，再决定重跑或人工处理 |

终端会打印逐条一行摘要；`--quiet` 可只看汇总。

### 5.5 阈值怎么定

- 默认 `0.85`，可用 `--threshold` 或环境变量覆盖；
- 阈值越高 → 越多结论转人工，越安全也越费人；
- **换模型必须重新校准**：不同模型的置信度分布不可比。

### 5.6 常见失败

| `error_code` | 处理 |
|---|---|
| `HTTP_401` | 密钥无效或过期，检查 `LLM_API_KEY` |
| `HTTP_404` | `LLM_API_BASE` 路径不对，确认服务商要求是否带 `/v1` |
| `HTTP_429` | 限流，降低并发/缩小 `--limit` 后重跑 |
| `TIMEOUT` | 网络或模型过慢，调大 `LLM_TIMEOUT_SECONDS` |
| `INVALID_JSON` / `SCHEMA_VALIDATION_FAILED` | 模型不遵守格式；已自动修复重试，仍失败则换模型或降级人工 |

> 失败会以退出码 `1` 结束，便于定时任务/CI 感知；不会把失败静默成"通过"。

## 6. 留痕

每次运行都会生成：

- `out/recon_report.json`：机器可读的完整报告（含输入文件 SHA-256）
- `out/recon_result.xlsx`：给人看的结果
- `out/review_report.json`：LLM 复核批次（含 provider、model、阈值、逐条结论与摘要）

把这三个文件和原始两份输入一起归档，事后可完整复现当次结论。

## 7. 退出码

| 退出码 | 含义 |
|---|---|
| 0 | 跑完，无阻断性问题 |
| 1 | 跑完，但存在差异（`--fail-on-difference`）或复核存在 `failed` |
| 2 | 命令行参数错误 |
| 3 | 输入文件/配置/环境变量不可用 |
