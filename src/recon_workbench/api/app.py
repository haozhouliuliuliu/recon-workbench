"""FastAPI 应用。

接口只做三件事：接收/校验请求、调用工作流服务、把结果序列化出去。
业务规则一律留在 ``workflow`` 与引擎层，避免"接口里写一套、CLI 里写另一套"。

并发模型：单进程 + 任务级线程锁；后台任务用 FastAPI 的 ``BackgroundTasks``，
运行在线程池里，自己开 SQLite 连接。多 worker 的注意事项见 docs/known-limitations.md。
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Annotated

from fastapi import BackgroundTasks, FastAPI, File, Form, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from .. import __version__
from ..errors import ConfigError, InputError
from ..ingest import read_table
from ..logging_config import setup_logging
from ..models import SourceName
from ..review.client import LLMSettings, OpenAICompatibleClient, StubClient
from ..review.demo import demo_verdict
from ..settings import AppSettings
from ..workflow import (
    ClientProvider,
    JobNotFound,
    WorkflowError,
    WorkflowStatus,
    compute_job_id,
    connect,
    create_job,
    get_status,
    list_jobs,
    record_decision,
    resume_job,
    run_job,
)
from ..workflow import repository as repo
from ..workflow.service import ReviewSettings
from .locks import JobLockRegistry
from .models import ArtifactInfo, DecisionIn, HealthOut, JobAccepted, JobListOut

LOGGER = logging.getLogger("recon_workbench.api")

ALLOWED_SUFFIXES = {".csv", ".xlsx", ".xlsm"}
MEDIA_TYPES = {
    "recon_report": "application/json",
    "review_report": "application/json",
    "recon_result": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
EXTENSIONS = {"recon_report": "json", "review_report": "json", "recon_result": "xlsx"}


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def create_app(settings: AppSettings | None = None, *, configure_logging: bool = True) -> FastAPI:
    active = settings or AppSettings.from_env()
    active.ensure_dirs()
    if configure_logging:
        setup_logging(active.log_level, active.log_json)

    application = FastAPI(
        title="对账工作台 API",
        version=__version__,
        summary="上传台账与对账单，异步对账、LLM 复核、人工决策与结果下载",
        description=(
            "所有数据均为虚构演示数据；本服务不接真实银行/ERP，不执行任何资金操作。\n\n"
            "上传接口立即返回 job_id，对账与复核在后台线程执行；用 GET /api/jobs/{job_id} 轮询状态。"
        ),
    )
    application.state.settings = active
    application.state.locks = JobLockRegistry()

    _register_error_handlers(application)
    _register_middleware(application)
    _register_routes(application)
    return application


# ---------------------------------------------------------------- errors


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"code": code, "message": message})


def _register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def handle_api_error(request: Request, exc: ApiError) -> JSONResponse:
        return _error(exc.status_code, exc.code, exc.message)

    @app.exception_handler(JobNotFound)
    async def handle_not_found(request: Request, exc: JobNotFound) -> JSONResponse:
        return _error(404, "JOB_NOT_FOUND", str(exc))

    @app.exception_handler(InputError)
    async def handle_input_error(request: Request, exc: InputError) -> JSONResponse:
        return _error(400, "INPUT_ERROR", str(exc))

    @app.exception_handler(WorkflowError)
    async def handle_workflow_error(request: Request, exc: WorkflowError) -> JSONResponse:
        return _error(400, "WORKFLOW_ERROR", str(exc))

    @app.exception_handler(ConfigError)
    async def handle_config_error(request: Request, exc: ConfigError) -> JSONResponse:
        return _error(503, "CONFIG_ERROR", str(exc))

    @app.exception_handler(sqlite3.Error)
    async def handle_db_error(request: Request, exc: sqlite3.Error) -> JSONResponse:
        LOGGER.exception("database_error")
        return _error(503, "DATABASE_ERROR", "数据库暂时不可用")

    @app.exception_handler(RequestValidationError)
    async def handle_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        details = [
            {"loc": [str(part) for part in error.get("loc", [])], "msg": str(error.get("msg", ""))}
            for error in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={"code": "VALIDATION_ERROR", "message": "请求参数不合法", "errors": details},
        )


def _register_middleware(app: FastAPI) -> None:
    @app.middleware("http")
    async def access_log(request: Request, call_next):
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            duration = (time.perf_counter() - started) * 1000
            LOGGER.exception(
                "request_failed",
                extra={
                    "request_id": request_id,
                    "method": request.method,
                    "path": request.url.path,
                    "duration_ms": round(duration, 1),
                },
            )
            raise
        duration = (time.perf_counter() - started) * 1000
        response.headers["x-request-id"] = request_id
        LOGGER.info(
            "request",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "duration_ms": round(duration, 1),
            },
        )
        return response


# ---------------------------------------------------------------- helpers


def _settings(request: Request) -> AppSettings:
    return request.app.state.settings


def _locks(request: Request) -> JobLockRegistry:
    return request.app.state.locks


def _write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


async def _read_upload(upload: UploadFile, limit: int) -> tuple[bytes, str]:
    suffix = Path(upload.filename or "").suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise ApiError(400, "UNSUPPORTED_FILE_TYPE", f"只接受 {' / '.join(sorted(ALLOWED_SUFFIXES))}")
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise ApiError(413, "FILE_TOO_LARGE", f"文件超过上限 {limit} 字节")
        chunks.append(chunk)
    if total == 0:
        raise ApiError(400, "EMPTY_FILE", "文件为空")
    return b"".join(chunks), suffix


def _parse_review(value: str | bool | None) -> bool | None:
    if value is None or isinstance(value, bool):
        return value
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ApiError(422, "VALIDATION_ERROR", "review 只能是 true 或 false")


def _resolve_review(settings: AppSettings, override: bool | None) -> ReviewSettings:
    if override is False:
        return ReviewSettings(enabled=False)
    return settings.review_settings()


def _provider_for_job(connection: sqlite3.Connection, job_id: str) -> ClientProvider | None:
    status = get_status(connection, job_id)
    if not status.review_enabled:
        return None
    if status.review_provider == "stub":
        return lambda requests: StubClient([demo_verdict(request) for request in requests])

    def provider(requests):
        return OpenAICompatibleClient(LLMSettings.from_env())

    return provider


def _process_job(settings: AppSettings, locks: JobLockRegistry, job_id: str, *, resume: bool) -> None:
    """后台任务：拿锁 → 推进阶段 → 记日志。异常只记录，不把线程打挂。"""

    with locks.hold(job_id) as acquired:
        if not acquired:
            LOGGER.info("job_skipped_locked", extra={"job_id": job_id})
            return
        connection: sqlite3.Connection | None = None
        try:
            connection = connect(settings.db_path)
            provider = _provider_for_job(connection, job_id)
            if resume:
                status = resume_job(
                    connection,
                    job_id,
                    storage_dir=settings.storage_dir,
                    client_provider=provider,
                )
            else:
                status = run_job(
                    connection,
                    job_id,
                    storage_dir=settings.storage_dir,
                    client_provider=provider,
                )
            LOGGER.info(
                "job_processed",
                extra={"job_id": job_id, "status": status.status, "pending": status.pending_count},
            )
        except Exception:
            LOGGER.exception("job_processing_failed", extra={"job_id": job_id})
        finally:
            if connection is not None:
                connection.close()


def _job_artifacts(connection: sqlite3.Connection, job_id: str) -> list[ArtifactInfo]:
    return [
        ArtifactInfo(
            kind=row["kind"],
            filename=Path(row["path"]).name,
            bytes=row["bytes"],
            sha256=row["sha256"],
            download_url=f"/api/jobs/{job_id}/download/{row['kind']}",
        )
        for row in repo.list_artifacts(connection, job_id)
    ]


# ---------------------------------------------------------------- routes


def _register_routes(app: FastAPI) -> None:
    @app.get("/healthz", response_model=HealthOut, tags=["ops"])
    def healthz(request: Request) -> HealthOut:
        settings = _settings(request)
        connection = connect(settings.db_path)
        try:
            connection.execute("SELECT 1")
        finally:
            connection.close()
        return HealthOut(status="ok", version=__version__, review_provider=settings.review_provider)

    @app.post("/api/jobs", response_model=JobAccepted, status_code=202, tags=["jobs"])
    async def upload_job(
        request: Request,
        background: BackgroundTasks,
        ledger: Annotated[UploadFile, File(description="内部应付台账 CSV/XLSX")],
        statement: Annotated[UploadFile, File(description="供应商对账单 CSV/XLSX")],
        review: str | None = Form(default=None, description="是否执行 LLM 复核（true/false），缺省用服务配置"),
    ) -> JobAccepted:
        settings = _settings(request)
        locks = _locks(request)

        review_settings = _resolve_review(settings, _parse_review(review))
        policy = settings.policy()

        ledger_bytes, ledger_suffix = await _read_upload(ledger, settings.max_upload_bytes)
        statement_bytes, statement_suffix = await _read_upload(statement, settings.max_upload_bytes)

        job_id = compute_job_id(
            hashlib.sha256(ledger_bytes).hexdigest(),
            hashlib.sha256(statement_bytes).hexdigest(),
            policy,
            review_settings,
        )
        job_dir = settings.upload_dir / job_id
        ledger_path = job_dir / f"ledger{ledger_suffix}"
        statement_path = job_dir / f"statement{statement_suffix}"
        await run_in_threadpool(_write_bytes, ledger_path, ledger_bytes)
        await run_in_threadpool(_write_bytes, statement_path, statement_bytes)

        try:
            ledger_table = await run_in_threadpool(read_table, ledger_path, SourceName.LEDGER)
            statement_table = await run_in_threadpool(read_table, statement_path, SourceName.STATEMENT)
        except InputError as exc:
            for path in (ledger_path, statement_path):
                path.unlink(missing_ok=True)
            raise ApiError(400, "INVALID_INPUT", str(exc)) from exc

        created = False
        status: WorkflowStatus
        connection = connect(settings.db_path)
        try:
            existing = repo.get_job(connection, job_id) is not None
            job_id = create_job(
                connection,
                ledger_path=ledger_path,
                statement_path=statement_path,
                ledger_table=ledger_table,
                statement_table=statement_table,
                policy=policy,
                review=review_settings,
            )
            status = get_status(connection, job_id)
            created = not existing
        finally:
            connection.close()

        if status.status not in {"running", "awaiting_human", "completed"}:
            should_resume = status.status == "failed"
            background.add_task(_process_job, settings, locks, job_id, resume=should_resume)

        return JobAccepted(
            job_id=job_id,
            status=status.status,
            created=created,
            detail_url=f"/api/jobs/{job_id}",
        )

    @app.get("/api/jobs", response_model=JobListOut, tags=["jobs"])
    def list_jobs_endpoint(
        request: Request,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> JobListOut:
        connection = connect(_settings(request).db_path)
        try:
            items = list_jobs(connection, limit, offset)
            total = repo.count_jobs(connection)
        finally:
            connection.close()
        return JobListOut(items=tuple(items), total=total, limit=limit, offset=offset)

    @app.get("/api/jobs/{job_id}", response_model=WorkflowStatus, tags=["jobs"])
    def get_job_endpoint(request: Request, job_id: str) -> WorkflowStatus:
        connection = connect(_settings(request).db_path)
        try:
            return get_status(connection, job_id)
        finally:
            connection.close()

    @app.get("/api/jobs/{job_id}/artifacts", response_model=list[ArtifactInfo], tags=["jobs"])
    def list_artifacts_endpoint(request: Request, job_id: str) -> list[ArtifactInfo]:
        connection = connect(_settings(request).db_path)
        try:
            get_status(connection, job_id)  # 任务不存在 -> 404
            return _job_artifacts(connection, job_id)
        finally:
            connection.close()

    @app.get("/api/jobs/{job_id}/download/{kind}", tags=["jobs"])
    def download_endpoint(request: Request, job_id: str, kind: str) -> FileResponse:
        if kind not in MEDIA_TYPES:
            raise ApiError(404, "ARTIFACT_NOT_FOUND", f"未知的产物类型：{kind}")

        settings = _settings(request)
        connection = connect(settings.db_path)
        try:
            get_status(connection, job_id)
            row = repo.get_artifact(connection, job_id, kind)
        finally:
            connection.close()

        if row is None:
            raise ApiError(404, "ARTIFACT_NOT_FOUND", f"任务 {job_id} 还没有 {kind} 产物")

        path = Path(row["path"]).resolve()
        storage_root = settings.storage_dir.resolve()
        if not path.is_relative_to(storage_root):
            raise ApiError(403, "ARTIFACT_FORBIDDEN", "产物路径不在允许的目录内")
        if not path.is_file():
            raise ApiError(404, "ARTIFACT_MISSING", "产物文件已不存在，请重新执行任务")

        return FileResponse(
            path,
            media_type=MEDIA_TYPES[kind],
            filename=f"{job_id}-{kind}.{EXTENSIONS[kind]}",
        )

    @app.post("/api/jobs/{job_id}/decisions", response_model=WorkflowStatus, tags=["jobs"])
    def decide_endpoint(request: Request, job_id: str, payload: DecisionIn) -> WorkflowStatus:
        if _locks(request).is_busy(job_id):
            raise ApiError(409, "JOB_BUSY", "任务正在执行，请稍后再提交决策")

        connection = connect(_settings(request).db_path)
        try:
            return record_decision(
                connection,
                job_id=job_id,
                item_id=payload.item_id,
                decision=payload.decision.value,
                decided_by=payload.decided_by,
                note=payload.note,
            )
        finally:
            connection.close()

    @app.post("/api/jobs/{job_id}/resume", response_model=JobAccepted, status_code=202, tags=["jobs"])
    def resume_endpoint(request: Request, background: BackgroundTasks, job_id: str) -> JobAccepted:
        settings = _settings(request)
        locks = _locks(request)
        if locks.is_busy(job_id):
            raise ApiError(409, "JOB_BUSY", "任务正在执行，请勿重复恢复")

        connection = connect(settings.db_path)
        try:
            status = get_status(connection, job_id)
        finally:
            connection.close()

        if status.status != "completed":
            background.add_task(_process_job, settings, locks, job_id, resume=True)

        return JobAccepted(
            job_id=job_id,
            status=status.status,
            created=False,
            detail_url=f"/api/jobs/{job_id}",
        )
