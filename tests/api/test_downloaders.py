import json
import logging
from datetime import UTC, datetime
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


def _create_transmission(client: TestClient, *, data_root: Path) -> dict[str, object]:
    source_root = data_root / "tr-source"
    source_root.mkdir(parents=True, exist_ok=True)
    response = client.post(
        "/api/v1/downloaders",
        headers=_csrf(client),
        json={
            "name": "目标 Transmission",
            "type": "TRANSMISSION",
            "base_url": "http://tr.invalid:9091/",
            "credential": {"username": "rpc", "password": "synthetic-password"},
            "path_mappings": [
                {"remote_prefix": "/downloads", "container_prefix": str(source_root)}
            ],
        },
    )
    assert response.status_code == 201
    return cast(dict[str, object], response.json())


def _install_transmission_probe(app: FastAPI) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.headers.get("X-Transmission-Session-Id") != "synthetic-session":
            return httpx2.Response(
                409,
                headers={
                    "X-Transmission-Session-Id": "synthetic-session",
                    "X-Transmission-Rpc-Version": "6.0.0",
                },
            )
        payload = json.loads(request.content.decode())
        assert payload == {
            "jsonrpc": "2.0",
            "method": "session_get",
            "params": {"fields": ["version", "rpc_version_semver"]},
            "id": 1,
        }
        return httpx2.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "result": {"version": "4.1.3", "rpc_version_semver": "6.0.0"},
                "id": 1,
            },
        )

    app.state.downloader_service._adapter_factory = DownloaderAdapterFactory(  # noqa: SLF001
        transport=httpx2.MockTransport(handler)
    )


def test_downloader_mapping_can_use_mount_outside_data_root_without_diagnostic(
    tmp_path: Path,
) -> None:
    client, _app = _authenticated_client(tmp_path)
    try:
        mount_root = tmp_path / "standalone-downloads"
        mount_root.mkdir()
        created = client.post(
            "/api/v1/downloaders",
            headers=_csrf(client),
            json={
                "name": "独立挂载 qB",
                "type": "QBITTORRENT",
                "base_url": "http://qb.invalid:8080/",
                "credential": {
                    "username": "admin",
                    "password": "synthetic-downloader-password",
                },
                "path_mappings": [
                    {
                        "remote_prefix": "/downloads",
                        "container_prefix": str(mount_root),
                    }
                ],
            },
        )
        assert created.status_code == 201
        payload = created.json()
        assert payload["enabled"] is True
        assert payload["path_mappings"] == [
            {
                "remote_prefix": "/downloads",
                "container_prefix": str(mount_root.resolve()),
            }
        ]
        removed = client.post(
            f"/api/v1/downloaders/{payload['id']}/path-diagnostics",
            headers=_csrf(client),
            json={"probes": []},
        )
        assert removed.status_code == 404
    finally:
        client.__exit__(None, None, None)


def test_unsaved_downloader_probe_does_not_persist_config_or_secret(tmp_path: Path) -> None:
    client, app = _authenticated_client(tmp_path)
    canary = "PACKBREAKER-UNSAVED-PROBE-CANARY-91f3"
    try:
        _install_qb_probe(app)
        response = client.post(
            "/api/v1/downloaders/probe",
            headers=_csrf(client),
            json={
                "type": "QBITTORRENT",
                "base_url": "http://qb.invalid:8080/",
                "credential": {"username": "admin", "password": canary},
            },
        )
        assert response.status_code == 200
        assert response.json()["capabilities"]["version"] == "v5.2.1"
        assert canary not in response.text
        with app.state.runtime.session_factory() as session:
            assert list(session.scalars(select(Downloader))) == []
            assert list(session.scalars(select(SecretRecord))) == []
    finally:
        client.__exit__(None, None, None)


def test_qbittorrent_runtime_metrics_endpoint_uses_all_torrent_sizes(tmp_path: Path) -> None:
    client, app = _authenticated_client(tmp_path)
    try:
        created = _create_qb(client, data_root=app.state.settings.data_dir)

        def handler(request: httpx2.Request) -> httpx2.Response:
            if request.url.path.endswith("/auth/login"):
                return httpx2.Response(200, text="Ok.", headers={"Set-Cookie": "SID=fake; path=/"})
            if request.url.path.endswith("/transfer/info"):
                return httpx2.Response(200, json={"up_info_speed": 1024, "dl_info_speed": 2048})
            if request.url.path.endswith("/torrents/info"):
                return httpx2.Response(200, json=[{"size": 100}, {"size": 250}])
            if request.url.path.endswith("/sync/maindata"):
                return httpx2.Response(200, json={"server_state": {"free_space_on_disk": 4096}})
            return httpx2.Response(404)

        app.state.downloader_service._adapter_factory = DownloaderAdapterFactory(  # noqa: SLF001
            transport=httpx2.MockTransport(handler)
        )

        response = client.get(f"/api/v1/downloaders/{created['id']}/metrics")

        assert response.status_code == 200
        body = response.json()
        assert body["upload_speed_bytes_per_second"] == 1024
        assert body["download_speed_bytes_per_second"] == 2048
        assert body["total_content_size_bytes"] == 350
        assert body["free_space_bytes"] == 4096
        assert body["active_torrent_count"] is None
        assert body["total_torrent_count"] == 2
        assert body["sampled_at"].endswith("Z")
    finally:
        client.__exit__(None, None, None)


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
        assert "测试下载器连接失败" in rendered_logs
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


def test_connection_is_the_only_probe_gate_for_downloader_use(tmp_path: Path) -> None:
    client, app = _authenticated_client(tmp_path)
    try:
        created = _create_qb(client, data_root=app.state.settings.data_dir)
        downloader_id = cast(str, created["id"])
        assert created["enabled"] is True

        _install_qb_probe(app)
        tested = client.post(
            f"/api/v1/downloaders/{downloader_id}/test",
            headers=_csrf(client),
        )
        assert tested.status_code == 200
        assert tested.json()["capabilities"]["version"] == "v5.2.1"

        binding = app.state.downloader_service.qbittorrent_write_binding(downloader_id)
        assert binding.downloader_id == downloader_id
        assert binding.downloader_version == 1
        assert binding.capabilities["api_version"] == "2.15.1"
        assert binding.remote_save_path(app.state.settings.data_dir / "source") == "/downloads"

        removed = client.post(
            f"/api/v1/downloaders/{downloader_id}/path-diagnostics",
            headers=_csrf(client),
            json={"probes": []},
        )
        assert removed.status_code == 404

        changed = client.patch(
            f"/api/v1/downloaders/{downloader_id}",
            headers={**_csrf(client), "If-Match": '"1"'},
            json={"base_url": "http://qb-new.invalid:8080"},
        )
        assert changed.status_code == 200
        assert changed.json()["enabled"] is False
        assert changed.json()["connection_status"] == "UNTESTED"
    finally:
        client.__exit__(None, None, None)


def test_downloaders_torrent_picker_reads_and_filters_real_qb_items(tmp_path: Path) -> None:
    client, app = _authenticated_client(tmp_path)
    try:
        created = _create_qb(client, data_root=app.state.settings.data_dir)
        downloader_id = cast(str, created["id"])
        now = datetime.now(UTC)
        with app.state.runtime.session_factory() as session:
            downloader = session.get(Downloader, downloader_id)
            assert downloader is not None
            downloader.enabled = True
            downloader.connection_status = "OK"
            downloader.path_mapping_status = "OK"
            downloader.capabilities = {
                "client": "qBittorrent",
                "version": "v5.2.3",
                "api_version": "2.15.1",
                "supports_skip_checking": True,
                "supports_force_recheck": True,
                "supports_verify_progress": True,
                "read_only_probe": True,
            }
            downloader.last_test_at = now
            downloader.last_path_diagnostic_at = now
            session.commit()

        def handler(request: httpx2.Request) -> httpx2.Response:
            if request.url.path.endswith("/auth/login"):
                return httpx2.Response(200, text="Ok.", headers={"Set-Cookie": "SID=fake; path=/"})
            if request.url.path.endswith("/torrents/info"):
                return httpx2.Response(
                    200,
                    json=[
                        {
                            "hash": "a" * 40,
                            "name": "Movie.One.2024",
                            "state": "uploading",
                            "progress": 1.0,
                            "size": 1024,
                            "category": "movie",
                            "tags": "manual,uhd",
                            "tracker": "https://tracker.invalid/announce",
                            "save_path": "/downloads",
                            "content_path": "/downloads/Movie.One.2024",
                        },
                        {
                            "hash": "b" * 40,
                            "name": "Series.Two.S01",
                            "state": "downloading",
                            "progress": 0.5,
                            "size": 2048,
                            "category": "tv",
                            "tags": "series",
                            "tracker": "https://other.invalid/announce",
                            "save_path": "/downloads/tv",
                            "content_path": "/downloads/tv/Series.Two.S01",
                        },
                    ],
                )
            return httpx2.Response(404)

        app.state.downloader_service._adapter_factory = DownloaderAdapterFactory(  # noqa: SLF001
            transport=httpx2.MockTransport(handler)
        )
        response = client.get(
            f"/api/v1/downloaders/{downloader_id}/torrents",
            params={"search": "movie", "category": "MOVIE", "tag": "UHD", "page_size": 25},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["page"] == 1
        assert body["page_size"] == 25
        assert body["total"] == 1
        assert body["items"] == [
            {
                "torrent_hash": "a" * 40,
                "name": "Movie.One.2024",
                "status": "uploading",
                "progress": 1.0,
                "size_bytes": 1024,
                "category": "movie",
                "tags": ["manual", "uhd"],
                "tracker": "https://tracker.invalid/announce",
                "save_path": "/downloads",
                "content_path": "/downloads/Movie.One.2024",
            }
        ]
    finally:
        client.__exit__(None, None, None)


def test_transmission_probe_and_write_binding_freeze_safe_capabilities(tmp_path: Path) -> None:
    client, app = _authenticated_client(tmp_path)
    try:
        created = _create_transmission(client, data_root=app.state.settings.data_dir)
        downloader_id = cast(str, created["id"])
        assert created["enabled"] is True
        _install_transmission_probe(app)

        tested = client.post(
            f"/api/v1/downloaders/{downloader_id}/test",
            headers=_csrf(client),
        )
        assert tested.status_code == 200
        assert tested.json()["capabilities"] == {
            "client": "Transmission",
            "version": "4.1.3",
            "api_version": "6.0.0",
            "supports_skip_checking": False,
            "supports_force_recheck": True,
            "supports_verify_progress": True,
            "read_only_probe": True,
        }

        binding = app.state.downloader_service.transmission_write_binding(downloader_id)
        assert binding.downloader_id == downloader_id
        assert binding.downloader_version == 1
        assert binding.capabilities["supports_skip_checking"] is False
        assert binding.capabilities["supports_force_recheck"] is True
        assert binding.remote_save_path(app.state.settings.data_dir / "tr-source") == "/downloads"
    finally:
        client.__exit__(None, None, None)


def test_multiple_path_mappings_do_not_require_preflight_diagnostics(tmp_path: Path) -> None:
    client, app = _authenticated_client(tmp_path)
    try:
        root_a = app.state.settings.data_dir / "source-a"
        root_b = app.state.settings.data_dir / "source-b"
        root_a.mkdir()
        root_b.mkdir()
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
        downloader_id = cast(str, created.json()["id"])
        assert created.json()["enabled"] is True
        _install_qb_probe(app)
        assert (
            client.post(
                f"/api/v1/downloaders/{downloader_id}/test",
                headers=_csrf(client),
            ).status_code
            == 200
        )

        binding = app.state.downloader_service.qbittorrent_write_binding(downloader_id)
        assert binding.remote_save_path(root_a) == "/a"
        assert binding.remote_save_path(root_b) == "/b"
        assert (
            client.post(
                f"/api/v1/downloaders/{downloader_id}/path-diagnostics",
                headers=_csrf(client),
                json={"probes": []},
            ).status_code
            == 404
        )
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
