from __future__ import annotations

from pathlib import Path
from typing import cast

import httpx2
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app.api.dependencies import CSRF_COOKIE
from backend.app.config import AppSettings
from backend.app.infrastructure.adapters.sites import SiteAdapterFactory
from backend.app.infrastructure.persistence.models import SecretRecord, Site
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
        }
        assert items["MTEAM"]["base_url"] == "https://kp.m-team.cc"
        assert items["HDTIME"]["base_url"] == "https://hdtime.org"
        assert items["HHCLUB"]["base_url"] == "https://hhanclub.net"
        assert items["HDTIME"]["supports_user_agent"] is True
        assert items["HDTIME"]["supports_browser_emulation"] is True
        assert items["KEEPFRDS"]["support_status"] == "PENDING_ADAPTER"
        assert items["ROUSI_PRO"]["credential_kind"] == "API_KEY"
    finally:
        client.__exit__(None, None, None)


def test_pending_site_profiles_cannot_create_or_probe_or_persist_secrets(tmp_path: Path) -> None:
    client, app = _authenticated_client(tmp_path)
    try:
        with app.state.runtime.session_factory() as session:
            before_sites = len(list(session.scalars(select(Site))))
            before_secrets = len(list(session.scalars(select(SecretRecord))))

        created = client.post(
            "/api/v1/sites",
            headers=_csrf(client),
            json={
                "name": "KeepFrds 待适配",
                "type": "KEEPFRDS",
                "credential": {"kind": "COOKIE", "value": "synthetic-cookie"},
            },
        )
        assert created.status_code == 409
        assert created.json()["code"] == "SITE_ADAPTER_PENDING"

        probe = client.post(
            "/api/v1/sites/probe",
            headers=_csrf(client),
            json={
                "type": "ROUSI_PRO",
                "credential": {"kind": "API_KEY", "value": "synthetic-passkey"},
            },
        )
        assert probe.status_code == 409
        assert probe.json()["code"] == "SITE_ADAPTER_PENDING"

        with app.state.runtime.session_factory() as session:
            assert len(list(session.scalars(select(Site)))) == before_sites
            assert len(list(session.scalars(select(SecretRecord)))) == before_secrets
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


def test_site_user_profile_is_fetched_only_when_detail_endpoint_is_requested(
    tmp_path: Path,
) -> None:
    client, app = _authenticated_client(tmp_path)
    api_key = "PACKBREAKER-PROFILE-CANARY-c102"
    requests: list[str] = []
    try:
        created = _create_site(client, api_key)

        def handler(request: httpx2.Request) -> httpx2.Response:
            requests.append(request.url.path)
            assert request.headers.get("x-api-key") == api_key
            return httpx2.Response(
                200,
                json={
                    "code": "0",
                    "data": {
                        "id": "123",
                        "username": "SyntheticUser",
                        "stats": {"uploaded": "200", "downloaded": "100", "seeding": "8"},
                    },
                },
            )

        app.state.site_service._adapter_factory = SiteAdapterFactory(  # noqa: SLF001
            transport=httpx2.MockTransport(handler)
        )

        listed = client.get("/api/v1/sites")
        assert listed.status_code == 200
        assert requests == []

        profile = client.get(f"/api/v1/sites/{created['id']}/profile")
        assert profile.status_code == 200
        body = profile.json()
        assert requests == ["/api/member/profile"]
        assert body["site_id"] == "mteam"
        assert body["uid"] == "123"
        assert body["username"] == "SyntheticUser"
        assert body["uploaded_bytes"] == 200
        assert body["downloaded_bytes"] == 100
        assert body["ratio"] == 2.0
        assert body["seeding_count"] == 8
        assert body["real_uploaded_bytes"] is None
        assert body["bonus_per_hour"] is None
        assert api_key not in profile.text
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
