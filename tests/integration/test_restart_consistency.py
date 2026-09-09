from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from backend.app.api.dependencies import CSRF_COOKIE, SESSION_COOKIE
from backend.app.config import AppSettings
from backend.app.infrastructure.persistence.models import Downloader, UnpackTask
from backend.app.main import create_app

_PASSWORD = "synthetic correct horse battery staple"


def _settings(tmp_path: Path) -> AppSettings:
    return AppSettings(
        config_dir=(tmp_path / "config").resolve(),
        data_dir=(tmp_path / "data").resolve(),
    )


def test_restart_preserves_config_revocation_and_empty_task_queue(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    source_root = settings.data_dir / "source"
    source_root.mkdir(parents=True)
    canary = "PACKBREAKER-RESTART-CANARY-5f9a"
    revoked_session: str | None = None
    downloader_id: str | None = None

    first_app = create_app(settings=settings)
    with TestClient(first_app, base_url="https://testserver") as client:
        assert client.post("/api/v1/auth/setup", json={"password": _PASSWORD}).status_code == 201
        assert client.post("/api/v1/auth/login", json={"password": _PASSWORD}).status_code == 200
        csrf = client.cookies.get(CSRF_COOKIE)
        revoked_session = client.cookies.get(SESSION_COOKIE)
        assert csrf is not None and revoked_session is not None

        created = client.post(
            "/api/v1/downloaders",
            headers={"X-CSRF-Token": csrf},
            json={
                "name": "restart-qB",
                "type": "QBITTORRENT",
                "base_url": "http://qb.invalid:8080",
                "credential": {"username": "admin", "password": canary},
                "path_mappings": [
                    {"remote_prefix": "/downloads", "container_prefix": str(source_root)}
                ],
            },
        )
        assert created.status_code == 201
        downloader_id = created.json()["id"]
        assert client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": csrf}).status_code == 204
        with first_app.state.runtime.session_factory() as session:
            assert session.scalar(select(func.count()).select_from(UnpackTask)) == 0

    assert revoked_session is not None and downloader_id is not None
    second_app = create_app(settings=settings)
    with TestClient(second_app, base_url="https://testserver") as client:
        client.cookies.set(SESSION_COOKIE, revoked_session)
        assert client.get("/api/v1/auth/me").json()["authenticated"] is False
        client.cookies.clear()

        assert client.post("/api/v1/auth/login", json={"password": _PASSWORD}).status_code == 200
        listed = client.get("/api/v1/downloaders")
        assert listed.status_code == 200
        items = listed.json()["items"]
        assert len(items) == 1
        assert items[0]["id"] == downloader_id
        assert items[0]["name"] == "restart-qB"
        assert items[0]["base_url"] == "http://qb.invalid:8080"
        assert items[0]["credential_configured"] is True
        assert canary not in listed.text

        with second_app.state.runtime.session_factory() as session:
            downloader = session.get(Downloader, downloader_id)
            assert downloader is not None and downloader.secret_id is not None
            assert canary.encode() in second_app.state.secret_store.get(downloader.secret_id)
            assert session.scalar(select(func.count()).select_from(UnpackTask)) == 0
