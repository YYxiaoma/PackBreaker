from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect

from backend.app.config import AppSettings
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
        assert report.current_revision == report.expected_revision == "0009_task_review"
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

    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert response.json()["checks"] == {
        "database": "ok",
        "migrations": "ok",
        "secrets": "ok",
        "worker_slot": "ok",
    }
    assert not app.state.runtime.started
