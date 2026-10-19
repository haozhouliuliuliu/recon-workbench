# 技术结构说明

> 本文档描述当前实现的架构与数据流；如与代码不一致，以代码和测试为准。

## 1. 设计原则

1. **确定性与非确定性分离。** 能算的用代码算，不能算的才交给 LLM；两者结果分别落盘、分别校验。
2. **一切结论可追溯。** 每条差异都能回答：来自哪个文件、第几行、原始值是什么、按哪条规则判定。
3. **宁可少而扎实。** 功能可以少，但已实现的部分必须有测试、有错误处理、有文档。
4. **默认安全。** 低置信度转人工；密钥只走环境变量；不接真实资金/银行/ERP。

## 2. 分层

```text
CLI / HTTP API
      │
      ▼
Pipeline（编排）           pipeline.py
      │
      ├── Ingest           ingest.py      读 CSV/XLSX → 原始行
      ├── Normalize        normalize.py   字段标准化 + 校验 → Entry / Issue
      ├── Dedupe           dedupe.py      重复与疑似重复检测
      ├── Reconcile        reconcile.py   台账 ↔ 对账单匹配
      ├── Review  review/        LLM 审核 + Pydantic 校验 + 转人工
      └── Export           export.py      规范化 JSON + 中文 Excel
```

## 3. 对账数据流

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
| `src/recon_workbench/workflow/db.py` | SQLite 连接、schema、事务 |
| `src/recon_workbench/workflow/repository.py` | 纯 SQL 数据访问（幂等 upsert） |
| `src/recon_workbench/workflow/models.py` | 工作流视图模型与状态枚举 |
| `src/recon_workbench/workflow/service.py` | 任务身份、阶段推进、恢复、人工决策 |
| `src/recon_workbench/api/app.py` | FastAPI 路由、错误处理、后台任务、下载 |
| `src/recon_workbench/api/locks.py` | 进程内任务锁 |
| `src/recon_workbench/settings.py` | 环境变量配置与校验 |
| `src/recon_workbench/logging_config.py` | 文本 / JSON 日志 |

## 6. LLM 审核层

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

### 6.6 与工作流的衔接

`ReviewOutcome` 是纯数据模型，可直接序列化进 SQLite；`failed` 与 `manual_review` 会作为
"待人工"状态进入工作流，`auto_suggested` 也只是"建议已生成"，不改变单据状态。

## 7. 工作流与状态机

### 7.1 表结构

```text
jobs              任务身份与输入指纹、整体状态、两个阶段的报告摘要
  ├── job_stages      每个阶段的 status / attempts / started_at / error
  ├── recon_items     对账明细快照（12 条，含 matched）
  ├── review_suggestions   LLM 建议（每条差异一行）
  ├── human_decisions      人工结论（每条差异一行，与建议互不覆盖）
  ├── job_events      追加式事件日志（阶段开始/成功/失败/中断、人工决策）
  └── artifacts       产物路径与 SHA-256（报告、Excel）
```

主键统一为 `(job_id, ...)` 或 `(job_id, stage)`，数据库层面挡住重复写入。

### 7.2 阶段推进

```text
start_job
  ├─ 读表拿 sha256 → 计算 job_id → INSERT ON CONFLICT DO NOTHING
  ├─ reconcile: pending → running → succeeded   （失败则 failed + job failed）
  ├─ review:    pending → running → succeeded   （可 skipped）
  └─ finalize:  pending_items ? awaiting_human : completed
```

- `run_job` 先看阶段状态：`succeeded` 直接跳过，其余重跑；
- `resume_job` 额外把 `running` 标成 `interrupted` 再走 `run_job`；
- 复核阶段**逐条 upsert**，因此崩溃后恢复只补未完成的条目，不重复调用模型。

### 7.3 幂等边界

| 操作 | 幂等 | 说明 |
|---|---|---|
| 重复 `workflow run` | ✅ | 同 job_id，阶段不重跑，不新增行 |
| 崩溃后 `resume` | ✅ | 已成功阶段跳过；建议按 item 去重 |
| 修改人工结论 | ✅ | upsert 同一行，事件留痕 |
| `--force` | 有损 | 清空建议与人工决策，用于输入未变但想重跑 |
| 输入变化 | 新任务 | job_id 变化，旧任务与决策保留在库 |

### 7.4 为什么人工决策要单独一张表

LLM 建议与人工结论是两类事实：前者是"模型说了什么"，后者是"人定了什么"。
放在同一条记录里更新，会让审计无法回答"模型原话是否被改过"。
因此 `review_suggestions` 只写模型输出，`human_decisions` 只写人工动作，
状态查询时按 `(job_id, item_id)` 拼起来展示。

### 7.5 并发与部署边界

当前假设**单机、单写进程**：SQLite 开 WAL 与 `busy_timeout=5s`，能容忍 CLI/测试/API 的轻度并发。
HTTP 接口层增加了**进程内任务锁**（见 8.3），但仍是单进程方案；多 worker/多副本需要数据库抢占，见已知限制。

## 8. HTTP 接口层

### 8.1 分层

```text
FastAPI 路由
  ├─ 请求校验（Pydantic / 文件后缀 / 大小）
  ├─ 调用 workflow.service（不复制业务规则）
  └─ 序列化 WorkflowStatus / 产物下载
```

接口里没有任何对账、匹配或重试逻辑：它只是工作流服务的一层薄封装，CLI 与 API 的行为因此一致。

### 8.2 上传与后台执行

```text
POST /api/jobs
  ├─ 分块读取上传（限制大小）
  ├─ 计算文件 SHA-256 → job_id
  ├─ 落盘 uploads/<job_id>/ledger.* / statement.*
  ├─ read_table 校验表头（失败清理并 400）
  ├─ create_job 登记任务（幂等）
  ├─ BackgroundTasks 调度 _process_job
  └─ 202 {job_id, status, detail_url}
```

`_process_job` 运行在线程池：自己开 SQLite 连接、拿任务锁、调 `run_job`/`resume_job`。
因此上传请求不会被 LLM 调用或大文件处理阻塞。

### 8.3 任务锁

`JobLockRegistry` 是 `threading.Lock` 的注册表：

- 后台任务 `try_acquire`，拿不到说明已有同任务在执行 → 记日志并跳过（幂等）；
- `resume` / `decisions` 先用 `is_busy` 判断，返回 `409 JOB_BUSY`；
- 锁只在单进程内有效。多 worker 时每个 worker 各有一份注册表，需要数据库层抢占（未实现）。

### 8.4 错误约定

| 场景 | HTTP | code |
|---|---|---|
| 参数/枚举不合法 | 422 | `VALIDATION_ERROR` |
| 上传文件类型/表头不对 | 400 | `UNSUPPORTED_FILE_TYPE` / `INVALID_INPUT` |
| 文件超过上限 | 413 | `FILE_TOO_LARGE` |
| 任务不存在 | 404 | `JOB_NOT_FOUND` |
| 任务执行中 | 409 | `JOB_BUSY` |
| 人工决策不合法（如对 matched 条目） | 400 | `WORKFLOW_ERROR` |
| LLM 未配置 | 503 | `CONFIG_ERROR` |
| 数据库不可用 | 503 | `DATABASE_ERROR` |

所有错误体统一为 `{"code","message"}`；校验错误额外带 `errors` 数组。

### 8.5 下载安全

产物路径来自数据库，但下载前会 `resolve()` 并校验必须位于 `RECON_STORAGE_DIR` 下，
防止数据库被篡改后读到任意文件（有测试覆盖）。

## 9. 配置与日志

### 9.1 配置

- 全部来自环境变量，`AppSettings.from_env` 在启动时校验类型与取值范围；
- 引擎策略（容差、正则等）仍走 TOML，可用 `RECON_POLICY_FILE` 指定；
- `recon config show` 输出可公开快照，密钥只显示 `llm_api_key_configured: true/false`；
- 复核配置（provider/model/threshold）参与任务身份，改配置 = 新任务。

### 9.2 日志

- `setup_logging` 幂等，不会叠加 handler；
- `RECON_LOG_JSON=1` 时每行一个 JSON，包含 `ts`、`level`、`logger`、`message` 与业务字段；
- 请求中间件记录 `request_id`、方法、路径、状态码、耗时，并回写 `X-Request-ID`；
- 不记录请求体、上传内容与密钥；LLM 失败只记录错误码与截断后的信息。
