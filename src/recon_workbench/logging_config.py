"""日志配置。

默认单行文本；``RECON_LOG_JSON=1`` 时输出一行一个 JSON，方便容器与采集端解析。
密钥与请求体不进日志：业务日志只记录 job_id、阶段、耗时、状态码。
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

RESERVED_ATTRS = {
    "name",
    "msg",
    "args",
    "levelname",
    "levelno",
    "pathname",
    "filename",
    "module",
    "exc_info",
    "exc_text",
    "stack_info",
    "lineno",
    "funcName",
    "created",
    "msecs",
    "relativeCreated",
    "thread",
    "threadName",
    "processName",
    "process",
    "taskName",
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in RESERVED_ATTRS or key.startswith("_"):
                continue
            payload[key] = value if isinstance(value, (str, int, float, bool)) or value is None else str(value)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)


class TextFormatter(logging.Formatter):
    """单行文本，并把 ``extra`` 里的业务字段追加在末尾。"""

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras: list[str] = []
        for key, value in record.__dict__.items():
            if key in RESERVED_ATTRS or key.startswith("_"):
                continue
            extras.append(f"{key}={value}")
        return f"{base} {' '.join(extras)}" if extras else base


def setup_logging(level: str = "INFO", json_format: bool = False, *, force: bool = False) -> None:
    """配置根 logger；重复调用不会叠加 handler。"""

    root = logging.getLogger()
    if getattr(root, "_recon_configured", False) and not force:
        root.setLevel(level.upper())
        return

    handler = logging.StreamHandler()
    if json_format:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(TextFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root.handlers = [handler]
    root.setLevel(level.upper())
    root._recon_configured = True  # type: ignore[attr-defined]

    # 访问日志由应用中间件统一记录，避免 uvicorn 自己再刷一遍
    logging.getLogger("uvicorn.access").propagate = False
