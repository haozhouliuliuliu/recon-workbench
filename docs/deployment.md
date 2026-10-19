# 部署指南

> 覆盖三种用法：本地命令行、本地 HTTP 服务、Docker Compose。全部无需外部数据库或 Redis。

## 1. 环境要求

- Python 3.11 或 3.12（3.13 未验证）
- 运行时依赖：`openpyxl`、`pydantic`、`httpx`；接口额外需要 `fastapi`、`uvicorn`、`python-multipart`
- 磁盘：源码与虚拟环境约 80 MB；`data/` 随任务量增长

## 2. 本地安装

```bash
git clone https://github.com/haozhouliuliuliu/recon-workbench.git
cd recon-workbench
python -m venv .venv
.venv\Scripts\activate        # Windows
source .venv/bin/activate     # macOS / Linux

pip install -e ".[dev]"       # 开发 + 接口依赖 + 测试
# 生产环境只需要接口： pip install -e ".[server]"
```

安装后可用 `recon` 命令，也可 `python -m recon_workbench.cli`。

验证：

```bash
recon --version      # recon-workbench 0.4.0
pytest -q            # 200 passed
```

## 3. 配置

所有配置来自环境变量，启动时校验，非法值直接报错。

```bash
cp .env.example .env
set -a; source .env; set +a          # bash
```

**程序不会自动加载 `.env`**：请用 shell、systemd、Docker Compose 或密钥管理服务注入。

### 3.1 运行参数

| 变量 | 默认 | 说明 |
|---|---|---|
| `RECON_DB_PATH` | `data/recon.db` | SQLite 路径 |
| `RECON_STORAGE_DIR` | `data/jobs` | 报告与 Excel 产物目录 |
| `RECON_UPLOAD_DIR` | `data/uploads` | 上传原文件目录 |
| `RECON_MAX_UPLOAD_BYTES` | `20971520` | 单文件大小上限 |
| `RECON_REVIEW_PROVIDER` | `stub` | `stub`=本地假响应；`openai`=真实调用 |
| `RECON_REVIEW_ENABLED` | `true` | 是否默认执行复核（接口可用表单覆盖单次） |
| `RECON_REVIEW_THRESHOLD` | `0.85` | 置信度阈值 |
| `RECON_REVIEW_LIMIT` | `20` | 单任务最多复核条数 |
| `RECON_LOG_LEVEL` | `INFO` | DEBUG/INFO/WARNING/ERROR/CRITICAL |
| `RECON_LOG_JSON` | `0` | 1=每行 JSON |
| `RECON_HOST` / `RECON_PORT` | `0.0.0.0` / `8000` | 监听地址 |

### 3.2 LLM（仅 `openai` 模式）

| 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `LLM_API_BASE` | 是 | — | 兼容 OpenAI 的地址，例如 `https://api.example.com/v1` |
| `LLM_API_KEY` | 是 | — | 只走环境变量，不落盘、不入日志 |
| `LLM_MODEL` | 是 | — | 服务商支持的模型名 |
| `LLM_TIMEOUT_SECONDS` | 否 | 30 | 单次请求超时 |
| `LLM_MAX_RETRIES` | 否 | 2 | 传输失败重试次数（总尝试 = 1 + 该值） |
| `LLM_CONFIDENCE_THRESHOLD` | 否 | 0.85 | 兜底阈值（任务创建时以 `RECON_REVIEW_THRESHOLD` 为准） |
| `LLM_TEMPERATURE` | 否 | 0 | 建议保持 0 |
| `LLM_JSON_MODE` | 否 | 1 | 是否发送 `response_format=json_object` |

没有密钥时仍可完整体验：把 `RECON_REVIEW_PROVIDER` 设为 `stub`。

## 4. 启动 HTTP 服务

```bash
recon api                       # 读取 RECON_HOST / RECON_PORT
recon api --port 9000           # 覆盖端口
# 或（工厂模式）：
uvicorn "recon_workbench.api.app:create_app" --factory --host 0.0.0.0 --port 8000
```

- 交互文档：`http://127.0.0.1:8000/docs`
- 健康检查：`GET /healthz`（含数据库连通性）
- 日志：请求级 `request_id`、耗时、状态码；`RECON_LOG_JSON=1` 便于采集

## 5. Docker Compose

```bash
docker compose up --build -d
curl http://127.0.0.1:8000/healthz
```

- 镜像：`python:3.12-slim`，非 root 用户，内置 healthcheck
- 数据：命名卷 `recon-data` 挂载到 `/app/data`（SQLite + 产物）
- 默认 `RECON_REVIEW_PROVIDER=stub`，开箱即用
- 接真实模型：

```bash
RECON_REVIEW_PROVIDER=openai \
LLM_API_BASE=https://api.example.com/v1 \
LLM_API_KEY=sk-... \
LLM_MODEL=your-model \
docker compose up --build -d
```

停止 / 清理：

```bash
docker compose down          # 保留数据卷
docker compose down -v       # 连数据一起删，慎用
```

> CI 会真实构建镜像并在容器内完成一次上传 → 轮询 → 下载的端到端验证。

## 6. 数据与备份

| 路径 | 内容 |
|---|---|
| `data/recon.db` | SQLite：任务、阶段、明细、LLM 建议、人工决策、事件 |
| `data/recon.db-wal` / `-shm` | WAL 日志，运行中正常 |
| `data/jobs/<job_id>/` | 对账报告、Excel、复核报告 |
| `data/uploads/<job_id>/` | 上传的原始文件 |

备份：**停服后**整体复制 `data/`；或使用 SQLite 在线备份接口。
只拷 `.db` 漏掉 `-wal` 可能丢最后几笔事务；只拷数据库漏掉 `data/jobs/` 会丢产物（数据库里仍保留路径与哈希，可发现不一致）。

恢复：把 `data/` 放回原位；若上传文件路径变化，`resume` 会提示，需重新创建任务。

## 7. 离线环境

```bash
pip download -d wheels -e ".[server,dev]"
```

把仓库与 `wheels/` 拷到离线机器：

```bash
pip install --no-index --find-links wheels -e ".[server,dev]"
```

离线且无 LLM 服务时，`RECON_REVIEW_PROVIDER=stub` 仍然可用。

## 8. 安全与上线前检查

当前版本**没有鉴权、没有 TLS、没有速率限制**，只适合本地或可信内网：

- 公网部署前必须在反向代理层加认证（Basic/OIDC）与 HTTPS；
- 不要把 `LLM_API_KEY` 写进镜像、Compose 文件或 Git；
- `/api/jobs/{id}/download/*` 已限制只能读取 `RECON_STORAGE_DIR` 下的文件；
- 单进程假设：`uvicorn --workers` > 1 会破坏内存任务锁，扩展方案见[已知限制](known-limitations.md)。

## 9. 常见问题

**Q: `recon` 命令找不到？**
A: 虚拟环境未激活，或未执行 `pip install -e .`。

**Q: `recon api` 报缺少模块？**
A: 未安装服务端依赖：`pip install -e ".[server]"`。

**Q: Excel 打开提示格式错误？**
A: 确认输出没有被其他程序占用（Windows 下 Excel 会锁文件），换一个输出目录重跑。

**Q: 中文乱码？**
A: 输入 CSV 建议 UTF-8（带 BOM 也可以）；GBK 会自动识别，其他编码请先转码。终端可 `chcp 65001`。

**Q: 上传返回 503 CONFIG_ERROR？**
A: `RECON_REVIEW_PROVIDER=openai` 但缺少 `LLM_*` 变量。补齐或改回 `stub`。
