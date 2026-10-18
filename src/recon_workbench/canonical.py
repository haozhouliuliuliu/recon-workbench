"""规范化序列化：所有报告共用同一套规则，保证摘要可跨次比较。

规则：键排序、无多余空白、UTF-8、金额在模型层已字符串化、不写时间戳。
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel


def canonical_dumps(payload: Any) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_model_bytes(model: BaseModel, *, exclude: set[str]) -> bytes:
    return canonical_dumps(model.model_dump(mode="json", exclude=exclude))
