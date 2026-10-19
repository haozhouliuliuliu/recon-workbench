"""HTTP 接口测试：上传、状态、下载、列表、决策、恢复、错误码。

全部走 TestClient，不启动真实端口、不联网、不烧 LLM key（默认 stub）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from recon_workbench.api import create_app
from recon_workbench.settings import AppSettings
from recon_workbench.workflow import connect

SAMPLE_DIR = Path(__file__).resolve().parents[1] / "sample_data"
LEDGER = SAMPLE_DIR / "ledger_2026_08.csv"
STATEMENT = SAMPLE_DIR / "statement_2026_08.xlsx"


def _settings(tmp_path: Path, **overrides) -> AppSettings:
    payload = {
        "db_path": tmp_path / "recon.db",
        "storage_dir": tmp_path / "jobs",
        "upload_dir": tmp_path / "uploads",
        "review_provider": "stub",
        "log_level": "WARNING",
    }
    payload.update(overrides)
    return AppSettings(**payload)


@pytest.fixture
def settings(tmp_path: Path) -> AppSettings:
    return _settings(tmp_path)


@pytest.fixture
def client(settings: AppSettings):
    app = create_app(settings, configure_logging=False)
    with TestClient(app) as test_client:
        yield test_client


def _upload(client: TestClient, *, ledger: Path = LEDGER, statement: Path = STATEMENT, review: bool | None = None):
    with ledger.open("rb") as ledger_handle, statement.open("rb") as statement_handle:
        files = {
            "ledger": (ledger.name, ledger_handle, "text/csv"),
            "statement": (
                statement.name,
                statement_handle,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ),
        }
        data = {} if review is None else {"review": "true" if review else "false"}
        return client.post("/api/jobs", files=files, data=data)


def _job_id(client: TestClient) -> str:
    return client.get("/api/jobs").json()["items"][0]["job_id"]


def test_health(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["review_provider"] == "stub"


def test_upload_creates_and_processes_job(client: TestClient) -> None:
    response = _upload(client)
    assert response.status_code == 202
    payload = response.json()
    assert payload["created"] is True
    assert payload["job_id"].startswith("J")

    status = client.get(f"/api/jobs/{payload['job_id']}").json()
    assert status["status"] == "awaiting_human"
    assert status["counts"]["matched"] == 8
    assert len(status["suggestions"]) == 4
    assert len(status["pending"]) == 4


def test_upload_same_files_reuses_job(client: TestClient) -> None:
    first = _upload(client).json()
    second = _upload(client).json()
    assert second["job_id"] == first["job_id"]
    assert second["created"] is False
    assert client.get("/api/jobs").json()["total"] == 1


def test_upload_invalid_headers_returns_400(client: TestClient, tmp_path: Path) -> None:
    bad = tmp_path / "bad.csv"
    bad.write_text("随便,写点,什么\n1,2,3\n", encoding="utf-8")
    response = _upload(client, ledger=bad)
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_INPUT"
    assert client.get("/api/jobs").json()["total"] == 0


def test_upload_unsupported_suffix_returns_400(client: TestClient, tmp_path: Path) -> None:
    bad = tmp_path / "ledger.txt"
    bad.write_text("hello", encoding="utf-8")
    response = _upload(client, ledger=bad)
    assert response.status_code == 400
    assert response.json()["code"] == "UNSUPPORTED_FILE_TYPE"


def test_upload_too_large_returns_413(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path, max_upload_bytes=100), configure_logging=False)
    with TestClient(app) as client:
        response = _upload(client)
    assert response.status_code == 413
    assert response.json()["code"] == "FILE_TOO_LARGE"


def test_unknown_job_returns_404(client: TestClient) -> None:
    response = client.get("/api/jobs/J0000000000000000")
    assert response.status_code == 404
    assert response.json()["code"] == "JOB_NOT_FOUND"


def test_list_jobs_pagination(client: TestClient) -> None:
    _upload(client)
    first = client.get("/api/jobs", params={"limit": 1, "offset": 0}).json()
    assert first["total"] == 1 and len(first["items"]) == 1
    empty = client.get("/api/jobs", params={"limit": 1, "offset": 5}).json()
    assert empty["items"] == []


def test_artifacts_are_listed(client: TestClient) -> None:
    _upload(client)
    job_id = _job_id(client)
    artifacts = client.get(f"/api/jobs/{job_id}/artifacts").json()
    assert {item["kind"] for item in artifacts} == {"recon_report", "recon_result", "review_report"}
    assert all(item["download_url"].startswith(f"/api/jobs/{job_id}/download/") for item in artifacts)


def test_download_report_and_excel(client: TestClient) -> None:
    _upload(client)
    job_id = _job_id(client)

    report = client.get(f"/api/jobs/{job_id}/download/recon_report")
    assert report.status_code == 200
    assert report.headers["content-type"].startswith("application/json")
    assert "attachment" in report.headers["content-disposition"]
    assert json.loads(report.content)["digest"] == client.get(f"/api/jobs/{job_id}").json()["reconcile_digest"]

    excel = client.get(f"/api/jobs/{job_id}/download/recon_result")
    assert excel.status_code == 200
    assert excel.content[:2] == b"PK"


def test_download_unknown_kind_returns_404(client: TestClient) -> None:
    _upload(client)
    job_id = _job_id(client)
    response = client.get(f"/api/jobs/{job_id}/download/whatever")
    assert response.status_code == 404
    assert response.json()["code"] == "ARTIFACT_NOT_FOUND"


def test_download_path_outside_storage_is_forbidden(client: TestClient, settings: AppSettings, tmp_path: Path) -> None:
    _upload(client)
    job_id = _job_id(client)
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")

    connection = connect(settings.db_path)
    try:
        connection.execute(
            "UPDATE artifacts SET path = ? WHERE job_id = ? AND kind = 'recon_report'", (str(outside), job_id)
        )
    finally:
        connection.close()

    response = client.get(f"/api/jobs/{job_id}/download/recon_report")
    assert response.status_code == 403
    assert response.json()["code"] == "ARTIFACT_FORBIDDEN"


def test_decision_flow_completes_job(client: TestClient) -> None:
    _upload(client)
    job_id = _job_id(client)
    status = client.get(f"/api/jobs/{job_id}").json()
    for item in status["pending"]:
        response = client.post(
            f"/api/jobs/{job_id}/decisions",
            json={"item_id": item["item_id"], "decision": "accepted", "decided_by": "张会计", "note": "API"},
        )
        assert response.status_code == 200
    final = client.get(f"/api/jobs/{job_id}").json()
    assert final["status"] == "completed"
    assert len(final["decisions"]) == 4


def test_decision_on_matched_item_returns_400(client: TestClient) -> None:
    _upload(client)
    job_id = _job_id(client)
    matched = next(item for item in client.get(f"/api/jobs/{job_id}").json()["items"] if item["status"] == "matched")
    response = client.post(
        f"/api/jobs/{job_id}/decisions",
        json={"item_id": matched["item_id"], "decision": "accepted", "decided_by": "张会计"},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "WORKFLOW_ERROR"


def test_decision_payload_validation(client: TestClient) -> None:
    _upload(client)
    job_id = _job_id(client)
    response = client.post(f"/api/jobs/{job_id}/decisions", json={"decision": "accepted", "decided_by": "x"})
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


def test_review_can_be_disabled_per_upload(client: TestClient) -> None:
    response = _upload(client, review=False)
    assert response.status_code == 202
    job_id = response.json()["job_id"]
    status = client.get(f"/api/jobs/{job_id}").json()
    assert status["suggestions"] == []
    assert status["status"] == "awaiting_human"


def test_resume_completed_job_is_noop(client: TestClient) -> None:
    _upload(client)
    job_id = _job_id(client)
    status = client.get(f"/api/jobs/{job_id}").json()
    for item in status["pending"]:
        client.post(
            f"/api/jobs/{job_id}/decisions",
            json={"item_id": item["item_id"], "decision": "accepted", "decided_by": "张会计"},
        )
    response = client.post(f"/api/jobs/{job_id}/resume")
    assert response.status_code == 202
    assert response.json()["status"] == "completed"


def test_resume_busy_returns_409(client: TestClient) -> None:
    _upload(client)
    job_id = _job_id(client)
    locks = client.app.state.locks
    assert locks.try_acquire(job_id)
    try:
        response = client.post(f"/api/jobs/{job_id}/resume")
    finally:
        locks.release(job_id)
    assert response.status_code == 409
    assert response.json()["code"] == "JOB_BUSY"


def test_request_id_header(client: TestClient) -> None:
    response = client.get("/healthz", headers={"x-request-id": "abc123"})
    assert response.headers["x-request-id"] == "abc123"
    generated = client.get("/healthz").headers["x-request-id"]
    assert len(generated) == 12


def test_openai_provider_without_key_returns_503(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("LLM_API_BASE", "LLM_API_KEY", "LLM_MODEL"):
        monkeypatch.delenv(name, raising=False)
    app = create_app(_settings(tmp_path, review_provider="openai"), configure_logging=False)
    with TestClient(app) as client:
        response = _upload(client)
    assert response.status_code == 503
    assert response.json()["code"] == "CONFIG_ERROR"
