"""LLM 审核相关异常。

错误码会成为报告里的 ``error_code``，因此保持稳定、可枚举、便于统计。
"""

from __future__ import annotations


class LLMError(Exception):
    """LLM 调用失败的基类。"""

    def __init__(self, code: str, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


class LLMTransportError(LLMError):
    """网络/服务端问题：超时、连接失败、429、5xx。"""


class LLMStructuredError(LLMError):
    """输出不可解析或不符合 schema。修复方式是带错误信息重试，而不是退避重试。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message, retryable=False)
