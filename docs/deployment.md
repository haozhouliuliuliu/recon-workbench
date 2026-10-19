# 部署指南（阶段 1）

> 阶段 1 是纯本地命令行工具，没有服务端依赖。阶段 3/4 引入 SQLite 与 FastAPI 后会补充 Docker Compose 部署章节。

## 环境要求

- Python 3.11 或 3.12（3.13 未验证）
- 无需数据库、无需 Redis
- 磁盘：源码与虚拟环境约 60 MB

## 本地安装

```bash
git clone https://github.com/haozhouliuliuliu/recon-workbench.git
cd recon-workbench
python -m venv .venv
.venv\Scripts\activate        # Windows
source .venv/bin/activate     # macOS / Linux
pip install -e ".[dev]"
```

安装后可使用 `recon` 命令，也可以 `python -m recon_workbench.cli`。

## 环境变量

```bash
cp .env.example .env
```

**程序不会自动加载 `.env`**：它只读进程环境变量。可以手动 `set -a; source .env`（bash）、
`$env:LLM_API_KEY="..."`（PowerShell），或由部署平台/密钥管理服务注入。
**`.env` 已在 `.gitignore` 中，不会被提交。**

### LLM（阶段 2）

| 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `LLM_API_BASE` | 是 | — | 兼容 OpenAI 的地址，例如 `https://api.example.com/v1` |
| `LLM_API_KEY` | 是 | — | 只走环境变量，不落盘、不入日志 |
| `LLM_MODEL` | 是 | — | 服务商支持的模型名 |
| `LLM_TIMEOUT_SECONDS` | 否 | 30 | 单次请求超时 |
| `LLM_MAX_RETRIES` | 否 | 2 | 传输失败重试次数（总尝试 = 1 + 该值） |
| `LLM_CONFIDENCE_THRESHOLD` | 否 | 0.85 | 低于该值转人工 |
| `LLM_TEMPERATURE` | 否 | 0 | 建议保持 0，减少输出漂移 |
| `LLM_JSON_MODE` | 否 | 1 | 是否发送 `response_format=json_object`；服务商不支持时设为 0 |

没有密钥时仍可完整体验流程：

```bash
recon review --report out/recon_report.json --stub
```

## 验证安装

```bash
recon --version
pytest -q
```

## 数据与备份（阶段 3）

任务模式会在本地生成：

| 路径 | 内容 |
|---|---|
| `data/recon.db` | SQLite：任务、阶段、明细、LLM 建议、人工决策、事件 |
| `data/recon.db-wal` / `-shm` | WAL 日志，运行中属正常现象 |
| `data/jobs/<job_id>/` | 对账报告、Excel、复核报告 |

备份建议：**停掉 CLI/服务后**，整体复制 `data/` 目录，或使用 SQLite 的在线备份接口。
只拷 `recon.db` 而漏掉 `-wal` 可能丢最后几笔事务；只拷数据库而漏掉 `data/jobs/` 会丢报告产物（数据库里仍保留路径与哈希，可发现不一致）。

恢复时把 `data/` 放回原位即可；若源文件路径变化，`workflow resume` 会提示，需要重新建任务。

## 离线环境

运行时依赖：`openpyxl`、`pydantic`、`httpx`。可在有网机器上执行：

```bash
pip download -d wheels -e ".[dev]"
```

把仓库与 `wheels/` 一并拷贝到离线机器，再 `pip install --no-index --find-links wheels -e .`。
离线且没有 LLM 服务时，`recon review --stub` 仍然可用（本地假响应，不联网）。

## 常见问题

**Q: `recon` 命令找不到？**
A: 虚拟环境未激活，或未执行 `pip install -e .`。

**Q: Excel 打开提示格式错误？**
A: 确认输出没有被其他程序占用（Windows 下 Excel 会锁文件），换一个 `--out-dir` 重跑。

**Q: 中文乱码？**
A: 输入 CSV 建议用 UTF-8（带 BOM 也可以）；GBK 会自动识别，其他编码请先转码。
