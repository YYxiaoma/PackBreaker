from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect

from backend.app.application.task_recovery import TaskRecoveryCoordinator
from backend.app.config import AppSettings
from backend.app.domain.task_state import TaskStatus
from backend.app.infrastructure.persistence.models import UnpackTask, new_uuid, utc_now
from backend.app.infrastructure.runtime import InstanceLockUnavailable, RuntimeManager
from backend.app.main import create_app


def _settings(tmp_path: Path) -> AppSettings:
    return AppSettings(
        config_dir=(tmp_path / "config").resolve(),
        data_dir=(tmp_path / "data").resolve(),
        secret_key_file=None,
    )


def test_runtime_migrates_database_and_becomes_ready(tmp_path: Path) -> None:
    runtime = RuntimeManager(_settings(tmp_path))
    runtime.start()
    try:
        report = runtime.readiness()
        assert report.ready
        assert report.database == "ok"
        assert report.migrations == "ok"
        assert report.secrets == "ok"
        assert report.worker_slot == "ok"
        assert report.current_revision == report.expected_revision == "0013_task_action_receipt"
        assert runtime.engine is not None
        assert {"unpack_task", "task_event", "operation_journal"}.issubset(
            set(inspect(runtime.engine).get_table_names())
        )
    finally:
        runtime.stop()


def test_second_runtime_cannot_share_same_config_directory(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    first = RuntimeManager(settings)
    second = RuntimeManager(settings)
    first.start()
    try:
        with pytest.raises(InstanceLockUnavailable):
            second.start()
    finally:
        first.stop()


def test_ready_endpoint_requires_started_runtime(tmp_path: Path) -> None:
    app = create_app(settings=_settings(tmp_path))
    client = TestClient(app)

    response = client.get("/api/v1/health/ready")

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "5"
    assert response.json()["status"] == "not_ready"


def test_ready_endpoint_is_healthy_inside_lifespan(tmp_path: Path) -> None:
    app = create_app(settings=_settings(tmp_path))

    with TestClient(app) as client:
        response = client.get("/api/v1/health/ready")
        recovery_report = app.state.task_recovery_report
        assert app.state.task_driver.running is True

    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert response.json()["checks"] == {
        "database": "ok",
        "migrations": "ok",
        "secrets": "ok",
        "worker_slot": "ok",
    }
    assert recovery_report.scanned_count == 0
    assert recovery_report.blocked_count == 0
    assert recovery_report.truncated is False
    assert app.state.task_driver.running is False
    assert not app.state.runtime.started


def test_startup_recovery_blocked_task_does_not_fail_readiness(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    runtime = RuntimeManager(settings)
    runtime.start()
    try:
        now = utc_now()
        with runtime.session_factory() as session:
            session.add(
                UnpackTask(
                    id=new_uuid(),
                    type="PACKAGE_UNPACK",
                    source_downloader_id="source-downloader",
                    source_hash="blocked-source",
                    normalized_unit_key="blocked-unit",
                    idempotency_key="b" * 64,
                    status=TaskStatus.ADDING.value,
                    trace_id=new_uuid(),
                    checkpoint={
                        "execution_plan_id": "missing-plan",
                        "execution_plan_digest": "c" * 64,
                    },
                    error_code=None,
                    version=1,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()
    finally:
        runtime.stop()

    app = create_app(settings=settings)
    with TestClient(app) as client:
        response = client.get("/api/v1/health/ready")
        report = app.state.task_recovery_report

    assert response.status_code == 200
    assert report.scanned_count == 1
    assert report.blocked_count == 1
    assert report.items[0].error_code == "RECOVERY_PLAN_MISMATCH"


def test_startup_recovery_unexpected_error_releases_runtime_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)

    async def fail_recovery(
        self: TaskRecoveryCoordinator,
        *,
        limit: int = 100,
        max_steps_per_task: int = 4,
        recover_abandoned_analysis: bool = False,
    ) -> object:
        del self, limit, max_steps_per_task
        assert recover_abandoned_analysis is True
        raise RuntimeError("synthetic recovery bug")

    monkeypatch.setattr(TaskRecoveryCoordinator, "reconcile_once", fail_recovery)
    app = create_app(settings=settings)
    with pytest.raises(RuntimeError, match="synthetic recovery bug"), TestClient(app):
        pass

    probe = RuntimeManager(settings)
    probe.start()
    probe.stop()
