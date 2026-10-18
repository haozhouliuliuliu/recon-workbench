"""把 LLM 的自由文本变成可校验的结构。

现实里模型会返回：纯 JSON、```json 代码块、前后带解释的文字、甚至截断的 JSON。
这里全部尝试一遍，仍失败就抛统一的 ``LLMStructuredError``，由 service 决定是否带着错误信息重试。
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import ValidationError

from .errors import LLMStructuredError
from .schema import ReviewVerdict

_FENCED = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)


def _candidates(text: str) -> list[str]:
    raw = text.strip()
    found: list[str] = []
    for match in _FENCED.finditer(raw):
        found.append(match.group(1).strip())
    found.append(raw)
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end > start:
        found.append(raw[start : end + 1])

    unique: list[str] = []
    for item in found:
        if item and item not in unique:
            unique.append(item)
    return unique


def extract_json_object(text: str) -> dict[str, Any]:
    if not text or not text.strip():
        raise LLMStructuredError("EMPTY_RESPONSE", "模型返回了空内容")

    problems: list[str] = []
    for candidate in _candidates(text):
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError as exc:
            problems.append(f"第 {exc.lineno} 行第 {exc.colno} 列：{exc.msg}")
            continue
        if isinstance(data, dict):
            return data
        problems.append(f"顶层类型是 {type(data).__name__}，要求 object")

    detail = "；".join(problems[:3]) or "未找到 JSON 对象"
    raise LLMStructuredError("INVALID_JSON", f"无法从模型输出中解析出 JSON 对象（{detail}）")


def format_validation_error(exc: ValidationError) -> str:
    parts: list[str] = []
    for error in exc.errors()[:5]:
        location = ".".join(str(item) for item in error["loc"]) or "<root>"
        parts.append(f"{location}: {error['msg']}")
    return "；".join(parts)


def parse_verdict(text: str) -> ReviewVerdict:
    data = extract_json_object(text)
    try:
        return ReviewVerdict.model_validate(data)
    except ValidationError as exc:
        raise LLMStructuredError(
            "SCHEMA_VALIDATION_FAILED",
            f"输出不符合约定字段（{format_validation_error(exc)}）",
        ) from exc
