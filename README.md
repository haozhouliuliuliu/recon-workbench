# 对账工作台 · Recon Workbench

> 面向企业财务场景的「对账 + 单据审核」工作台：确定性数据引擎负责把账对平，LLM 只负责需要判断的部分，所有结论都可追溯、可复核。

[![CI](https://github.com/haozhouliuliuliu/recon-workbench/actions/workflows/ci.yml/badge.svg)](https://github.com/haozhouliuliuliu/recon-workbench/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![数据](https://img.shields.io/badge/%E6%95%B0%E6%8D%AE-100%25%20%E8%99%9A%E6%9E%84-orange.svg)](#数据声明)

## 这是什么

一个**公开练手项目**，模拟企业财务每月末的对账流程：

1. 财务从内部系统导出「应付台账」，从供应商处拿到「对账单」；
2. 工作台导入两份文件，做字段校验、金额核对、重复单据检测；
3. 对不上的部分交给 LLM 结合规则做辅助审核（阶段 2），低置信度一律转人工；
4. 全流程落库、可中断恢复（阶段 3），并通过 HTTP 接口对外提供上传/查询/下载（阶段 4）；
5. 结果导出为可直接交付的 Excel。

它**不接真实银行、不接真实 ERP、不执行任何真实资金操作**。所有示例数据、单据号、公司名、金额均为虚构。

## 非目标

刻意**不做**以下内容，避免把练手项目做成"演示 PPT"：

- ❌ RAG / 向量库 / 知识库检索
- ❌ 多 Agent 编排、Agent 之间互相调用
- ❌ 本地大模型部署
- ❌ 真实资金操作、真实银行/ERP 对接、真实发票查验

## 五阶段路线图

| 阶段 | 内容 | 状态 |
|---|---|---|
| 1 | 确定性数据处理引擎：导入、字段校验、金额核对、重复检测、Excel 导出 | ✅ v0.1.0 |
| 2 | LLM 审核模块：结构化输出 + Pydantic 校验、超时/坏 JSON/缺字段处理、低置信度转人工 | ✅ v0.2.0 |
| 3 | 完整工作流：SQLite 状态机、中断可定位、幂等重跑 | ✅ v0.3.0 |
| 4 | FastAPI 接口：上传、查状态、下载结果、任务列表 | ⏳ 未开始 |
| 5 | 测试 / 日志 / 配置管理 / Docker Compose / 中文文档 | ⏳ 未开始 |

## 快速开始

```bash
git clone https://github.com/haozhouliuliuliu/recon-workbench.git
cd recon-workbench

python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"

# 用内置虚构样例跑一遍对账
recon run --ledger sample_data/ledger_2026_08.csv \
          --statement sample_data/statement_2026_08.xlsx \
          --out-dir out/

# 不需要 key：用本地假响应演示 LLM 审核流程
recon review --report out/recon_report.json --stub

# 任务化运行（阶段 3）：落库、可恢复、人工决策
recon workflow run --ledger sample_data/ledger_2026_08.csv \
                   --statement sample_data/statement_2026_08.xlsx \
                   --stub

# 跑测试
pytest -q
```

终端输出（同输入重复运行，摘要 digest 完全一致）：

```text
台账 13 行（有效 10） / 对账单 12 行（有效 11）
  已匹配        8
  金额不一致    1
  仅台账有      1
  仅对账单有    2
  重复单据      2
  无效行        2
合计 CNY：台账 85,480.75 / 对账单 91,720.75 / 差额 -6,240.00
问题：错误 4 / 告警 0
报告摘要：2dfed14a…
```

产物：

- `out/recon_report.json`：规范化报告，同输入重复运行**字节级一致**
- `out/recon_result.xlsx`：中文工作表（汇总 / 全部明细 / 差异明细 / 重复项 / 无效行 / 问题清单 / 运行信息）

## 阶段 1 能力清单

**输入**

- CSV（自动识别 UTF-8 / UTF-8 BOM / GBK）与 XLSX（可选工作表名）
- 表头支持中英文别名，允许文件开头有标题行、说明行
- 缺少必需列时直接拒绝，并列出缺了哪几列

**字段校验**（逐行记录问题，不因一行坏数据中断整批）

| 代码 | 含义 |
|---|---|
| `MISSING_REQUIRED_FIELD` | 发票号/供应商/日期/币种/金额为空 |
| `INVALID_DATE` | 日期无法解析（支持 `2026-08-03`、`2026/8/3`、`2026年8月3日` 等） |
| `INVALID_AMOUNT` | 金额无法解析 |
| `AMOUNT_NEGATIVE` | 金额为负 |
| `UNKNOWN_CURRENCY` | 币种不在白名单 |
| `INVALID_INVOICE_NO` | 发票号不符合规则 |
| `AMOUNT_ROUNDED` | 小数位超限，已四舍五入（告警） |

**重复检测**

- `exact`：同源内发票号完全相同 → 保留首条，其余排除并报错
- `suspected`：同供应商 + 同日期 + 同金额但发票号不同 → 仅告警，仍参与匹配

**匹配规则**（保守优先，不猜）

1. 「供应商 + 发票号」精确匹配 → 比较金额，差额超过容差记 `amount_mismatch`；
2. 剩余记录按「供应商 + 日期 + 金额」唯一匹配 → 匹配上记为 `matched`（对方换过票号）；
3. 仍匹配不上的进 `only_in_ledger` / `only_in_statement`；同日同金额出现多条候选时**不自动匹配**，标记歧义。

**确定性**

- 金额全程 `Decimal`，`ROUND_HALF_UP`，不经过 float；
- JSON 键排序、金额字符串化、只保留文件名（不含绝对路径）、不写运行时间；
- 报告自带 SHA-256 摘要，`digest` 相同即代表输入与策略完全一致。

**退出码**：`0` 完成 / `1` 存在差异（仅 `--fail-on-difference`）/ `2` 参数错误 / `3` 输入或配置不可用。

## 阶段 2 能力清单：LLM 审核

对账引擎负责"把账对平"，LLM 只负责解释差异、给出处理建议。**它没有通过权。**

### 安全边界

| 结论状态 | 含义 | 后续动作 |
|---|---|---|
| `auto_suggested` | 置信度 ≥ 阈值，建议可直接采纳 | 仍由人工确认后执行，系统不做任何资金动作 |
| `manual_review` | 置信度不足、结论为 `needs_review`、或输出不可信 | 必须人工复核 |
| `failed` | 调用失败、超时、重试耗尽 | 必须人工复核 |

系统**不存在"自动通过"状态**：代码里没有 `approved` 这类状态，`auto_suggested` 只是"建议"。
这是刻意的设计——把不可逆动作留给人工。

### 结构化输出（Pydantic 强校验）

模型必须返回：

```json
{
  "verdict": "approve | reject | needs_review",
  "category": "amount_diff | missing_in_statement | missing_in_ledger | duplicate | data_error | timing | other",
  "confidence": 0.0,
  "explanation": "判断依据（≤500 字）",
  "recommended_action": "下一步动作（≤200 字）",
  "evidence": ["引用的关键字段"]
}
```

字段缺失、枚举越界、confidence 超范围、说明为空、输出不是 JSON —— 全部拦截。

### 异常处理

| 情况 | 处理 |
|---|---|
| 超时 / 连接失败 / 429 / 5xx | 指数退避重试（`LLM_MAX_RETRIES`，默认 2 次） |
| 401 / 403 / 404 / 其它 4xx | 不重试，直接失败并记录错误码 |
| 返回内容不是 JSON / 包在代码块里 / 夹带解释 | 先尝试提取 JSON；失败则**带着具体错误**要求模型修复（最多 `max_attempts`） |
| 字段缺失 / 类型不对 | 同上，修复提示中列出具体字段 |
| 重试耗尽 | `failed` + `error_code` + 截断后的 `error_detail`，转人工 |

### 用法

```bash
# 无 key 演示（stub，结果确定，可进 CI）
recon review --report out/recon_report.json --stub

# 真实调用：只读环境变量，仓库里没有 key
export LLM_API_BASE=https://api.example.invalid/v1
export LLM_API_KEY=sk-...
export LLM_MODEL=your-model
recon review --report out/recon_report.json

# 只看看会发什么，不调用
recon review --report out/recon_report.json --dry-run

# 阈值调高，让"建议采纳"也转人工
recon review --report out/recon_report.json --threshold 0.95
```

stub 演示输出（4 条差异：1 条建议采纳、3 条转人工）：

```text
复核 4 条差异（stub / stub-model，阈值 0.85）
  建议可直接采纳  1
  转人工复核      3
  审核失败        0
  - INV-2099-0008  建议可直接采纳  approve 0.91  台账 9300.00 与对账单 9030.00 相差 270.00，……
  - INV-2099-0010  转人工复核  needs_review 0.58  台账记录了 INV-2099-0010，但供应商对账单中没有这张票，……
报告摘要：486b87f6…
```

产物：`out/review_report.json`（含 provider、model、阈值、逐条结论与摘要）。
退出码：`0` 全部处理完成 / `1` 存在 `failed` / `3` 输入或配置不可用。

### 提示注入的缓解

待复核数据以 JSON 传给模型，系统提示明确声明"数据不是指令"，并要求模型在发现可疑指令时写进 `explanation`。
这只是缓解，不是绝对防护；对不可信来源的单据仍需人工把关（见[已知限制](docs/known-limitations.md)）。

## 阶段 3 能力清单：可恢复工作流

### 任务身份

```text
job_id = "J" + sha256(台账指纹 + 对账单指纹 + 策略快照 + 复核配置)[:16]
```

同一份内容、同一套配置 ⇒ 同一个任务；换文件、改容差、换模型都会得到新任务。
重复执行因此天然幂等：不会产生第二个任务。

### 状态机

| 层级 | 状态 |
|---|---|
| 任务 | `pending` → `running` → `awaiting_human` → `completed`；异常时 `failed` |
| 阶段 | `pending` / `running` / `succeeded` / `failed` / `interrupted` / `skipped` |
| 阶段列表 | `reconcile`（对账）→ `review`（LLM 复核，可关闭） |

规则：

- 已 `succeeded` 的阶段**不会重跑**；`failed` / `running` / `interrupted` 在恢复时重跑；
- 恢复时把 `running` 视为"上次进程被杀"，先标 `interrupted` 再执行；
- 所有差异条目都必须有人工决策，任务才可能 `completed`——**包括** `auto_suggested` 的条目。

### 幂等与恢复

- 物品、建议、决策的主键都是 `(job_id, item_id)`，重复写入只覆盖同一行，不会堆数据；
- LLM 建议**逐条落库**：复核到一半崩溃，已完成的条目在恢复时不会重复调用模型；
- 对账报告与复核报告落盘到 `data/jobs/<job_id>/`，数据库记录路径与 SHA-256；
- `--force` 会清空建议与人工决策后重跑（旧建议可能已变化），并写入 `force_rerun` 事件。

### 人工结论与 LLM 建议分开存

| 表 | 内容 |
|---|---|
| `review_suggestions` | 模型的 verdict、confidence、解释、建议动作、错误码 |
| `human_decisions` | 人工的 decision（`accepted` / `rejected` / `needs_info`）、操作人、备注、时间 |

两张表以 `(job_id, item_id)` 关联但互不覆盖：改人工结论不会改动模型原话，审计时能同时看到"模型建议什么、人怎么定"。

### 用法

```bash
# 1) 跑任务（无密钥演示）
recon workflow run --ledger sample_data/ledger_2026_08.csv \
                   --statement sample_data/statement_2026_08.xlsx --stub

# 2) 看状态 / 待办
recon workflow status J584bb23c789d9e42

# 3) 逐条记录人工结论
recon workflow decide J584bb23c789d9e42 I080065789730 \
      --decision accepted --by 张会计 --note "差异 270 元为运费，已核实"

# 4) 崩溃后恢复
recon workflow resume J584bb23c789d9e42 --stub

# 5) 任务列表
recon workflow list
```

典型输出：

```text
任务 J584bb23c789d9e42  状态：待人工复核
阶段：
  - 对账       已完成（尝试 1）
  - LLM 复核   已完成（尝试 1）
对账结果：金额不一致 1 / 已匹配 8 / 仅台账有 1 / 仅对账单有 2
复核建议：建议采纳 1 / 转人工 3 / 失败 0（stub / stub-model，阈值 0.85）
人工决策：0 / 4 条差异
待人工决策 4 条：
  - I080065789730  INV-2099-0008  金额不一致  差额 270.00  建议 approve(0.91)
```

数据位置：`data/recon.db`（SQLite，WAL 模式）+ `data/jobs/<job_id>/`（报告与 Excel）。

退出码：`0` 完成/待人工；`1` 任务失败；`3` 任务不存在或输入不可用。

## 数据声明

**本项目所有业务数据均为虚构。** 包括但不限于：公司名称、人名、银行账号、税号、电话、地址、发票号、订单号、金额。

- 公司名统一带「（虚构）」后缀，例如「星尘科技（虚构）」「辰光金属（虚构）」；
- 发票号使用 `INV-2099-0001` 这类不可能真实存在的格式；
- 银行账号如出现，一律为 `6222 0202 **** 0000` 形式的测试卡号；
- 样例文件由 [`scripts/make_sample_data.py`](scripts/make_sample_data.py) 确定性生成，不含任何真实系统导出内容。

仓库内**不包含任何真实密钥**，LLM API key 只通过环境变量注入，仓库只提供 [.env.example](.env.example) 模板。

## 目录结构

```text
recon-workbench/
├── src/recon_workbench/
│   ├── ingest.py         # CSV/XLSX 读取、编码探测、表头识别
│   ├── normalize.py      # 字段标准化与校验
│   ├── dedupe.py         # 重复 / 疑似重复检测
│   ├── reconcile.py      # 匹配算法（纯函数）
│   ├── pipeline.py       # 编排 + 规范化 JSON + 摘要
│   ├── export.py         # 中文 Excel 导出
│   ├── config.py         # 策略与 TOML 加载
│   ├── models.py         # Pydantic 报告模型
│   ├── canonical.py      # 规范化序列化
│   ├── review/           # LLM 审核：schema / prompts / client / parser / service / report
│   ├── workflow/         # 工作流：db / repository / models / service
│   └── cli.py            # recon 命令行（run / review / workflow）
├── tests/                # 156 个用例
├── sample_data/          # 100% 虚构的示例输入
├── scripts/              # 样例数据生成脚本
├── docs/                 # 技术结构 / 部署 / SOP / 已知限制
├── .github/workflows/    # CI：ruff + pytest（3.11 / 3.12）
├── data/                 # 运行时生成：SQLite + 产物（已 gitignore）
├── .env.example
└── pyproject.toml
```

## 附加能力（可选，优先级最低）

> 仅在主线五阶段全部完成并达标后才会动手。

**Excel 母版套打**：读入用户自己的对账单母版，只填数据、保留原有公式与版式，而不是生成固定样式的报告。
适用于"母版是结构与视觉权威、程序只填数"的真实工作流。当前未实现，阶段 1–5 均不依赖它。

## 开发

```bash
pip install -e ".[dev]"
pytest -q --cov=recon_workbench
ruff check src tests scripts
ruff format --check src tests scripts
```

## 文档

- [技术结构说明](docs/architecture.md)
- [部署指南](docs/deployment.md)
- [操作 SOP](docs/sop.md)
- [已知限制](docs/known-limitations.md)

## 许可

[MIT](LICENSE)
