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

## 6. 任务化模式（阶段 3）

阶段 1/2 是"一次性命令"，每次都要自己记住输出在哪。阶段 3 把对账、复核、人工决策串成任务：

### 6.1 建任务

```bash
recon workflow run --ledger 台账.csv --statement 对账单.xlsx --stub
```

- 默认数据库 `data/recon.db`，产物 `data/jobs/<job_id>/`；
- 相同内容重复执行会复用同一任务，不会重跑已成功的阶段；
- `--no-review` 只跑对账；真实复核去掉 `--stub`，先配好 LLM 环境变量。

输出会直接告诉你：阶段状态、对账结果、建议分布、待人工条数。

### 6.2 看任务

```bash
recon workflow status J584bb23c789d9e42
recon workflow status J584bb23c789d9e42 --json
recon workflow list
```

### 6.3 记录人工结论

```bash
recon workflow decide J584bb23c789d9e42 I080065789730 \
      --decision accepted --by 张会计 --note "270 元为运费，已核实"
```

- `accepted` 认可 / `rejected` 驳回 / `needs_info` 退回补充；
- 人工结论与 LLM 建议分开存储，**不会覆盖模型原话**；
- 只有全部差异都有结论，任务才会变成 `completed`。

### 6.4 中断与恢复

进程被杀、断电、机器重启后：

1. `workflow status` 会显示停在哪个阶段（`running` 或 `failed`）以及最后一次错误；
2. `workflow resume <job_id>` 继续：已成功的阶段不重跑，复核只补没做完的条目，不重复烧 token；
3. 如果提示"源文件已变化/不存在"，说明输入被动过，需恢复原文件或重建任务。

### 6.5 重跑

```bash
recon workflow run ... --force
```

`--force` 会清空该任务的建议与人工决策后重跑，属于破坏性操作，先确认。

## 7. 留痕

每次运行都会生成：

- `out/recon_report.json`：机器可读的完整报告（含输入文件 SHA-256）
- `out/recon_result.xlsx`：给人看的结果
- `out/review_report.json`：LLM 复核批次（含 provider、model、阈值、逐条结论与摘要）

任务模式额外保留：

- `data/recon.db`：任务、阶段、明细、建议、人工决策、事件
- `data/jobs/<job_id>/recon_report.json`、`recon_result.xlsx`、`review_report.json`

把输入文件、`data/` 目录一起归档，事后可完整复现当次结论与人工动作。

## 8. 退出码

| 退出码 | 含义 |
|---|---|
| 0 | 跑完，无阻断性问题（任务模式下含"待人工"） |
| 1 | 跑完，但存在差异（`--fail-on-difference`）、复核存在 `failed`，或任务失败 |
| 2 | 命令行参数错误 |
| 3 | 输入文件/配置/环境变量/任务号不可用 |
