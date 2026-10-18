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
| 1 | 确定性数据处理引擎：导入、字段校验、金额核对、重复检测、Excel 导出 | 🚧 进行中 |
| 2 | LLM 审核模块：结构化输出 + Pydantic 校验、超时/坏 JSON/缺字段处理、低置信度转人工 | ⏳ 未开始 |
| 3 | 完整工作流：SQLite 状态机、中断可定位、幂等重跑 | ⏳ 未开始 |
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
│   └── cli.py            # recon 命令行
├── tests/                # pytest 用例
├── sample_data/          # 100% 虚构的示例输入
├── scripts/              # 样例数据生成脚本
├── docs/                 # 技术结构 / 部署 / SOP / 已知限制
├── .github/workflows/    # CI：ruff + pytest（3.11 / 3.12）
├── .env.example
└── pyproject.toml
```

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
