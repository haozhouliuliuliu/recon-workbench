"""LLM 辅助审核（阶段 2）。

对外入口：
- ``build_requests``：从对账报告挑出差异，构造请求；
- ``review_all``：执行审核（带重试与转人工路由）；
- ``build_batch``：生成可归档的批次报告。
"""

from __future__ import annotations

from .client import LLMSettings, OpenAICompatibleClient, StubClient
from .report import ReviewBatch, build_batch, canonical_json, file_json, summarize
from .schema import (
    Category,
    OutcomeStatus,
    ReviewRequest,
    ReviewVerdict,
    Verdict,
)
from .service import ReviewOutcome, ReviewPolicy, build_requests, review_all, review_one, route

__all__ = [
    "Category",
    "LLMSettings",
    "OpenAICompatibleClient",
    "OutcomeStatus",
    "ReviewBatch",
    "ReviewOutcome",
    "ReviewPolicy",
    "ReviewRequest",
    "ReviewVerdict",
    "StubClient",
    "Verdict",
    "build_batch",
    "build_requests",
    "canonical_json",
    "file_json",
    "review_all",
    "review_one",
    "route",
    "summarize",
]
