"""日志测试：JSON 字段、文本格式、重复初始化不叠加 handler。"""

from __future__ import annotations

import json
import logging

import pytest

from recon_workbench.logging_config import JsonFormatter, setup_logging


def _record(**extra: object) -> logging.LogRecord:
    record = logging.LogRecord(
        name="recon.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="job_processed",
        args=(),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_json_formatter_includes_context() -> None:
    record = logging.LogRecord(
        name="recon.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="job_processed",
        args=(),
        exc_info=None,
    )
    record.job_id = "J123"
    record.status = "awaiting_human"

    payload = json.loads(JsonFormatter().format(record))
    assert payload["message"] == "job_processed"
    assert payload["level"] == "INFO"
    assert payload["job_id"] == "J123"
    assert payload["status"] == "awaiting_human"
    assert payload["ts"].endswith("Z") or "+00:00" in payload["ts"]


def test_json_formatter_includes_exception() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        exc_info = sys.exc_info()
    record = logging.LogRecord("x", logging.ERROR, __file__, 1, "failed", (), exc_info)
    payload = json.loads(JsonFormatter().format(record))
    assert "ValueError: boom" in payload["exception"]


@pytest.fixture
def restore_root_logging():
    root = logging.getLogger()
    handlers = list(root.handlers)
    level = root.level
    configured = getattr(root, "_recon_configured", False)
    yield
    root.handlers = handlers
    root.setLevel(level)
    if not configured and hasattr(root, "_recon_configured"):
        delattr(root, "_recon_configured")


def test_setup_logging_is_idempotent(restore_root_logging) -> None:
    setup_logging("DEBUG", json_format=False, force=True)
    first = list(logging.getLogger().handlers)
    setup_logging("INFO", json_format=True)
    assert logging.getLogger().handlers == first
    assert logging.getLogger().level == logging.INFO


def test_setup_logging_sets_json_formatter(restore_root_logging) -> None:
    setup_logging("INFO", json_format=True, force=True)
    handler = logging.getLogger().handlers[0]
    assert isinstance(handler.formatter, JsonFormatter)


def test_text_formatter_appends_extra_fields(restore_root_logging) -> None:
    setup_logging("INFO", json_format=False, force=True)
    formatter = logging.getLogger().handlers[0].formatter
    line = formatter.format(_record(job_id="J1", status="awaiting_human"))
    assert "job_processed" in line
    assert "job_id=J1" in line
    assert "status=awaiting_human" in line
