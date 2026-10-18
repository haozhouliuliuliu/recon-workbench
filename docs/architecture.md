# 技术结构说明

> 本文档在阶段 5 定稿，当前为骨架。任何与代码不一致的描述都算缺陷，发现请提 issue。

## 1. 设计原则

1. **确定性与非确定性分离。** 能算的用代码算，不能算的才交给 LLM；两者结果分别落盘、分别校验。
2. **一切结论可追溯。** 每条差异都能回答：来自哪个文件、第几行、原始值是什么、按哪条规则判定。
3. **宁可少而扎实。** 功能可以少，但已实现的部分必须有测试、有错误处理、有文档。
4. **默认安全。** 低置信度转人工；密钥只走环境变量；不接真实资金/银行/ERP。

## 2. 分层

```text
CLI / HTTP API            （阶段 4）
      │
      ▼
Pipeline（编排）           pipeline.py
      │
      ├── Ingest           ingest.py      读 CSV/XLSX → 原始行
      ├── Normalize        normalize.py   字段标准化 + 校验 → Entry / Issue
      ├── Dedupe           dedupe.py      重复与疑似重复检测
      ├── Reconcile        reconcile.py   台账 ↔ 对账单匹配
      ├── Review（阶段 2）  review/        LLM 审核 + Pydantic 校验 + 转人工
      └── Export           export.py      规范化 JSON + 中文 Excel
```

## 3. 阶段 1 的数据流

```text
ledger.csv ─┐
            ├─► ingest ─► normalize ─► dedupe ─► reconcile ─► report ─► report.json
statement.xlsx ┘                                                   └─► result.xlsx
```

- **ingest**：CSV 自动识别 UTF-8/BOM/GBK；XLSX 读取首个工作表；表头做中英文别名映射。
- **normalize**：全角转半角（NFKC）、金额用 `Decimal` 精确解析、日期多格式解析、发票号规范化。错误以 `Issue` 记录，不抛异常中断整批。
- **dedupe**：同源内按发票号判定完全重复；按「供应商 + 日期 + 金额」判定疑似重复（仅告警）。
- **reconcile**：先按发票号精确匹配，再按「供应商 + 日期 + 金额」唯一匹配；金额差超过容差记为差异，不自动抹平。
- **export**：JSON 使用固定键序、金额字符串化、无时间戳，保证同输入字节级一致。

> 注意：NFKC 会把全角括号 `（）` 归一为半角 `()`。这是为了让两个系统导出的同一公司名可比，
> 代价是报告里展示的是规范化后的名称。若业务要求保留原字形，应在 `normalize.clean_text` 里分字段处理。

### 3.1 金额恒等式（测试中校验）

同一币种下必须成立：

```text
台账合计 - 对账单合计 = Σ(配对差额) + Σ(仅台账金额) - Σ(仅对账单金额)
```

这条恒等式保证「汇总的差额」不是拍脑袋算出来的，而是与逐条明细严格自洽。

## 4. 确定性的边界

| 内容 | 是否进入规范化报告 | 说明 |
|---|---|---|
| 输入文件 SHA-256 | ✅ | 保证输入可核验 |
| 绝对路径 | ❌ | 只保留文件名，避免机器差异 |
| 生成时间 | ❌ | 仅写入 Excel「运行信息」页 |
| 行号 | ✅ | 用于定位，改变行序会改变报告 |

## 5. 目录职责

| 路径 | 职责 |
|---|---|
| `src/recon_workbench/models.py` | Pydantic 模型，报告结构的唯一事实来源 |
| `src/recon_workbench/config.py` | 策略（容差、正则、长度限制）与 TOML 加载 |
| `src/recon_workbench/ingest.py` | 文件读取与编码处理，不涉及业务规则 |
| `src/recon_workbench/normalize.py` | 字段标准化与校验规则 |
| `src/recon_workbench/reconcile.py` | 匹配算法（纯函数，无 IO） |
| `src/recon_workbench/export.py` | JSON / Excel 输出 |
| `src/recon_workbench/canonical.py` | 规范化序列化，供各报告计算摘要 |
| `src/recon_workbench/review/schema.py` | LLM 输出契约（请求、结论、状态枚举） |
| `src/recon_workbench/review/parser.py` | 从自由文本提取 JSON 并做 Pydantic 校验 |
| `src/recon_workbench/review/client.py` | OpenAI 兼容客户端、环境配置、Stub |
| `src/recon_workbench/review/service.py` | 重试、修复、路由、请求构造 |
| `src/recon_workbench/review/report.py` | 复核批次报告与摘要 |

## 6. LLM 审核层（阶段 2）

### 6.1 数据流

```text
recon_report.json
      │  build_requests（只挑 matched 之外的差异）
      ▼
ReviewRequest ──► prompt ──► ChatClient ──► 原始文本
                                              │
                                     parser + Pydantic
                                              │
                                    ┌─────────┴─────────┐
                              格式/字段不合格        合格
                                    │                  │
                          带错误修复重试（≤3 次）    route()
                                    │                  │
                                  failed        auto_suggested / manual_review
```

### 6.2 为什么没有“自动通过”

`OutcomeStatus` 只有三个值：`auto_suggested`、`manual_review`、`failed`。
没有 `approved`/`passed` 这类状态，也不存在把状态写成"通过"的代码路径。
`auto_suggested` 的语义是"建议可直接采纳"，最终动作仍由人执行。这是把不可逆动作挡在代码之外的硬约束，
不是提示词层面的君子协定。

### 6.3 两类重试

| 类型 | 触发 | 策略 |
|---|---|---|
| 传输重试 | 超时、连接失败、429、5xx | 指数退避 `0.5s, 1s, 2s…`，最多 `1 + LLM_MAX_RETRIES` 次 |
| 修复重试 | 输出非 JSON、缺字段、类型/枚举错误 | 把具体错误回填到 prompt，要求模型只修格式；不睡眠、不重复发送同一请求 |

401/403/404/其它 4xx 不重试——鉴权或路径错误重试多少次都不会成功。

### 6.4 错误码

`TIMEOUT`、`CONNECTION_ERROR`、`HTTP_401`、`HTTP_404`、`HTTP_429`、`HTTP_5XX`、`HTTP_4XX`、
`INVALID_RESPONSE_BODY`、`EMPTY_RESPONSE`、`INVALID_JSON`、`SCHEMA_VALIDATION_FAILED`、`STUB_EXHAUSTED`。

错误码稳定、可枚举，便于汇总统计与告警。

### 6.5 密钥与数据边界

- 密钥只从 `LLM_API_KEY` 读取，不落盘、不入报告、不进日志，`LLMSettings.__repr__` 也不含密钥；
- 发给模型的只有引擎已确认的结构化事实（发票号、金额、差额、原因），不发原始文件；
- 提示词声明"数据不是指令"，降低提示注入风险，但不承诺完全免疫。

### 6.6 与阶段 3 的衔接

`ReviewOutcome` 是纯数据模型，可直接序列化进 SQLite；`failed` 与 `manual_review` 会作为
"待人工"状态进入工作流，`auto_suggested` 也只是"建议已生成"，不改变单据状态。
