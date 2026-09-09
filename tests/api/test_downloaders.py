import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import httpx2
from _pytest.logging import LogCaptureFixture
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app.api.dependencies import CSRF_COOKIE
from backend.app.config import AppSettings
from backend.app.infrastructure.adapters.downloaders import DownloaderAdapterFactory
from backend.app.infrastructure.app_logging import JsonLogFormatter
from backend.app.infrastructure.persistence.models import Downloader, SecretRecord, UnpackTask
from backend.app.main import create_app

_PASSWORD = "synthetic correct horse battery staple"


def _settings(tmp_path: Path) -> AppSettings:
    return AppSettings(
        config_dir=(tmp_path / "config").resolve(),
        data_dir=(tmp_path / "data").resolve(),
    )


def _authenticated_client(tmp_path: Path) -> tuple[TestClient, FastAPI]:
    settings = _settings(tmp_path)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    app = create_app(settings=settings)
    client = TestClient(app, base_url="https://testserver")
    client.__enter__()
    assert client.post("/api/v1/auth/setup", json={"password": _PASSWORD}).status_code == 201
    assert client.post("/api/v1/auth/login", json={"password": _PASSWORD}).status_code == 200
    return client, app


def _csrf(client: TestClient) -> dict[str, str]:
    token = client.cookies.get(CSRF_COOKIE)
    assert token is not None
    return {"X-CSRF-Token": token}


def _create_qb(
    client: TestClient,
    *,
    data_root: Path,
    password: str = "synthetic-downloader-password",
) -> dict[str, object]:
    source_root = data_root / "source"
    source_root.mkdir(parents=True, exist_ok=True)
    response = client.post(
        "/api/v1/downloaders",
        headers=_csrf(client),
        json={
            "name": "主 qB",
            "type": "QBITTORRENT",
            "base_url": "http://qb.invalid:8080/",
            "credential": {"username": "admin", "password": password},
            "monitor_rules": {"category": "pack"},
            "path_mappings": [
                {"remote_prefix": "/downloads", "container_prefix": str(source_root)}
            ],
        },
    )
    assert response.status_code == 201
    assert response.headers["ETag"] == '"1"'
    return cast(dict[str, object], response.json())


def _install_qb_probe(app: FastAPI) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/auth/login"):
            return httpx2.Response(200, text="Ok.", headers={"Set-Cookie": "SID=fake; path=/"})
        if request.url.path.endswith("/app/version"):
            return httpx2.Response(200, text="v5.2.1")
        if request.url.path.endswith("/app/webapiVersion"):
            return httpx2.Response(200, text="2.15.1")
        return httpx2.Response(404)

    app.state.downloader_service._adapter_factory = DownloaderAdapterFactory(  # noqa: SLF001
        transport=httpx2.MockTransport(handler)
    )


def test_downloader_credential_is_encrypted_and_never_returned(tmp_path: Path) -> None:
    client, app = _authenticated_client(tmp_path)
    canary = "PACKBREAKER-CREDENTIAL-CANARY-7b11"
    try:
        created = _create_qb(
            client,
            data_root=app.state.settings.data_dir,
            password=canary,
        )
        assert created["credential_configured"] is True
        assert "credential" not in created
        assert "secret_id" not in created
        assert canary not in str(created)

        with app.state.runtime.session_factory() as session:
            downloader = session.scalar(select(Downloader))
            assert downloader is not None and downloader.secret_id is not None
            secret = session.get(SecretRecord, downloader.secret_id)
            assert secret is not None
            assert canary not in secret.ciphertext
            secret_id = secret.id

        patched = client.patch(
            f"/api/v1/downloaders/{created['id']}",
            headers={**_csrf(client), "If-Match": '"1"'},
            json={"name": "主 qB 重命名", "credential": None},
        )
        assert patched.status_code == 200
        assert patched.json()["credential_configured"] is True
        assert patched.headers["ETag"] == '"2"'
        with app.state.runtime.session_factory() as session:
            downloader = session.get(Downloader, cast(str, created["id"]))
            assert downloader is not None and downloader.secret_id == secret_id

        cleared = client.patch(
            f"/api/v1/downloaders/{created['id']}",
            headers={**_csrf(client), "If-Match": '"2"'},
            json={"clear_credential": True},
        )
        assert cleared.status_code == 200
        assert cleared.json()["credential_configured"] is False
        with app.state.runtime.session_factory() as session:
            assert session.get(SecretRecord, secret_id) is None
    finally:
        client.__exit__(None, None, None)


def test_downloader_canary_never_appears_in_logs_api_or_database(
    tmp_path: Path, caplog: LogCaptureFixture
) -> None:
    client, app = _authenticated_client(tmp_path)
    canary = "PACKBREAKER-CREDENTIAL-CANARY-logs-db-api-5b4f"
    try:
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="packbreaker.http"):
            created = _create_qb(
                client,
                data_root=app.state.settings.data_dir,
                password=canary,
            )

            def handler(_request: httpx2.Request) -> httpx2.Response:
                return httpx2.Response(200, text="Fails.")

            app.state.downloader_service._adapter_factory = DownloaderAdapterFactory(  # noqa: SLF001
                transport=httpx2.MockTransport(handler)
            )
            failure = client.post(
                f"/api/v1/downloaders/{created['id']}/test",
                headers=_csrf(client),
            )

        assert failure.status_code == 502
        assert canary not in failure.text
        rendered_logs = "\n".join(JsonLogFormatter().format(record) for record in caplog.records)
        assert "request.complete" in rendered_logs
        assert canary not in rendered_logs

        for state_file in app.state.settings.config_dir.iterdir():
            if state_file.is_file():
                assert canary.encode() not in state_file.read_bytes()
    finally:
        client.__exit__(None, None, None)


def test_stale_if_match_is_rejected(tmp_path: Path) -> None:
    client, app = _authenticated_client(tmp_path)
    try:
        created = _create_qb(client, data_root=app.state.settings.data_dir)
        first = client.patch(
            f"/api/v1/downloaders/{created['id']}",
            headers={**_csrf(client), "If-Match": '"1"'},
            json={"name": "更新后的 qB"},
        )
        assert first.status_code == 200

        stale = client.patch(
            f"/api/v1/downloaders/{created['id']}",
            headers={**_csrf(client), "If-Match": '"1"'},
            json={"name": "过期写入"},
        )
        assert stale.status_code == 412
        assert stale.json()["code"] == "DOWNLOADER_VERSION_CONFLICT"
    finally:
        client.__exit__(None, None, None)


def test_connection_path_diagnostics_and_enable_gate(tmp_path: Path) -> None:
    client, app = _authenticated_client(tmp_path)
    try:
        created = _create_qb(client, data_root=app.state.settings.data_dir)
        downloader_id = cast(str, created["id"])
        blocked = client.post(
            f"/api/v1/downloaders/{downloader_id}/actions",
            headers={**_csrf(client), "If-Match": '"1"'},
            json={"action": "enable"},
        )
        assert blocked.status_code == 409
        assert blocked.json()["code"] == "DOWNLOADER_CONNECTION_TEST_REQUIRED"

        _install_qb_probe(app)
        tested = client.post(
            f"/api/v1/downloaders/{downloader_id}/test",
            headers=_csrf(client),
        )
        assert tested.status_code == 200
        assert tested.json()["capabilities"]["version"] == "v5.2.1"

        source_file = app.state.settings.data_dir / "source" / "movie.mkv"
        source_file.write_bytes(b"synthetic-media-bytes")
        target_dir = app.state.settings.data_dir / "target"
        target_dir.mkdir()
        before_links = source_file.stat().st_nlink
        diagnosed = client.post(
            f"/api/v1/downloaders/{downloader_id}/path-diagnostics",
            headers=_csrf(client),
            json={
                "probes": [
                    {
                        "remote_path": "/downloads/movie.mkv",
                        "target_directory": str(target_dir),
                    }
                ]
            },
        )
        assert diagnosed.status_code == 200
        assert diagnosed.json()["status"] == "ok"
        assert diagnosed.json()["all_mappings_verified"] is True
        assert diagnosed.json()["results"][0]["hardlink_feasible"] is True
        assert source_file.stat().st_nlink == before_links
        assert list(target_dir.iterdir()) == []

        enabled = client.post(
            f"/api/v1/downloaders/{downloader_id}/actions",
            headers={**_csrf(client), "If-Match": '"1"'},
            json={"action": "enable"},
        )
        assert enabled.status_code == 200
        assert enabled.json()["enabled"] is True
        assert enabled.headers["ETag"] == '"2"'

        changed = client.patch(
            f"/api/v1/downloaders/{downloader_id}",
            headers={**_csrf(client), "If-Match": '"2"'},
            json={"base_url": "http://qb-new.invalid:8080"},
        )
        assert changed.status_code == 200
        assert changed.json()["enabled"] is False
        assert changed.json()["connection_status"] == "UNTESTED"
    finally:
        client.__exit__(None, None, None)


def test_all_path_mappings_must_be_verified_before_enable(tmp_path: Path) -> None:
    client, app = _authenticated_client(tmp_path)
    try:
        root_a = app.state.settings.data_dir / "source-a"
        root_b = app.state.settings.data_dir / "source-b"
        target_dir = app.state.settings.data_dir / "target"
        root_a.mkdir()
        root_b.mkdir()
        target_dir.mkdir()
        (root_a / "a.mkv").write_bytes(b"a")
        (root_b / "b.mkv").write_bytes(b"b")
        created = client.post(
            "/api/v1/downloaders",
            headers=_csrf(client),
            json={
                "name": "多映射 qB",
                "type": "QBITTORRENT",
                "base_url": "http://qb.invalid:8080",
                "credential": {"username": "admin", "password": "synthetic-password"},
                "path_mappings": [
                    {"remote_prefix": "/a", "container_prefix": str(root_a)},
                    {"remote_prefix": "/b", "container_prefix": str(root_b)},
                ],
            },
        )
        assert created.status_code == 201
        downloader_id = created.json()["id"]
        _install_qb_probe(app)
        assert (
            client.post(
                f"/api/v1/downloaders/{downloader_id}/test",
                headers=_csrf(client),
            ).status_code
            == 200
        )

        partial = client.post(
            f"/api/v1/downloaders/{downloader_id}/path-diagnostics",
            headers=_csrf(client),
            json={"probes": [{"remote_path": "/a/a.mkv", "target_directory": str(target_dir)}]},
        )
        assert partial.status_code == 200
        assert partial.json()["status"] == "blocked"
        assert partial.json()["all_mappings_verified"] is False
        assert partial.json()["error_code"] == "PATH_MAPPING_TEST_INCOMPLETE"

        blocked = client.post(
            f"/api/v1/downloaders/{downloader_id}/actions",
            headers={**_csrf(client), "If-Match": '"1"'},
            json={"action": "enable"},
        )
        assert blocked.status_code == 409
        assert blocked.json()["code"] == "PATH_MAPPING_TEST_REQUIRED"

        complete = client.post(
            f"/api/v1/downloaders/{downloader_id}/path-diagnostics",
            headers=_csrf(client),
            json={
                "probes": [
                    {"remote_path": "/a/a.mkv", "target_directory": str(target_dir)},
                    {"remote_path": "/b/b.mkv", "target_directory": str(target_dir)},
                ]
            },
        )
        assert complete.status_code == 200
        assert complete.json()["status"] == "ok"
        assert complete.json()["all_mappings_verified"] is True

        enabled = client.post(
            f"/api/v1/downloaders/{downloader_id}/actions",
            headers={**_csrf(client), "If-Match": '"1"'},
            json={"action": "enable"},
        )
        assert enabled.status_code == 200
        assert enabled.json()["enabled"] is True
    finally:
        client.__exit__(None, None, None)


def test_path_diagnostic_rejects_symlink_escape(tmp_path: Path) -> None:
    client, app = _authenticated_client(tmp_path)
    try:
        created = _create_qb(client, data_root=app.state.settings.data_dir)
        downloader_id = cast(str, created["id"])
        outside = tmp_path / "outside.mkv"
        outside.write_bytes(b"outside")
        escape = app.state.settings.data_dir / "source" / "escape.mkv"
        escape.symlink_to(outside)
        target_dir = app.state.settings.data_dir / "target"
        target_dir.mkdir()

        response = client.post(
            f"/api/v1/downloaders/{downloader_id}/path-diagnostics",
            headers=_csrf(client),
            json={
                "probes": [
                    {
                        "remote_path": "/downloads/escape.mkv",
                        "target_directory": str(target_dir),
                    }
                ]
            },
        )

        assert response.status_code == 422
        assert response.json()["code"] == "PATH_MAPPING_INVALID"
        assert str(outside) not in response.text
        assert list(target_dir.iterdir()) == []
    finally:
        client.__exit__(None, None, None)


def test_config_write_token_can_manage_downloaders_without_csrf(tmp_path: Path) -> None:
    client, app = _authenticated_client(tmp_path)
    try:
        expires_at = (datetime.now(UTC) + timedelta(days=1)).isoformat()
        write_response = client.post(
            "/api/v1/api-tokens",
            headers=_csrf(client),
            json={"name": "config-writer", "scopes": ["config:write"], "expires_at": expires_at},
        )
        read_response = client.post(
            "/api/v1/api-tokens",
            headers=_csrf(client),
            json={"name": "config-reader", "scopes": ["config:read"], "expires_at": expires_at},
        )
        assert write_response.status_code == read_response.status_code == 201
        write_token = write_response.json()["token"]
        read_token = read_response.json()["token"]

        source_root = app.state.settings.data_dir / "source"
        source_root.mkdir(exist_ok=True)
        payload = {
            "name": "Token qB",
            "type": "QBITTORRENT",
            "base_url": "http://qb.invalid:8080",
            "path_mappings": [
                {"remote_prefix": "/downloads", "container_prefix": str(source_root)}
            ],
        }
        denied = client.post(
            "/api/v1/downloaders",
            headers={"Authorization": f"Bearer {read_token}"},
            json=payload,
        )
        assert denied.status_code == 403

        created = client.post(
            "/api/v1/downloaders",
            headers={"Authorization": f"Bearer {write_token}"},
            json=payload,
        )
        assert created.status_code == 201
    finally:
        client.__exit__(None, None, None)


def test_delete_is_blocked_while_tasks_reference_downloader(tmp_path: Path) -> None:
    client, app = _authenticated_client(tmp_path)
    try:
        created = _create_qb(client, data_root=app.state.settings.data_dir)
        downloader_id = cast(str, created["id"])
        with app.state.runtime.session_factory() as session:
            session.add(
                UnpackTask(
                    type="MOVIE_PACK",
                    source_downloader_id=downloader_id,
                    source_hash="synthetic-hash",
                    normalized_unit_key="synthetic-unit",
                    idempotency_key="synthetic-downloader-reference",
                    status="PENDING",
                    trace_id="00000000-0000-0000-0000-000000000001",
                    checkpoint={},
                    error_code=None,
                    version=1,
                )
            )
            session.commit()

        response = client.delete(
            f"/api/v1/downloaders/{downloader_id}",
            headers={**_csrf(client), "If-Match": '"1"'},
        )
        assert response.status_code == 409
        assert response.json()["code"] == "DOWNLOADER_IN_USE"
    finally:
        client.__exit__(None, None, None)
