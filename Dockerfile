FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 先拷贝元数据与源码，利用 Docker 层缓存
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install ".[server]"

# 非 root 运行；/app/data 用于 SQLite、产物与上传
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app
USER appuser

ENV RECON_DB_PATH=/app/data/recon.db \
    RECON_STORAGE_DIR=/app/data/jobs \
    RECON_UPLOAD_DIR=/app/data/uploads \
    RECON_HOST=0.0.0.0 \
    RECON_PORT=8000 \
    RECON_LOG_JSON=1

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')"

CMD ["python", "-m", "recon_workbench.api"]
