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

（阶段 2 起补充 LLM 审核、状态机、API 三节。）
