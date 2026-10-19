"""工作流层（阶段 3）：SQLite 状态机、断点恢复、幂等写入、人工决策。

对外入口：
- ``start_job`` / ``run_job`` / ``resume_job``：推进任务；
- ``record_decision``：记录人工结论（与 LLM 建议分开存储）；
- ``get_status`` / ``list_jobs``：查询。
"""

from __future__ import annotations

from .db import connect, transaction
from .models import (
    DecisionView,
    EventView,
    HumanDecision,
    ItemView,
    JobStatus,
    JobSummary,
    StageName,
    StageStatus,
    StageView,
    SuggestionView,
    WorkflowStatus,
)
from .service import (
    ClientProvider,
    JobNotFound,
    ReviewSettings,
    WorkflowError,
    compute_job_id,
    create_job,
    get_status,
    list_jobs,
    record_decision,
    resume_job,
    run_job,
    start_job,
)

__all__ = [
    "ClientProvider",
    "DecisionView",
    "EventView",
    "HumanDecision",
    "ItemView",
    "JobNotFound",
    "JobStatus",
    "JobSummary",
    "ReviewSettings",
    "StageName",
    "StageStatus",
    "StageView",
    "SuggestionView",
    "WorkflowError",
    "WorkflowStatus",
    "compute_job_id",
    "connect",
    "create_job",
    "get_status",
    "list_jobs",
    "record_decision",
    "resume_job",
    "run_job",
    "start_job",
    "transaction",
]
