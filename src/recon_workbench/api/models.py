"""HTTP 接口的请求/响应模型。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from ..workflow.models import HumanDecision, JobSummary


class JobAccepted(BaseModel):
    """上传/恢复接口的受理结果。"""

    model_config = ConfigDict(frozen=True)

    job_id: str
    status: str
    created: bool = Field(description="true 表示新建任务；false 表示复用了已有任务")
    detail_url: str


class ArtifactInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: str
    filename: str
    bytes: int
    sha256: str
    download_url: str


class DecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: str = Field(min_length=1, max_length=64)
    decision: HumanDecision
    decided_by: str = Field(min_length=1, max_length=64)
    note: str | None = Field(default=None, max_length=500)


class JobListOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    items: tuple[JobSummary, ...]
    total: int
    limit: int
    offset: int


class HealthOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: str
    version: str
    review_provider: str
