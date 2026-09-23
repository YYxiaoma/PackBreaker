from __future__ import annotations

import asyncio
from pathlib import Path
from typing import cast

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app.api.dependencies import CSRF_COOKIE
from backend.app.application import sites as site_application
from backend.app.application.errors import ApplicationError
from backend.app.application.secrets import SecretStore
from backend.app.config import AppSettings
from backend.app.domain.site_config import SiteKind, site_kind_is_persistable, site_profile
from backend.app.infrastructure.adapters.sites import SiteAdapterFactory
from backend.app.infrastructure.persistence.models import SecretRecord, Site
from backend.app.infrastructure.persistence.site_repositories import SiteRepository
from backend.app.main import create_app

_PASSWORD = "synthetic correct horse battery staple"


def _authenticated_client(tmp_path: Path) -> tuple[TestClient, FastAPI]:
    settings = AppSettings(
        config_dir=(tmp_path / "config").resolve(),
        data_dir=(tmp_path / "data").resolve(),
    )
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


def _create_site(client: TestClient, api_key: str = "synthetic-mteam-key") -> dict[str, object]:
    response = client.post(
        "/api/v1/sites",
        headers=_csrf(client),
        json={
            "name": "M-Team 主站",
            "type": "MTEAM",
            "credential": {"kind": "API_KEY", "value": api_key},
        },
    )
    assert response.status_code == 201
    assert response.headers["ETag"] == '"1"'
    return cast(dict[str, object], response.json())


def test_removed_site_user_profile_has_no_business_entrypoint(tmp_path: Path) -> None:
    """Deleting the UI/API must also retire the unused service entrypoint."""
    client, app = _authenticated_client(tmp_path)
    try:
        assert not hasattr(app.state.site_service, "user_profile")
        routes = app.openapi()["paths"]
        assert all("user-profile" not in path for path in routes)
    finally:
        client.__exit__(None, None, None)


def test_site_profiles_expose_fixed_trusted_origins(tmp_path: Path) -> None:
    client, _app = _authenticated_client(tmp_path)
    try:
        response = client.get("/api/v1/sites/profiles")
        assert response.status_code == 200
        items = {item["kind"]: item for item in response.json()["items"]}
        assert set(items) == {
            "MTEAM",
            "HDTIME",
            "HHCLUB",
            "KEEPFRDS",
            "HDHOME",
            "UBITS",
            "HDFANS",
            "BTSCHOOL",
            "PTTIME",
            "ROUSI_PRO",
            "LINGYIN_CLUB",
        }
        assert items["MTEAM"]["base_url"] == "https://kp.m-team.cc"
        assert items["HDTIME"]["base_url"] == "https://hdtime.org"
        assert items["HHCLUB"]["base_url"] == "https://hhanclub.net"
        assert items["HDTIME"]["supports_user_agent"] is True
        assert items["HDTIME"]["supports_browser_emulation"] is True
        assert items["KEEPFRDS"]["support_status"] == "PENDING_ADAPTER"
        assert items["ROUSI_PRO"]["credential_kind"] == "API_KEY"
        assert items["LINGYIN_CLUB"]["base_url"] == "https://pt.soulvoice.club"
        assert items["LINGYIN_CLUB"]["credential_kind"] == "COOKIE"
        assert items["LINGYIN_CLUB"]["support_status"] == "PENDING_ADAPTER"
    finally:
        client.__exit__(None, None, None)


def test_pending_site_profiles_cannot_create_or_probe_or_persist_secrets(tmp_path: Path) -> None:
    client, app = _authenticated_client(tmp_path)
    try:
        pending_kinds = tuple(kind for kind in SiteKind if not site_kind_is_persistable(kind))
        assert set(pending_kinds) == {
            SiteKind.HDHOME,
            SiteKind.KEEPFRDS,
            SiteKind.UBITS,
            SiteKind.HDFANS,
            SiteKind.ROUSI_PRO,
            SiteKind.BTSCHOOL,
            SiteKind.PTTIME,
            SiteKind.LINGYIN_CLUB,
        }
        with app.state.runtime.session_factory() as session:
            before_sites = len(list(session.scalars(select(Site))))
            before_secrets = len(list(session.scalars(select(SecretRecord))))

        for kind in pending_kinds:
            profile = site_profile(kind)
            secret = f"synthetic-secret-for-{kind.value}"
            payload = {
                "type": kind.value,
                "credential": {"kind": profile.credential_kind.value, "value": secret},
                **(
                    {"download_cookie": "synthetic-rousi-cookie"}
                    if kind is SiteKind.ROUSI_PRO
                    else {}
                ),
            }
            for endpoint, request in (
                ("/api/v1/sites", {"name": f"Synthetic pending {kind.value}", **payload}),
                ("/api/v1/sites/probe", payload),
            ):
                response = client.post(endpoint, headers=_csrf(client), json=request)
                assert response.status_code == 409, kind
                assert response.json()["code"] == "SITE_ADAPTER_PENDING", kind
                assert secret not in response.text
                assert "synthetic-rousi-cookie" not in response.text

        with app.state.runtime.session_factory() as session:
            assert len(list(session.scalars(select(Site)))) == before_sites
            assert len(list(session.scalars(select(SecretRecord)))) == before_secrets
    finally:
        client.__exit__(None, None, None)


def test_rousi_dual_credential_lifecycle_after_synthetic_only_promotion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Test the future supported path without changing production's PENDING gate.
    monkeypatch.setattr(
        site_application,
        "site_kind_is_persistable",
        lambda kind: kind is SiteKind.ROUSI_PRO or site_kind_is_persistable(kind),
    )
    client, app = _authenticated_client(tmp_path)
    key = "synthetic-rousi-key-first"
    cookie = "synthetic-rousi-cookie-first"
    try:
        created = client.post(
            "/api/v1/sites",
            headers=_csrf(client),
            json={
                "name": "Synthetic Rousi",
                "type": "ROUSI_PRO",
                "credential": {"kind": "API_KEY", "value": key},
                "download_cookie": cookie,
            },
        )
        assert created.status_code == 201
        site_id = created.json()["id"]
        assert created.json()["download_credential_configured"] is True
        assert created.json()["enabled"] is False
        assert key not in created.text and cookie not in created.text
        assert "download_secret_id" not in created.text

        with app.state.runtime.session_factory() as session:
            row = session.get(Site, site_id)
            assert row is not None and row.secret_id is not None
            assert row.download_secret_id is not None and row.secret_id != row.download_secret_id
            original_main, original_cookie = row.secret_id, row.download_secret_id
            main_record = session.get(SecretRecord, original_main)
            cookie_record = session.get(SecretRecord, original_cookie)
            assert main_record is not None and main_record.kind == "SITE_API_KEY"
            assert cookie_record is not None and cookie_record.kind == "SITE_DOWNLOAD_COOKIE"
            assert key not in main_record.ciphertext
            assert cookie not in cookie_record.ciphertext
        store = SecretStore(app.state.runtime.session_factory, app.state.runtime.secret_cipher)
        assert store.get(original_main) == key.encode()
        assert store.get(original_cookie) == cookie.encode()

        stale = client.patch(
            f"/api/v1/sites/{site_id}",
            headers={**_csrf(client), "If-Match": '"2"'},
            json={"download_cookie": "synthetic-rousi-cookie-second"},
        )
        assert stale.status_code == 412
        assert stale.json()["code"] == "SITE_VERSION_CONFLICT"
        with app.state.runtime.session_factory() as session:
            assert session.get(Site, site_id).download_secret_id == original_cookie

        rotated = client.patch(
            f"/api/v1/sites/{site_id}",
            headers={**_csrf(client), "If-Match": '"1"'},
            json={"download_cookie": "synthetic-rousi-cookie-second"},
        )
        assert rotated.status_code == 200
        assert rotated.json()["download_credential_configured"] is True
        assert rotated.headers["ETag"] == '"2"'
        assert cookie not in rotated.text
        with app.state.runtime.session_factory() as session:
            row = session.get(Site, site_id)
            assert row is not None and row.secret_id == original_main
            assert row.download_secret_id is not None
            rotated_cookie = row.download_secret_id
            assert rotated_cookie != original_cookie
            assert session.get(SecretRecord, original_cookie) is None
        assert store.get(rotated_cookie) == b"synthetic-rousi-cookie-second"
        expected_torrent = (
            b"d4:infod6:lengthi1e4:name9:synthetic12:piece lengthi16384e"
            b"6:pieces20:01234567890123456789ee"
        )
        download_requests: list[str] = []

        def download_handler(request: httpx2.Request) -> httpx2.Response:
            download_requests.append(str(request.url))
            assert str(request.url) == "https://rousi.pro/api/v1/torrents/123/download"
            assert request.headers.get("cookie") == "synthetic-rousi-cookie-second"
            assert request.headers.get("api-token") is None
            assert request.headers.get("authorization") is None
            return httpx2.Response(
                200,
                content=expected_torrent,
                headers={"content-type": "application/x-bittorrent"},
            )

        app.state.site_service._adapter_factory = SiteAdapterFactory(  # noqa: SLF001
            transport=httpx2.MockTransport(download_handler)
        )
        adapter = app.state.site_service._create_adapter(  # noqa: SLF001
            app.state.site_service._connection_snapshot(site_id),  # noqa: SLF001
            key,
        )
        assert asyncio.run(adapter.capabilities()).search_results_are_complete is True
        assert asyncio.run(adapter.fetch_torrent("123")).content == expected_torrent
        assert download_requests == ["https://rousi.pro/api/v1/torrents/123/download"]

        rekeyed = client.patch(
            f"/api/v1/sites/{site_id}",
            headers={**_csrf(client), "If-Match": '"2"'},
            json={"credential": {"kind": "API_KEY", "value": "synthetic-rousi-key-second"}},
        )
        assert rekeyed.status_code == 200
        assert rekeyed.json()["download_credential_configured"] is False
        with app.state.runtime.session_factory() as session:
            row = session.get(Site, site_id)
            assert row is not None and row.download_secret_id is None
            assert session.get(SecretRecord, original_main) is None
            assert session.get(SecretRecord, rotated_cookie) is None
        blocked_enable = client.post(
            f"/api/v1/sites/{site_id}/actions",
            headers={**_csrf(client), "If-Match": '"3"'},
            json={"action": "enable"},
        )
        assert blocked_enable.status_code == 409
        assert blocked_enable.json()["code"] == "SITE_DOWNLOAD_CREDENTIAL_REQUIRED"
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize("wrong_reference", ("primary", "download"))
def test_imported_rousi_wrong_secret_kind_cannot_activate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    wrong_reference: str,
) -> None:
    """Wrongly imported primary/secondary secrets must not cross auth headers."""
    monkeypatch.setattr(
        site_application,
        "site_kind_is_persistable",
        lambda kind: kind is SiteKind.ROUSI_PRO or site_kind_is_persistable(kind),
    )
    client, app = _authenticated_client(tmp_path)
    key = "synthetic-primary-key-not-a-cookie"
    cookie = "synthetic-independent-download-cookie"
    try:
        created = client.post(
            "/api/v1/sites",
            headers=_csrf(client),
            json={
                "name": "Synthetic wrong reference",
                "type": "ROUSI_PRO",
                "credential": {"kind": "API_KEY", "value": key},
                "download_cookie": cookie,
            },
        )
        assert created.status_code == 201
        site_id = created.json()["id"]
        with app.state.runtime.session_factory() as session:
            imported = session.get(Site, site_id)
            assert imported is not None and imported.secret_id is not None
            assert imported.download_secret_id is not None
            # Simulate imported/corrupted references, not a public API action.
            if wrong_reference == "download":
                imported.download_secret_id = imported.secret_id
            else:
                imported.secret_id = imported.download_secret_id
            imported.enabled = True
            session.commit()

        requests: list[str] = []

        def reject_network(request: httpx2.Request) -> httpx2.Response:
            requests.append(str(request.url))
            raise AssertionError("Wrong-type secondary secret must not reach the network")

        app.state.site_service._adapter_factory = SiteAdapterFactory(  # noqa: SLF001
            transport=httpx2.MockTransport(reject_network)
        )
        with pytest.raises(ApplicationError) as blocked:
            app.state.site_service.enabled_adapters()
        assert blocked.value.code == "SITE_ENABLED_CONFIG_INVALID"
        assert key not in str(blocked.value) and cookie not in str(blocked.value)
        assert requests == []
        with app.state.runtime.session_factory() as session:
            unchanged = session.get(Site, site_id)
            assert unchanged is not None and unchanged.download_secret_id == unchanged.secret_id
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize(
    "pending_kind",
    (
        SiteKind.KEEPFRDS,
        SiteKind.HDHOME,
        SiteKind.UBITS,
        SiteKind.HDFANS,
        SiteKind.BTSCHOOL,
        SiteKind.PTTIME,
        SiteKind.ROUSI_PRO,
        SiteKind.LINGYIN_CLUB,
    ),
)
def test_imported_pending_site_api_actions_fail_closed_without_credential_use(
    tmp_path: Path,
    pending_kind: SiteKind,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, app = _authenticated_client(tmp_path)
    profile = site_profile(pending_kind)
    marker = f"synthetic-imported-pending-{pending_kind.value}"
    requests: list[str] = []
    try:
        with app.state.runtime.session_factory() as session:
            secret_id = SecretStore(
                app.state.runtime.session_factory,
                app.state.runtime.secret_cipher,
            ).put_in_session(
                session,
                kind="SITE_API_KEY"
                if profile.credential_kind.value == "API_KEY"
                else "SITE_COOKIE",
                value=marker.encode(),
            )
            record = SiteRepository(session).create(
                name=f"Synthetic imported {pending_kind.value}",
                kind=pending_kind.value,
                base_url=profile.base_url,
                credential_kind=profile.credential_kind.value,
                secret_id=secret_id,
                request_timeout_seconds=15,
                search_interval_seconds=2,
                user_agent=None,
                browser_emulation_enabled=False,
                proxy_enabled=False,
                proxy_host=None,
                proxy_port=None,
                proxy_username=None,
                proxy_secret_id=None,
            )
            site_id = record.id
            session.commit()

        def handler(request: httpx2.Request) -> httpx2.Response:
            requests.append(str(request.url))
            raise AssertionError("pending site must not make network requests")

        app.state.site_service._adapter_factory = SiteAdapterFactory(  # noqa: SLF001
            transport=httpx2.MockTransport(handler)
        )
        for endpoint, payload in (
            (f"/api/v1/sites/{site_id}/test", None),
            (f"/api/v1/sites/{site_id}/actions", {"action": "refresh_capabilities"}),
        ):
            response = client.post(endpoint, headers=_csrf(client), json=payload)
            assert response.status_code == 409
            assert response.json()["code"] == "SITE_ADAPTER_PENDING"
            assert marker not in response.text
        enabled = client.post(
            f"/api/v1/sites/{site_id}/actions",
            headers={**_csrf(client), "If-Match": '"1"'},
            json={"action": "enable"},
        )
        assert enabled.status_code == 409
        assert enabled.json()["code"] == "SITE_ADAPTER_PENDING"
        assert marker not in enabled.text
        with app.state.runtime.session_factory() as session:
            unchanged = session.get(Site, site_id)
            assert unchanged is not None
            assert unchanged.enabled is False
            assert unchanged.version == 1
            assert unchanged.secret_id == secret_id
            # Legacy/imported rows may already carry enabled=True. This must
            # not make the pending adapter visible to the task analysis path.
            unchanged.enabled = True
            session.commit()

        def forbidden_secret_read(_secret_id: str) -> bytes:
            raise AssertionError("Pending adapter gate must run before decrypting any secret")

        monkeypatch.setattr(app.state.site_service._secret_store, "get", forbidden_secret_read)
        with pytest.raises(ApplicationError) as adapters_blocked:
            app.state.site_service.enabled_adapters()
        assert adapters_blocked.value.code == "SITE_ADAPTER_PENDING"
        with pytest.raises(ApplicationError) as versions_blocked:
            app.state.site_service.enabled_site_versions()
        assert versions_blocked.value.code == "SITE_ADAPTER_PENDING"
        assert requests == []
    finally:
        client.__exit__(None, None, None)


def test_unsaved_site_probe_is_read_only_and_does_not_persist_secrets(tmp_path: Path) -> None:
    client, app = _authenticated_client(tmp_path)
    api_key = "PACKBREAKER-TEMP-PROBE-CANARY-a781"
    try:

        def handler(request: httpx2.Request) -> httpx2.Response:
            assert request.url.host == "api.m-team.cc"
            assert request.headers.get("x-api-key") == api_key
            return httpx2.Response(200, json={"code": "0", "data": {"id": "1"}})

        app.state.site_service._adapter_factory = SiteAdapterFactory(  # noqa: SLF001
            transport=httpx2.MockTransport(handler)
        )
        with app.state.runtime.session_factory() as session:
            before_sites = len(list(session.scalars(select(Site))))
            before_secrets = len(list(session.scalars(select(SecretRecord))))

        response = client.post(
            "/api/v1/sites/probe",
            headers=_csrf(client),
            json={
                "type": "MTEAM",
                "credential": {"kind": "API_KEY", "value": api_key},
                "request_timeout_seconds": 18,
                "search_interval_seconds": 3,
            },
        )
        assert response.status_code == 200
        assert response.json()["status"] == "ok"
        assert api_key not in response.text

        with app.state.runtime.session_factory() as session:
            assert len(list(session.scalars(select(Site)))) == before_sites
            assert len(list(session.scalars(select(SecretRecord)))) == before_secrets
    finally:
        client.__exit__(None, None, None)


def test_site_proxy_password_is_encrypted_and_write_only(tmp_path: Path) -> None:
    client, app = _authenticated_client(tmp_path)
    canary = "PACKBREAKER-PROXY-PASSWORD-CANARY-b912"
    try:
        response = client.post(
            "/api/v1/sites",
            headers=_csrf(client),
            json={
                "name": "代理 M-Team",
                "type": "MTEAM",
                "credential": {"kind": "API_KEY", "value": "synthetic-key"},
                "proxy": {
                    "enabled": True,
                    "host": "proxy.internal",
                    "port": 8080,
                    "username": "proxy-user",
                    "password": canary,
                },
            },
        )
        assert response.status_code == 201
        body = response.json()
        assert body["proxy_enabled"] is True
        assert body["proxy_host"] == "proxy.internal"
        assert body["proxy_username"] == "proxy-user"
        assert body["proxy_credential_configured"] is True
        assert canary not in response.text

        with app.state.runtime.session_factory() as session:
            site = session.scalar(select(Site).where(Site.name == "代理 M-Team"))
            assert site is not None and site.proxy_secret_id is not None
            proxy_secret_id = site.proxy_secret_id
            secret = session.get(SecretRecord, proxy_secret_id)
            assert secret is not None and secret.kind == "SITE_PROXY_PASSWORD"
            assert canary not in secret.ciphertext

        cleared = client.patch(
            f"/api/v1/sites/{body['id']}",
            headers={**_csrf(client), "If-Match": '"1"'},
            json={"proxy": {"clear_password": True}},
        )
        assert cleared.status_code == 200
        assert cleared.json()["proxy_credential_configured"] is False
        with app.state.runtime.session_factory() as session:
            assert session.get(SecretRecord, proxy_secret_id) is None
    finally:
        client.__exit__(None, None, None)


def test_imported_site_wrong_proxy_secret_kind_cannot_activate(tmp_path: Path) -> None:
    """An imported primary API key must not be reused as a proxy password."""
    client, app = _authenticated_client(tmp_path)
    key = "synthetic-key-must-not-become-proxy-password"
    proxy_password = "synthetic-independent-proxy-password"
    try:
        created = client.post(
            "/api/v1/sites",
            headers=_csrf(client),
            json={
                "name": "Synthetic proxy secret mismatch",
                "type": "MTEAM",
                "credential": {"kind": "API_KEY", "value": key},
                "proxy": {
                    "enabled": True,
                    "host": "proxy.invalid",
                    "port": 8080,
                    "username": "synthetic-user",
                    "password": proxy_password,
                },
            },
        )
        assert created.status_code == 201
        site_id = created.json()["id"]
        with app.state.runtime.session_factory() as session:
            imported = session.get(Site, site_id)
            assert imported is not None and imported.secret_id is not None
            assert imported.proxy_secret_id is not None
            imported.proxy_secret_id = imported.secret_id
            imported.enabled = True
            session.commit()

        with pytest.raises(ApplicationError) as blocked:
            app.state.site_service.enabled_adapters()
        assert blocked.value.code == "SITE_ENABLED_CONFIG_INVALID"
        assert key not in str(blocked.value)
        assert proxy_password not in str(blocked.value)
    finally:
        client.__exit__(None, None, None)


def test_site_credential_is_encrypted_write_only_and_clearable(tmp_path: Path) -> None:
    client, app = _authenticated_client(tmp_path)
    canary = "PACKBREAKER-MTEAM-APIKEY-CANARY-f872"
    try:
        created = _create_site(client, canary)
        assert created["credential_kind"] == "API_KEY"
        assert created["credential_configured"] is True
        assert "credential" not in created
        assert "secret_id" not in created
        assert canary not in str(created)

        with app.state.runtime.session_factory() as session:
            site = session.scalar(select(Site))
            assert site is not None and site.secret_id is not None
            secret = session.get(SecretRecord, site.secret_id)
            assert secret is not None
            assert secret.kind == "SITE_API_KEY"
            assert canary not in secret.ciphertext
            secret_id = secret.id

        renamed = client.patch(
            f"/api/v1/sites/{created['id']}",
            headers={**_csrf(client), "If-Match": '"1"'},
            json={"name": "M-Team 重命名"},
        )
        assert renamed.status_code == 200
        assert renamed.json()["credential_configured"] is True
        assert renamed.headers["ETag"] == '"2"'

        cleared = client.patch(
            f"/api/v1/sites/{created['id']}",
            headers={**_csrf(client), "If-Match": '"2"'},
            json={"clear_credential": True},
        )
        assert cleared.status_code == 200
        assert cleared.json()["credential_configured"] is False
        with app.state.runtime.session_factory() as session:
            assert session.get(SecretRecord, secret_id) is None
    finally:
        client.__exit__(None, None, None)


def test_site_connection_probe_enable_gate_and_config_invalidation(tmp_path: Path) -> None:
    client, app = _authenticated_client(tmp_path)
    api_key = "synthetic-mteam-key"
    try:
        created = _create_site(client, api_key)
        site_id = cast(str, created["id"])
        blocked = client.post(
            f"/api/v1/sites/{site_id}/actions",
            headers={**_csrf(client), "If-Match": '"1"'},
            json={"action": "enable"},
        )
        assert blocked.status_code == 409
        assert blocked.json()["code"] == "SITE_CONNECTION_TEST_REQUIRED"

        def handler(request: httpx2.Request) -> httpx2.Response:
            assert request.headers.get("x-api-key") == api_key
            assert request.url.path == "/api/member/profile"
            return httpx2.Response(200, json={"code": "0", "data": {"id": "1"}})

        app.state.site_service._adapter_factory = SiteAdapterFactory(  # noqa: SLF001
            transport=httpx2.MockTransport(handler)
        )
        tested = client.post(f"/api/v1/sites/{site_id}/test", headers=_csrf(client))
        assert tested.status_code == 200
        assert tested.json()["capabilities"]["supports_imdb_id"] is True

        refreshed = client.get(f"/api/v1/sites/{site_id}")
        assert refreshed.status_code == 200
        assert refreshed.json()["connection_status"] == "OK"

        enabled = client.post(
            f"/api/v1/sites/{site_id}/actions",
            headers={**_csrf(client), "If-Match": '"1"'},
            json={"action": "enable"},
        )
        assert enabled.status_code == 200
        assert enabled.json()["enabled"] is True
        assert enabled.headers["ETag"] == '"2"'

        changed = client.patch(
            f"/api/v1/sites/{site_id}",
            headers={**_csrf(client), "If-Match": '"2"'},
            json={"request_timeout_seconds": 25},
        )
        assert changed.status_code == 200
        assert changed.json()["enabled"] is False
        assert changed.json()["connection_status"] == "UNTESTED"
    finally:
        client.__exit__(None, None, None)


def test_retired_site_profile_endpoint_is_not_available_and_does_not_access_remote(
    tmp_path: Path,
) -> None:
    client, app = _authenticated_client(tmp_path)
    calls = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request.url.path)
        raise AssertionError("已移除的站点用户资料接口不应访问远端")

    try:
        created = _create_site(client)
        app.state.site_service._adapter_factory = SiteAdapterFactory(  # noqa: SLF001
            transport=httpx2.MockTransport(handler)
        )
        response = client.get(f"/api/v1/sites/{created['id']}/profile")
        assert response.status_code == 404
        assert calls == []
        assert client.get("/api/v1/sites").status_code == 200
    finally:
        client.__exit__(None, None, None)


def test_site_stale_if_match_and_legacy_base_url_cannot_override_profile(tmp_path: Path) -> None:
    client, _app = _authenticated_client(tmp_path)
    try:
        created = _create_site(client)
        site_id = cast(str, created["id"])
        updated = client.patch(
            f"/api/v1/sites/{site_id}",
            headers={**_csrf(client), "If-Match": '"1"'},
            json={"name": "新名称"},
        )
        assert updated.status_code == 200
        stale = client.patch(
            f"/api/v1/sites/{site_id}",
            headers={**_csrf(client), "If-Match": '"1"'},
            json={"name": "过期名称"},
        )
        assert stale.status_code == 412
        assert stale.json()["code"] == "SITE_VERSION_CONFLICT"

        legacy = client.post(
            "/api/v1/sites",
            headers=_csrf(client),
            json={
                "name": "危险地址",
                "type": "MTEAM",
                "base_url": "https://user:pass@api.m-team.cc/path?token=1",
                "credential": {"kind": "API_KEY", "value": "synthetic"},
            },
        )
        assert legacy.status_code == 201
        assert legacy.json()["base_url"] == "https://kp.m-team.cc"
        assert "user:pass" not in legacy.text

        wrong_mteam_host = client.post(
            "/api/v1/sites",
            headers=_csrf(client),
            json={
                "name": "错误 M-Team 域名",
                "type": "MTEAM",
                "base_url": "https://example.com",
                "credential": {"kind": "API_KEY", "value": "synthetic"},
            },
        )
        assert wrong_mteam_host.status_code == 201
        assert wrong_mteam_host.json()["base_url"] == "https://kp.m-team.cc"
    finally:
        client.__exit__(None, None, None)


def test_hdtime_cookie_is_encrypted_and_drives_read_only_connection_probe(tmp_path: Path) -> None:
    client, app = _authenticated_client(tmp_path)
    cookie = "uid=123; pass=PACKBREAKER-HDTIME-COOKIE-CANARY-a91d"
    try:
        response = client.post(
            "/api/v1/sites",
            headers=_csrf(client),
            json={
                "name": "HDTime 主站",
                "type": "HDTIME",
                "credential": {"kind": "COOKIE", "value": cookie},
            },
        )
        assert response.status_code == 201
        created = cast(dict[str, object], response.json())
        assert created["credential_kind"] == "COOKIE"
        assert created["credential_configured"] is True
        assert cookie not in response.text

        with app.state.runtime.session_factory() as session:
            site = session.scalar(select(Site).where(Site.type == "HDTIME"))
            assert site is not None and site.secret_id is not None
            assert site.credential_kind == "COOKIE"
            secret = session.get(SecretRecord, site.secret_id)
            assert secret is not None
            assert secret.kind == "SITE_COOKIE"
            assert cookie not in secret.ciphertext

        def handler(request: httpx2.Request) -> httpx2.Response:
            assert request.url.host == "hdtime.org"
            assert request.url.path == "/index.php"
            assert request.headers.get("cookie") == cookie
            return httpx2.Response(200, text='<a href="usercp.php">profile</a>')

        app.state.site_service._adapter_factory = SiteAdapterFactory(  # noqa: SLF001
            transport=httpx2.MockTransport(handler)
        )
        tested = client.post(f"/api/v1/sites/{created['id']}/test", headers=_csrf(client))
        assert tested.status_code == 200
        assert cookie not in tested.text
        refreshed = client.get(f"/api/v1/sites/{created['id']}")
        assert refreshed.json()["connection_status"] == "OK"
    finally:
        client.__exit__(None, None, None)


def test_hhclub_cookie_is_encrypted_and_drives_read_only_connection_probe(tmp_path: Path) -> None:
    client, app = _authenticated_client(tmp_path)
    cookie = "uid=123; pass=PACKBREAKER-HHCLUB-COOKIE-CANARY-b72f"
    try:
        response = client.post(
            "/api/v1/sites",
            headers=_csrf(client),
            json={
                "name": "HHClub 主站",
                "type": "HHCLUB",
                "credential": {"kind": "COOKIE", "value": cookie},
            },
        )
        assert response.status_code == 201
        created = cast(dict[str, object], response.json())
        assert created["credential_kind"] == "COOKIE"
        assert created["credential_configured"] is True
        assert cookie not in response.text

        with app.state.runtime.session_factory() as session:
            site = session.scalar(select(Site).where(Site.type == "HHCLUB"))
            assert site is not None and site.secret_id is not None
            secret = session.get(SecretRecord, site.secret_id)
            assert secret is not None
            assert secret.kind == "SITE_COOKIE"
            assert cookie not in secret.ciphertext

        def handler(request: httpx2.Request) -> httpx2.Response:
            assert request.url.host == "hhanclub.net"
            assert request.url.path == "/index.php"
            assert request.headers.get("cookie") == cookie
            return httpx2.Response(200, text='<a href="usercp.php">profile</a>')

        app.state.site_service._adapter_factory = SiteAdapterFactory(  # noqa: SLF001
            transport=httpx2.MockTransport(handler)
        )
        tested = client.post(f"/api/v1/sites/{created['id']}/test", headers=_csrf(client))
        assert tested.status_code == 200
        assert tested.json()["capabilities"]["supports_imdb_id"] is True
        assert cookie not in tested.text
        refreshed = client.get(f"/api/v1/sites/{created['id']}")
        assert refreshed.json()["connection_status"] == "OK"
    finally:
        client.__exit__(None, None, None)


def test_site_health_reset_is_safe_and_versioned(tmp_path: Path) -> None:
    client, app = _authenticated_client(tmp_path)
    canary = "PACKBREAKER-SITE-HEALTH-CANARY-a72c"
    try:
        created = _create_site(client, canary)
        site_id = cast(str, created["id"])

        def auth_failure(_request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(401, text=f"remote secret {canary}")

        app.state.site_service._adapter_factory = SiteAdapterFactory(  # noqa: SLF001
            transport=httpx2.MockTransport(auth_failure)
        )
        failed = client.post(f"/api/v1/sites/{site_id}/test", headers=_csrf(client))
        assert failed.status_code == 502
        assert failed.json()["code"] == "SITE_AUTH_FAILED"
        assert canary not in failed.text

        health = client.get(f"/api/v1/sites/{site_id}/health")
        assert health.status_code == 200
        payload = health.json()
        assert payload["config_version"] == 1
        assert payload["circuit_state"] == "OPEN"
        assert payload["failure_count"] >= 1
        assert payload["last_error_code"] == "SITE_AUTH_FAILED"
        assert payload["requests_started"] == payload["requests_failed"] == 1
        assert "base_url" not in payload
        assert canary not in health.text

        no_csrf = client.post(
            f"/api/v1/sites/{site_id}/actions",
            headers={"If-Match": '"1"'},
            json={"action": "reset_circuit"},
        )
        assert no_csrf.status_code == 403
        assert no_csrf.json()["code"] == "CSRF_INVALID"

        missing_if_match = client.post(
            f"/api/v1/sites/{site_id}/actions",
            headers=_csrf(client),
            json={"action": "reset_circuit"},
        )
        assert missing_if_match.status_code == 428

        stale = client.post(
            f"/api/v1/sites/{site_id}/actions",
            headers={**_csrf(client), "If-Match": '"2"'},
            json={"action": "reset_circuit"},
        )
        assert stale.status_code == 412
        assert stale.json()["code"] == "SITE_VERSION_CONFLICT"

        read_health = client.get(f"/api/v1/sites/{site_id}/health")
        assert read_health.status_code == 200

        reset = client.post(
            f"/api/v1/sites/{site_id}/actions",
            headers={**_csrf(client), "If-Match": '"1"'},
            json={"action": "reset_circuit"},
        )
        assert reset.status_code == 200
        assert reset.headers["ETag"] == '"1"'
        reset_payload = reset.json()
        assert reset_payload["circuit_state"] == "CLOSED"
        assert reset_payload["failure_count"] == 0
        assert reset_payload["last_error_code"] is None
        assert reset_payload["requests_failed"] == 1
    finally:
        client.__exit__(None, None, None)


def test_site_rejects_mismatched_credential_kind_and_unsafe_type_switch(tmp_path: Path) -> None:
    client, _app = _authenticated_client(tmp_path)
    try:
        mismatch = client.post(
            "/api/v1/sites",
            headers=_csrf(client),
            json={
                "name": "错误 HDTime",
                "type": "HDTIME",
                "credential": {"kind": "API_KEY", "value": "synthetic"},
            },
        )
        assert mismatch.status_code == 422
        assert mismatch.json()["code"] == "SITE_CREDENTIAL_KIND_MISMATCH"

        created = _create_site(client)
        site_id = cast(str, created["id"])
        stale_credential = client.patch(
            f"/api/v1/sites/{site_id}",
            headers={**_csrf(client), "If-Match": '"1"'},
            json={"type": "HDTIME"},
        )
        assert stale_credential.status_code == 422
        assert stale_credential.json()["code"] == "SITE_CREDENTIAL_REPLACEMENT_REQUIRED"

        switched = client.patch(
            f"/api/v1/sites/{site_id}",
            headers={**_csrf(client), "If-Match": '"1"'},
            json={
                "type": "HDTIME",
                "clear_credential": True,
            },
        )
        assert switched.status_code == 200
        assert switched.json()["type"] == "HDTIME"
        assert switched.json()["credential_kind"] == "COOKIE"
        assert switched.json()["credential_configured"] is False
        assert switched.json()["connection_status"] == "UNTESTED"
        assert switched.json()["base_url"] == "https://hdtime.org"

        legacy_hdtime_host = client.post(
            "/api/v1/sites",
            headers=_csrf(client),
            json={
                "name": "伪 HDTime",
                "type": "HDTIME",
                "base_url": "https://hdtime.example",
                "credential": {"kind": "COOKIE", "value": "uid=1; pass=2"},
            },
        )
        assert legacy_hdtime_host.status_code == 201
        assert legacy_hdtime_host.json()["base_url"] == "https://hdtime.org"
    finally:
        client.__exit__(None, None, None)
