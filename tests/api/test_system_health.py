from __future__ import annotations

import hashlib
import io
import json
import logging
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.config import AppSettings
from backend.app.domain.operation import OperationStatus
from backend.app.domain.task_state import TaskStatus
from backend.app.infrastructure.app_logging import JsonLogFormatter
from backend.app.infrastructure.persistence.models import (
    Downloader,
    OperationJournal,
    Site,
    TaskExecution,
    TaskExecutionEvent,
    UnpackTask,
    new_uuid,
)
from backend.app.main import create_app

_PASSWORD = "synthetic correct horse battery staple"
_CANARY_SITE_URL = "https://secret-canary.example.invalid"
_CANARY_DOWNLOADER_URL = "http://user:password@downloader.invalid:8080"
_CANARY_PATH = "/synthetic/private/media/secret.mkv"
_CANARY_HASH = "0123456789abcdef0123456789abcdef01234567"
_CANARY_TOKEN = "api_key=synthetic-super-secret-token"


def _settings(tmp_path: Path) -> AppSettings:
    config = (tmp_path / "config").resolve()
    data = (tmp_path / "data").resolve()
    config.mkdir(mode=0o700)
    data.mkdir()
    return AppSettings(config_dir=config, data_dir=data)


def _authenticated_client(tmp_path: Path) -> tuple[TestClient, FastAPI]:
    app = create_app(settings=_settings(tmp_path))
    client = TestClient(app, base_url="https://testserver")
    client.__enter__()
    setup = client.post("/api/v1/auth/setup", json={"username": "admin", "password": _PASSWORD})
    assert setup.status_code == 201
    login = client.post("/api/v1/auth/login", json={"username": "admin", "password": _PASSWORD})
    assert login.status_code == 200
    return client, app


def _seed_sensitive_health_evidence(app: FastAPI) -> str:
    runtime = app.state.runtime
    now = datetime.now(UTC)
    task_id = new_uuid()
    with runtime.session_factory() as session:
        session.add(
            Site(
                id=new_uuid(),
                name="secret site name",
                type="MTEAM",
                base_url=_CANARY_SITE_URL,
                credential_kind="API_KEY",
                secret_id=None,
                capabilities={"debug": _CANARY_TOKEN},
                connection_status="FAILED",
                enabled=True,
                version=1,
                last_test_at=now - timedelta(minutes=5),
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            Downloader(
                id=new_uuid(),
                name="secret downloader name",
                type="QBITTORRENT",
                base_url=_CANARY_DOWNLOADER_URL,
                secret_id=None,
                monitor_rules={"secret": _CANARY_TOKEN},
                path_mappings=[
                    {"remote_prefix": "/remote/private", "container_prefix": _CANARY_PATH}
                ],
                capabilities={"version": "synthetic"},
                connection_status="FAILED",
                path_mapping_status="FAILED",
                enabled=True,
                version=1,
                last_test_at=now - timedelta(minutes=5),
                last_path_diagnostic_at=now - timedelta(minutes=5),
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            UnpackTask(
                id=task_id,
                type="PACKAGE_UNPACK",
                source_downloader_id="synthetic-source-downloader",
                source_hash=_CANARY_HASH,
                normalized_unit_key="secret/private/unit",
                idempotency_key="a" * 64,
                parent_task_id=None,
                run_number=1,
                status=TaskStatus.RETRY.value,
                trace_id=new_uuid(),
                checkpoint={"path": _CANARY_PATH, "credential": _CANARY_TOKEN},
                error_code="SYNTHETIC_RETRY",
                version=1,
                created_at=now - timedelta(days=2),
                updated_at=now - timedelta(days=2),
            )
        )
        session.flush()
        session.add(
            OperationJournal(
                id=new_uuid(),
                task_id=task_id,
                idempotency_key="b" * 64,
                operation_type="QBITTORRENT_ADD",
                target={"path": _CANARY_PATH, "hash": _CANARY_HASH},
                intent={"authorization": _CANARY_TOKEN, "url": _CANARY_DOWNLOADER_URL},
                status=OperationStatus.RECONCILE_REQUIRED.value,
                before_snapshot=None,
                after_snapshot=None,
                created_at=now - timedelta(hours=2),
                updated_at=now - timedelta(hours=2),
            )
        )
        session.commit()
    return task_id


def _write_operational_log_canary(app: FastAPI) -> tuple[str, str]:
    canary = "PACKBREAKER-API-LOG-CANARY-91ef"
    trace_id = "trace-safe-operational-log"
    record = logging.LogRecord(
        name="packbreaker.api.canary",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg=(
            f"token={canary} "
            f"https://user:{canary}@example.invalid/private/{canary}?api_key={canary}"
        ),
        args=(),
        exc_info=None,
    )
    record.fields = {"authorization": canary, "trace_id": trace_id, "status_code": 503}
    rendered = JsonLogFormatter().format(record)
    log_dir = app.state.runtime.settings.log_dir
    log_dir.mkdir(mode=0o700)
    log_file = log_dir / "packbreaker.jsonl"
    log_file.write_text(rendered + "\n", encoding="utf-8")
    log_file.chmod(0o600)
    return canary, trace_id


def test_release_preflight_api_is_local_read_only_and_skips_backup_exercise(
    tmp_path: Path,
) -> None:
    client, app = _authenticated_client(tmp_path)
    media = app.state.runtime.settings.data_dir / "preflight-api-canary.mkv"
    media.write_bytes(b"synthetic-preflight-api-canary")
    before = media.stat()
    try:
        response = client.get("/api/v1/system/release/preflight")

        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        payload = response.json()
        assert payload["status"] == "ready"
        assert payload["app_version"] == "0.1.8"
        codes = {item["code"] for item in payload["checks"]}
        assert {
            "CONFIG_DIR_OK",
            "DATABASE_OK",
            "SECRET_KEY_OK",
            "DATA_ROOT_OK",
            "BACKUP_EXERCISE_SKIPPED",
        } <= codes
        assert not (app.state.runtime.settings.config_dir / "backups" / "preflight").exists()
        after = media.stat()
        assert media.read_bytes() == b"synthetic-preflight-api-canary"
        assert (after.st_ino, after.st_size, after.st_mtime_ns) == (
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        )
    finally:
        client.__exit__(None, None, None)


def test_system_health_aggregates_existing_evidence_without_exposing_identifiers(
    tmp_path: Path,
) -> None:
    client, app = _authenticated_client(tmp_path)
    try:
        task_id = _seed_sensitive_health_evidence(app)

        response = client.get("/api/v1/system/health")

        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "warning"
        checks = {item["name"]: item for item in payload["checks"]}
        assert checks["sites"]["metrics"]["connection_failed"] == 1
        assert checks["downloaders"]["metrics"]["connection_failed"] == 1
        assert checks["tasks"]["metrics"]["retry"] == 1
        assert checks["tasks"]["metrics"]["stale_active"] == 1
        assert checks["operations"]["metrics"]["reconcile_required"] == 1
        assert checks["operations"]["metrics"]["stale_unsettled"] == 1
        assert checks["backups"]["code"] == "BACKUP_MISSING"

        encoded = response.text
        for canary in (
            _CANARY_SITE_URL,
            _CANARY_DOWNLOADER_URL,
            _CANARY_PATH,
            _CANARY_HASH,
            _CANARY_TOKEN,
            "secret site name",
            "secret downloader name",
            task_id,
        ):
            assert canary not in encoded
    finally:
        client.__exit__(None, None, None)


def test_diagnostic_export_contains_only_whitelisted_aggregate_health(tmp_path: Path) -> None:
    client, app = _authenticated_client(tmp_path)
    try:
        task_id = _seed_sensitive_health_evidence(app)

        response = client.get("/api/v1/system/diagnostics/export")

        assert response.status_code == 200
        assert response.headers["content-type"] == "application/zip"
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-sha256"] == hashlib.sha256(response.content).hexdigest()
        assert "packbreaker-diagnostics-" in response.headers["content-disposition"]

        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            assert set(archive.namelist()) == {"health.json", "manifest.json"}
            health_bytes = archive.read("health.json")
            manifest_bytes = archive.read("manifest.json")
            manifest = json.loads(manifest_bytes)
            assert manifest["files"] == [
                {
                    "name": "health.json",
                    "sha256": hashlib.sha256(health_bytes).hexdigest(),
                }
            ]
            assert manifest["privacy"] == {
                "contains_credentials": False,
                "contains_logs": False,
                "contains_media_content": False,
                "contains_paths": False,
                "contains_task_ids": False,
                "contains_torrent_or_source_hashes": False,
                "contains_urls": False,
            }
            combined = health_bytes + b"\n" + manifest_bytes
            for canary in (
                _CANARY_SITE_URL,
                _CANARY_DOWNLOADER_URL,
                _CANARY_PATH,
                _CANARY_HASH,
                _CANARY_TOKEN,
                "secret site name",
                "secret downloader name",
                task_id,
            ):
                assert canary.encode() not in combined
    finally:
        client.__exit__(None, None, None)


def test_operational_log_query_and_export_are_bounded_and_redacted(tmp_path: Path) -> None:
    client, app = _authenticated_client(tmp_path)
    try:
        canary, trace_id = _write_operational_log_canary(app)

        response = client.get(
            "/api/v1/system/logs",
            params={"window_minutes": 60, "limit": 20, "q": trace_id},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["count"] == 1
        assert payload["truncated"] is False
        assert payload["window_minutes"] == 60
        assert payload["limit"] == 20
        assert payload["max_file_bytes"] == app.state.runtime.settings.log_file_max_bytes
        assert payload["backup_count"] == app.state.runtime.settings.log_file_backup_count
        encoded = response.text
        assert trace_id in encoded
        assert canary not in encoded
        assert "user:" not in encoded
        assert "/private/" not in encoded
        assert "https://example.invalid" in encoded
        assert "[REDACTED]" in encoded

        exported = client.get(
            "/api/v1/system/logs/export",
            params={"window_minutes": 60, "limit": 50, "q": trace_id},
        )
        assert exported.status_code == 200
        assert exported.headers["content-type"] == "application/json"
        assert exported.headers["cache-control"] == "no-store"
        assert exported.headers["x-content-sha256"] == hashlib.sha256(exported.content).hexdigest()
        assert "packbreaker-logs-" in exported.headers["content-disposition"]
        assert trace_id.encode() in exported.content
        assert canary.encode() not in exported.content

        assert (
            client.get("/api/v1/system/logs", params={"window_minutes": 10081}).status_code == 422
        )
        assert client.get("/api/v1/system/logs", params={"limit": 501}).status_code == 422
        assert client.get("/api/v1/system/logs/export", params={"limit": 2001}).status_code == 422
    finally:
        client.__exit__(None, None, None)


def test_system_logs_include_filterable_task_execution_events(tmp_path: Path) -> None:
    client, app = _authenticated_client(tmp_path)
    try:
        now = datetime.now(UTC)
        definition_id = new_uuid()
        execution_id = new_uuid()
        trace_id = new_uuid()
        with app.state.runtime.session_factory() as session:
            session.add(
                TaskExecution(
                    id=execution_id,
                    task_definition_id=None,
                    task_name="阶段 G 日志任务",
                    trigger="MANUAL",
                    status="PENDING",
                    phase="WAITING",
                    source_execution_id=None,
                    trace_id=trace_id,
                    config_snapshot={"task_definition_id": definition_id},
                    discovered_count=1,
                    success_count=0,
                    failed_count=0,
                    skipped_count=0,
                    started_at=now,
                    finished_at=None,
                    created_at=now,
                )
            )
            session.add(
                TaskExecutionEvent(
                    id=new_uuid(),
                    execution_id=execution_id,
                    event_code="TASK_UNPACK_RUN_MATERIALIZED",
                    message="Safe unpack run created and waiting for preflight",
                    trace_id=trace_id,
                    context={
                        "task_id": definition_id,
                        "unpack_task_id": "synthetic-run-id",
                        "api_key": "must-be-redacted",
                    },
                    created_at=now,
                )
            )
            session.commit()

        response = client.get(
            "/api/v1/system/logs",
            params={
                "source": "TASK_EVENT",
                "execution_id": execution_id,
                "trace_id": trace_id,
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["count"] == 1
        entry = body["items"][0]
        assert entry["source"] == "TASK_EVENT"
        assert entry["event_code"] == "TASK_UNPACK_RUN_MATERIALIZED"
        assert entry["task_id"] == definition_id
        assert entry["execution_id"] == execution_id
        assert entry["trace_id"] == trace_id
        assert entry["task_name"] == "阶段 G 日志任务"
        assert entry["message"] == "Safe unpack run created and waiting for preflight"
        assert entry["fields"]["api_key"] == "[REDACTED]"

        exported = client.get(
            "/api/v1/system/logs/export",
            params={"source": "TASK_EVENT", "execution_id": execution_id},
        )
        assert exported.status_code == 200
        exported_body = json.loads(exported.content)
        assert exported_body["items"][0]["event_code"] == "TASK_UNPACK_RUN_MATERIALIZED"
        assert exported_body["items"][0]["source"] == "TASK_EVENT"
        assert b"must-be-redacted" not in exported.content
    finally:
        client.__exit__(None, None, None)
