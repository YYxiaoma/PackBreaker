from pathlib import Path

from fastapi.testclient import TestClient

from backend.app.api.dependencies import CSRF_COOKIE
from backend.app.config import AppSettings
from backend.app.main import create_app

_PASSWORD = "synthetic correct horse battery staple"


def _client(tmp_path: Path) -> TestClient:
    config = (tmp_path / "config").resolve()
    data = (tmp_path / "data").resolve()
    config.mkdir(mode=0o700)
    data.mkdir()
    settings = AppSettings(
        config_dir=config,
        data_dir=data,
        backup_driver_interval_seconds=3600,
    )
    client = TestClient(create_app(settings=settings), base_url="https://testserver")
    client.__enter__()
    assert (
        client.post(
            "/api/v1/auth/setup", json={"username": "admin", "password": _PASSWORD}
        ).status_code
        == 201
    )
    assert (
        client.post(
            "/api/v1/auth/login", json={"username": "admin", "password": _PASSWORD}
        ).status_code
        == 200
    )
    return client


def _csrf(client: TestClient) -> dict[str, str]:
    token = client.cookies.get(CSRF_COOKIE)
    assert token is not None
    return {"X-CSRF-Token": token}


def test_backup_policy_defaults_disabled_requires_csrf_and_strong_version(tmp_path: Path) -> None:
    client = _client(tmp_path)
    try:
        current = client.get("/api/v1/system/backups/policy")
        assert current.status_code == 200
        assert current.headers["etag"] == '"1"'
        assert current.headers["cache-control"] == "no-store"
        assert current.json()["enabled"] is False
        assert current.json()["interval_hours"] == 24
        assert current.json()["retention_days"] == 30
        assert current.json()["keep_latest"] == 3
        assert current.json()["driver_running"] is True

        payload = {
            "enabled": True,
            "interval_hours": 12,
            "retention_days": 14,
            "keep_latest": 4,
        }
        missing_csrf = client.put(
            "/api/v1/system/backups/policy",
            headers={"If-Match": '"1"'},
            json=payload,
        )
        assert missing_csrf.status_code == 403

        missing_version = client.put(
            "/api/v1/system/backups/policy",
            headers=_csrf(client),
            json=payload,
        )
        assert missing_version.status_code == 428
        assert missing_version.json()["code"] == "IF_MATCH_REQUIRED"

        updated = client.put(
            "/api/v1/system/backups/policy",
            headers={**_csrf(client), "If-Match": '"1"'},
            json=payload,
        )
        assert updated.status_code == 200
        assert updated.headers["etag"] == '"2"'
        assert updated.json()["enabled"] is True
        assert updated.json()["version"] == 2

        stale = client.put(
            "/api/v1/system/backups/policy",
            headers={**_csrf(client), "If-Match": '"1"'},
            json=payload,
        )
        assert stale.status_code == 412
        assert stale.json()["code"] == "BACKUP_POLICY_VERSION_CONFLICT"
    finally:
        client.__exit__(None, None, None)


def test_manual_backup_uses_same_driver_and_persists_success_without_version_bump(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    try:
        updated = client.put(
            "/api/v1/system/backups/policy",
            headers={**_csrf(client), "If-Match": '"1"'},
            json={
                "enabled": True,
                "interval_hours": 24,
                "retention_days": 30,
                "keep_latest": 3,
            },
        )
        assert updated.status_code == 200
        assert updated.json()["version"] == 2

        executed = client.post(
            "/api/v1/system/backups/actions",
            headers=_csrf(client),
            json={"action": "run_now"},
        )
        assert executed.status_code == 200
        result = executed.json()
        assert result["created"] is True
        assert result["skipped_reason"] is None
        assert result["database_file"].startswith("packbreaker-")
        assert result["database_file"].endswith(".db")
        assert result["database_size_bytes"] > 0

        backups = (tmp_path / "config" / "backups").resolve()
        assert len(list(backups.glob("packbreaker-*.db"))) == 1
        assert len(list(backups.glob("packbreaker-*.json"))) == 1

        policy = client.get("/api/v1/system/backups/policy")
        assert policy.status_code == 200
        assert policy.headers["etag"] == '"2"'
        assert policy.json()["version"] == 2
        assert policy.json()["last_success_at"] is not None
        assert policy.json()["last_error_code"] is None

        health = client.get("/api/v1/system/health")
        assert health.status_code == 200
        backup_check = next(item for item in health.json()["checks"] if item["name"] == "backups")
        assert backup_check["metrics"]["schedule_enabled"] is True
        assert backup_check["metrics"]["valid_backup_count"] == 1
    finally:
        client.__exit__(None, None, None)
