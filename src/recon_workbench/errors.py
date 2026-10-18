"""领域内异常。

所有异常都带一句中文说明，直接在 CLI 里展示给使用者；堆栈细节交给日志。
"""

from __future__ import annotations


class ReconError(Exception):
    """本项目的异常基类。"""


class InputError(ReconError):
    """输入文件不可用：缺失、格式不支持、表头缺失、编码无法识别。"""


class ConfigError(ReconError):
    """策略配置非法：未知键、类型不对、取值越界。"""
