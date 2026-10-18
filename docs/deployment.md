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

阶段 1 不读取任何环境变量；`.env` 仅为阶段 2 的 LLM 配置预留。
**`.env` 已在 `.gitignore` 中，不会被提交。**

## 验证安装

```bash
recon --version
pytest -q
```

## 离线环境

阶段 1 的运行时依赖只有 `openpyxl` 与 `pydantic`。可在有网机器上执行：

```bash
pip download -d wheels -e ".[dev]"
```

把仓库与 `wheels/` 一并拷贝到离线机器，再 `pip install --no-index --find-links wheels -e .`。

## 常见问题

**Q: `recon` 命令找不到？**
A: 虚拟环境未激活，或未执行 `pip install -e .`。

**Q: Excel 打开提示格式错误？**
A: 确认输出没有被其他程序占用（Windows 下 Excel 会锁文件），换一个 `--out-dir` 重跑。

**Q: 中文乱码？**
A: 输入 CSV 建议用 UTF-8（带 BOM 也可以）；GBK 会自动识别，其他编码请先转码。
