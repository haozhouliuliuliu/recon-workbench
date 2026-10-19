"""HTTP 接口层（阶段 4）。

启动方式：

    recon api
    # 或
    python -m recon_workbench.api
    # 或（工厂模式，便于 uvicorn 热重载/多 worker）
    uvicorn "recon_workbench.api.app:create_app" --factory
"""

from __future__ import annotations

from ..settings import AppSettings
from .app import ApiError, create_app
from .locks import JobLockRegistry

__all__ = ["ApiError", "AppSettings", "JobLockRegistry", "create_app"]
