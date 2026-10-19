"""``python -m recon_workbench.api`` 入口。"""

from __future__ import annotations

import uvicorn

from ..logging_config import setup_logging
from ..settings import AppSettings
from .app import create_app


def main() -> None:
    settings = AppSettings.from_env()
    application = create_app(settings)
    setup_logging(settings.log_level, settings.log_json)
    uvicorn.run(
        application,
        host=settings.api_host,
        port=settings.api_port,
        log_config=None,
        access_log=False,
    )


if __name__ == "__main__":
    main()
