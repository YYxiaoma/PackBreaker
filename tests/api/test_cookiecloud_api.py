from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping
from pathlib import Path

import httpx2
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app.api.dependencies import CSRF_COOKIE
from backend.app.application.cookiecloud import CookieCloudService
from backend.app.config import AppSettings
from backend.app.infrastructure.cookiecloud import CookieCloudClient
from backend.app.infrastructure.persistence.models import CookieCloudSetting, SecretRecord, Site
from backend.app.main import create_app

_PASSWORD = "synthetic correct horse battery staple"
_COOKIECLOUD_PASSWORD = "synthetic-cookiecloud-password"


def _settings(tmp_path: Path) -> AppSettings:
    return AppSettings(
        config_dir=(tmp_path / "config").resolve(),
        data_dir=(tmp_path / "data").resolve(),
    )


def _login(client: TestClient) -> dict[str, str]:
    assert client.post("/api/v1/auth/setup", json={"password": _PASSWORD}).status_code == 201
    assert (
        client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": _PASSWORD},
        ).status_code
        == 200
    )
    csrf = client.cookies.get(CSRF_COOKIE)
    assert csrf is not None
    return {"X-CSRF-Token": csrf}


def _fixed_encrypt(uuid: str, password: str, payload: Mapping[str, object]) -> str:
    key = (
        hashlib.md5(  # noqa: S324 - easychen/CookieCloud protocol fixture
            f"{uuid}-{password}".encode(),
            usedforsecurity=False,
        )
        .hexdigest()[:16]
        .encode()
    )
    padder = padding.PKCS7(128).padder()
    padded = padder.update(json.dumps(payload).encode()) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(b"\x00" * 16)).encryptor()
    return base64.b64encode(encryptor.update(padded) + encryptor.finalize()).decode()


def test_cookiecloud_cron_preview_validates_and_returns_next_runs(tmp_path: Path) -> None:
    app = create_app(settings=_settings(tmp_path))
    with TestClient(app, base_url="https://testserver") as client:
        headers = _login(client)
        preview = client.post(
            "/api/v1/cookiecloud/cron-preview",
            headers=headers,
            json={"cron_expression": "0 */6 * * *"},
        )
        assert preview.status_code == 200
        payload = preview.json()
        assert payload["cron_expression"] == "0 */6 * * *"
        assert payload["timezone"] == "Asia/Shanghai"
        assert len(payload["next_runs"]) == 5
        assert payload["description"] == "每 6 小时的第 0 分钟"

        invalid = client.post(
            "/api/v1/cookiecloud/cron-preview",
            headers=headers,
            json={"cron_expression": "not a cron"},
        )
        assert invalid.status_code == 422
        assert invalid.json()["code"] == "COOKIECLOUD_CRON_INVALID"


def test_cookiecloud_config_encrypts_password_and_never_echoes_it(tmp_path: Path) -> None:
    app = create_app(settings=_settings(tmp_path))
    with TestClient(app, base_url="https://testserver") as client:
        headers = _login(client)
        current = client.get("/api/v1/cookiecloud/config")
        assert current.status_code == 200
        assert current.json()["password_configured"] is False
        assert current.json()["last_sync_status"] == "NEVER"

        saved = client.put(
            "/api/v1/cookiecloud/config",
            headers={**headers, "If-Match": current.headers["etag"]},
            json={
                "enabled": True,
                "server_url": "https://cookie.example.test/root/",
                "uuid": "synthetic-uuid",
                "password_action": "SET",
                "password": _COOKIECLOUD_PASSWORD,
                "auto_sync": True,
                "sync_cron_expression": "*/30 * * * *",
                "request_timeout_seconds": 15,
            },
        )
        assert saved.status_code == 200
        payload = saved.json()
        assert payload["server_url"] == "https://cookie.example.test/root"
        assert payload["password_configured"] is True
        assert payload["connection_status"] == "UNTESTED"
        assert _COOKIECLOUD_PASSWORD not in saved.text
        assert "password" not in payload

        with app.state.runtime.session_factory() as session:
            setting = session.get(CookieCloudSetting, "default")
            assert setting is not None and setting.password_secret_id is not None
            secret = session.get(SecretRecord, setting.password_secret_id)
            assert secret is not None and secret.kind == "COOKIECLOUD_PASSWORD"
            assert _COOKIECLOUD_PASSWORD not in secret.ciphertext


def test_cookiecloud_saved_probe_uses_get_uuid_and_local_decryption(tmp_path: Path) -> None:
    uuid = "synthetic uuid"
    cookie_payload = {
        "cookie_data": {
            ".example.test": [
                {
                    "name": "session",
                    "value": "synthetic-cookie-value",
                    "domain": ".example.test",
                    "path": "/",
                }
            ]
        },
        "local_storage_data": {},
        "update_time": "2026-09-30T12:00:00.000Z",
    }
    encrypted = _fixed_encrypt(uuid, _COOKIECLOUD_PASSWORD, cookie_payload)
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        assert str(request.url) == "https://cookie.example.test/root/get/synthetic%20uuid"
        assert _COOKIECLOUD_PASSWORD not in str(request.url)
        return httpx2.Response(
            200,
            json={"encrypted": encrypted, "crypto_type": "aes-128-cbc-fixed"},
        )

    app = create_app(settings=_settings(tmp_path))
    with TestClient(app, base_url="https://testserver") as client:
        headers = _login(client)
        current = client.get("/api/v1/cookiecloud/config")
        saved = client.put(
            "/api/v1/cookiecloud/config",
            headers={**headers, "If-Match": current.headers["etag"]},
            json={
                "enabled": True,
                "server_url": "https://cookie.example.test/root",
                "uuid": uuid,
                "password_action": "SET",
                "password": _COOKIECLOUD_PASSWORD,
                "auto_sync": True,
                "sync_cron_expression": "*/30 * * * *",
                "request_timeout_seconds": 15,
            },
        )
        assert saved.status_code == 200
        app.state.cookiecloud_service = CookieCloudService(
            app.state.runtime.session_factory,
            app.state.secret_store,
            client=CookieCloudClient(transport=httpx2.MockTransport(handler)),
        )

        tested = client.post("/api/v1/cookiecloud/test", headers=headers)
        assert tested.status_code == 200
        result = tested.json()
        assert result["status"] == "ok"
        assert result["crypto_type"] == "aes-128-cbc-fixed"
        assert result["domain_count"] == 1
        assert result["cookie_count"] == 1
        assert result["update_time"] == "2026-09-30T12:00:00.000Z"
        assert "synthetic-cookie-value" not in tested.text
        assert _COOKIECLOUD_PASSWORD not in tested.text
        assert len(requests) == 1

        refreshed = client.get("/api/v1/cookiecloud/config")
        assert refreshed.json()["connection_status"] == "OK"
        assert refreshed.json()["last_test_at"] is not None


def test_cookiecloud_wrong_password_error_does_not_leak_secrets(tmp_path: Path) -> None:
    encrypted = _fixed_encrypt(
        "synthetic-uuid",
        "actual-password",
        {"cookie_data": {}, "local_storage_data": {}},
    )

    def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            json={"encrypted": encrypted, "crypto_type": "aes-128-cbc-fixed"},
        )

    app = create_app(settings=_settings(tmp_path))
    with TestClient(app, base_url="https://testserver") as client:
        headers = _login(client)
        current = client.get("/api/v1/cookiecloud/config")
        saved = client.put(
            "/api/v1/cookiecloud/config",
            headers={**headers, "If-Match": current.headers["etag"]},
            json={
                "enabled": True,
                "server_url": "https://cookie.example.test",
                "uuid": "synthetic-uuid",
                "password_action": "SET",
                "password": "wrong-password",
                "auto_sync": False,
                "sync_cron_expression": "*/30 * * * *",
                "request_timeout_seconds": 15,
            },
        )
        assert saved.status_code == 200
        app.state.cookiecloud_service = CookieCloudService(
            app.state.runtime.session_factory,
            app.state.secret_store,
            client=CookieCloudClient(transport=httpx2.MockTransport(handler)),
        )

        tested = client.post("/api/v1/cookiecloud/test", headers=headers)
        assert tested.status_code == 422
        assert tested.json()["code"] == "COOKIECLOUD_DECRYPT_FAILED"
        assert "wrong-password" not in tested.text
        assert "actual-password" not in tested.text
        assert encrypted not in tested.text
        assert client.get("/api/v1/cookiecloud/config").json()["connection_status"] == "FAILED"


def test_cookiecloud_sync_updates_cookie_sites_and_preserves_rousi_api_key(tmp_path: Path) -> None:
    uuid = "sync-uuid"
    payload = {
        "cookie_data": {
            "https://keepfrds.com/": [
                {
                    "name": "session",
                    "value": "cookiecloud-keepfrds",
                    "domain": "https://keepfrds.com/",
                    "path": "/",
                    "expirationDate": 0,
                }
            ],
            ".rousi.pro": [
                {
                    "name": "rousi_session",
                    "value": "cookiecloud-rousi",
                    "domain": ".rousi.pro",
                    "path": "/",
                }
            ],
            ".outside.invalid": [
                {
                    "name": "ignored",
                    "value": "foreign",
                    "domain": ".outside.invalid",
                    "path": "/",
                }
            ],
        },
        "local_storage_data": {},
        "update_time": "2026-09-30T13:00:00.000Z",
    }
    encrypted = _fixed_encrypt(uuid, _COOKIECLOUD_PASSWORD, payload)

    def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            json={"encrypted": encrypted, "crypto_type": "aes-128-cbc-fixed"},
        )

    app = create_app(settings=_settings(tmp_path))
    with TestClient(app, base_url="https://testserver") as client:
        headers = _login(client)
        keepfrds = client.post(
            "/api/v1/sites",
            headers=headers,
            json={
                "name": "KeepFrds",
                "type": "KEEPFRDS",
                "credential": {"kind": "COOKIE", "value": "session=old-keepfrds"},
            },
        )
        assert keepfrds.status_code == 201
        keepfrds_id = keepfrds.json()["id"]
        enabled = client.post(
            f"/api/v1/sites/{keepfrds_id}/actions",
            headers={**headers, "If-Match": keepfrds.headers["etag"]},
            json={"action": "enable"},
        )
        assert enabled.status_code == 200 and enabled.json()["enabled"] is True

        rousi = client.post(
            "/api/v1/sites",
            headers=headers,
            json={
                "name": "Rousi Pro",
                "type": "ROUSI_PRO",
                "credential": {"kind": "API_KEY", "value": "rousi-api-key"},
                "download_cookie": "rousi_session=old",
            },
        )
        assert rousi.status_code == 201
        rousi_id = rousi.json()["id"]
        enabled = client.post(
            f"/api/v1/sites/{rousi_id}/actions",
            headers={**headers, "If-Match": rousi.headers["etag"]},
            json={"action": "enable"},
        )
        assert enabled.status_code == 200 and enabled.json()["enabled"] is True

        current = client.get("/api/v1/cookiecloud/config")
        saved = client.put(
            "/api/v1/cookiecloud/config",
            headers={**headers, "If-Match": current.headers["etag"]},
            json={
                "enabled": True,
                "server_url": "https://cookie.example.test",
                "uuid": uuid,
                "password_action": "SET",
                "password": _COOKIECLOUD_PASSWORD,
                "auto_sync": True,
                "sync_cron_expression": "*/30 * * * *",
                "request_timeout_seconds": 15,
            },
        )
        assert saved.status_code == 200
        app.state.cookiecloud_service = CookieCloudService(
            app.state.runtime.session_factory,
            app.state.secret_store,
            client=CookieCloudClient(transport=httpx2.MockTransport(handler)),
            site_service=app.state.site_service,
        )

        synced = client.post("/api/v1/cookiecloud/sync", headers=headers)
        assert synced.status_code == 200
        report = synced.json()
        assert report["source_domains"] == 3
        assert report["source_cookies"] == 3
        assert report["eligible_sites"] == 2
        assert report["matched_sites"] == 2
        assert report["updated_sites"] == 2
        assert report["unchanged_sites"] == 0
        assert report["unmatched_domains"] == 1
        assert "cookiecloud-keepfrds" not in synced.text
        assert "cookiecloud-rousi" not in synced.text

        refreshed = client.get("/api/v1/cookiecloud/config").json()
        assert refreshed["source_domains"] == 3
        assert refreshed["source_cookies"] == 3
        assert refreshed["eligible_sites"] == 2
        assert refreshed["matched_sites"] == 2
        assert refreshed["updated_sites"] == 2
        assert refreshed["unchanged_sites"] == 0

        with app.state.runtime.session_factory() as session:
            sites = {
                row.type: row
                for row in session.scalars(select(Site).where(Site.id.in_([keepfrds_id, rousi_id])))
            }
            keepfrds_row = sites["KEEPFRDS"]
            rousi_row = sites["ROUSI_PRO"]
            assert keepfrds_row.enabled is True and keepfrds_row.secret_id is not None
            assert rousi_row.enabled is True
            assert rousi_row.secret_id is not None and rousi_row.download_secret_id is not None
            assert (
                app.state.secret_store.get(
                    keepfrds_row.secret_id,
                    expected_kind="SITE_COOKIE",
                )
                == b"session=cookiecloud-keepfrds"
            )
            assert (
                app.state.secret_store.get(
                    rousi_row.secret_id,
                    expected_kind="SITE_API_KEY",
                )
                == b"rousi-api-key"
            )
            assert (
                app.state.secret_store.get(
                    rousi_row.download_secret_id,
                    expected_kind="SITE_DOWNLOAD_COOKIE",
                )
                == b"rousi_session=cookiecloud-rousi"
            )

        second = client.post("/api/v1/cookiecloud/sync", headers=headers)
        assert second.status_code == 200
        assert second.json()["matched_sites"] == 2
        assert second.json()["updated_sites"] == 0


def test_cookiecloud_sync_imports_missing_supported_cookie_sites_without_api_keys(
    tmp_path: Path,
) -> None:
    from urllib.parse import urlsplit

    from backend.app.domain.site_config import SiteCredentialKind, site_profiles

    profiles = site_profiles()
    payload = {
        "cookie_data": {
            urlsplit(profile.base_url).hostname: [
                {
                    "name": "session",
                    "value": "sensitive_" + profile.kind.value,
                    "domain": urlsplit(profile.base_url).hostname,
                    "path": "/",
                }
            ]
            for profile in profiles
        },
        "local_storage_data": {},
    }
    encrypted = _fixed_encrypt("import-uuid", _COOKIECLOUD_PASSWORD, payload)
    transport = httpx2.MockTransport(
        lambda _request: httpx2.Response(
            200, json={"encrypted": encrypted, "crypto_type": "aes-128-cbc-fixed"}
        )
    )
    app = create_app(settings=_settings(tmp_path))
    with TestClient(app, base_url="https://testserver") as client:
        headers = _login(client)
        current = client.get("/api/v1/cookiecloud/config")
        saved = client.put(
            "/api/v1/cookiecloud/config",
            headers={**headers, "If-Match": current.headers["etag"]},
            json={
                "enabled": True,
                "server_url": "https://cookie.example.test",
                "uuid": "import-uuid",
                "password_action": "SET",
                "password": _COOKIECLOUD_PASSWORD,
                "auto_sync": False,
                "sync_cron_expression": "*/30 * * * *",
            },
        )
        assert saved.status_code == 200
        app.state.cookiecloud_service = CookieCloudService(
            app.state.runtime.session_factory,
            app.state.secret_store,
            client=CookieCloudClient(transport=transport),
            site_service=app.state.site_service,
        )
        synced = client.post("/api/v1/cookiecloud/sync", headers=headers)
        assert synced.status_code == 200
        report = synced.json()
        expected_cookies = sum(
            profile.credential_kind is SiteCredentialKind.COOKIE for profile in profiles
        )
        assert report["created_sites"] == expected_cookies + 1
        assert report["updated_sites"] == expected_cookies
        assert sorted(report["skipped_api_key_sites"]) == ["M-TEAM", "Rousi Pro"]
        assert report["matched_sites"] == expected_cookies
        imported = client.get("/api/v1/sites").json()["items"]
        assert len(imported) == expected_cookies + 1
        assert all(not row["enabled"] for row in imported)
        mteam = next(row for row in imported if row["type"] == "MTEAM")
        assert mteam["credential_configured"] is False
        assert "sensitive_" not in synced.text

        repeated = client.post("/api/v1/cookiecloud/sync", headers=headers)
        assert repeated.status_code == 200
        assert repeated.json()["created_sites"] == 0
        assert repeated.json()["updated_sites"] == 0
        assert repeated.json()["unchanged_sites"] == expected_cookies


def test_cookiecloud_sync_seven_v111_sites_are_encrypted_and_remain_disabled(
    tmp_path: Path,
) -> None:
    """The seven unaccepted adapters may be configured but must not seed."""
    from urllib.parse import urlsplit

    from backend.app.domain.site_config import SiteKind, site_profile

    kinds = (
        SiteKind.PTERCLUB,
        SiteKind.AUDIENCES,
        SiteKind.SPRING_SUNDAY,
        SiteKind.HDDOLBY,
        SiteKind.U2,
        SiteKind.TANGPT,
        SiteKind.CARPT,
    )
    cookie_data: dict[str, list[dict[str, object]]] = {}
    for kind in kinds:
        host = urlsplit(site_profile(kind).base_url).hostname
        assert host is not None
        cookie_data[host] = [
            {
                "name": "synthetic_session",
                "value": "PRIVATE_SYNTHETIC_" + kind.value,
                "domain": host,
                "path": "/",
            }
        ]
    # A similarly named domain must never be promoted into a site cookie.
    cookie_data["pterclub.net.evil.invalid"] = [
        {
            "name": "foreign",
            "value": "UNTRUSTED_NOT_A_SITE_COOKIE",
            "domain": "pterclub.net.evil.invalid",
            "path": "/",
        }
    ]
    uuid = "v111-seven-site-uuid"
    encrypted = _fixed_encrypt(
        uuid,
        _COOKIECLOUD_PASSWORD,
        {"cookie_data": cookie_data, "local_storage_data": {}},
    )
    requests: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(str(request.url))
        assert str(request.url) == "https://cookie.example.test/get/" + uuid
        return httpx2.Response(
            200,
            json={"encrypted": encrypted, "crypto_type": "aes-128-cbc-fixed"},
        )

    app = create_app(settings=_settings(tmp_path))
    with TestClient(app, base_url="https://testserver") as client:
        headers = _login(client)
        config = client.get("/api/v1/cookiecloud/config")
        configured = client.put(
            "/api/v1/cookiecloud/config",
            headers={**headers, "If-Match": config.headers["etag"]},
            json={
                "enabled": True,
                "server_url": "https://cookie.example.test",
                "uuid": uuid,
                "password_action": "SET",
                "password": _COOKIECLOUD_PASSWORD,
                "auto_sync": False,
                "sync_cron_expression": "0 */6 * * *",
            },
        )
        assert configured.status_code == 200
        app.state.cookiecloud_service = CookieCloudService(
            app.state.runtime.session_factory,
            app.state.secret_store,
            client=CookieCloudClient(transport=httpx2.MockTransport(handler)),
            site_service=app.state.site_service,
        )

        synced = client.post("/api/v1/cookiecloud/sync", headers=headers)
        assert synced.status_code == 200
        summary = synced.json()
        assert summary["created_sites"] == len(kinds)
        assert summary["matched_sites"] == len(kinds)
        assert summary["updated_sites"] == len(kinds)
        assert summary["unmatched_domains"] == 1
        assert len(requests) == 1
        for kind in kinds:
            assert "PRIVATE_SYNTHETIC_" + kind.value not in synced.text
        assert "UNTRUSTED_NOT_A_SITE_COOKIE" not in synced.text

        profile_result = client.get("/api/v1/sites/profiles")
        assert profile_result.status_code == 200
        available = {profile["kind"]: profile for profile in profile_result.json()["items"]}
        sites_response = client.get("/api/v1/sites")
        assert sites_response.status_code == 200
        sites = {site["type"]: site for site in sites_response.json()["items"]}
        assert set(sites) == {kind.value for kind in kinds}
        for kind in kinds:
            assert available[kind.value]["support_status"] == "PENDING_REAL_VALIDATION"
            site = sites[kind.value]
            assert site["enabled"] is False
            assert site["credential_configured"] is True
            assert "PRIVATE_SYNTHETIC_" not in json.dumps(site)
            blocked = client.post(
                f"/api/v1/sites/{site['id']}/actions",
                headers={**headers, "If-Match": f'"{site["version"]}"'},
                json={"action": "enable"},
            )
            assert blocked.status_code == 409
            assert blocked.json()["code"] == "SITE_ADAPTER_PENDING"

        with app.state.runtime.session_factory() as session:
            stored = session.scalars(select(Site)).all()
            assert len(stored) == len(kinds)
            for site in stored:
                assert site.enabled is False
                assert site.secret_id is not None
                assert (
                    app.state.secret_store.get(site.secret_id)
                    == ("synthetic_session=PRIVATE_SYNTHETIC_" + site.type).encode()
                )
                assert site.secret_id not in synced.text

        repeated = client.post("/api/v1/cookiecloud/sync", headers=headers)
        assert repeated.status_code == 200
        assert repeated.json()["created_sites"] == 0
        assert repeated.json()["updated_sites"] == 0
        assert repeated.json()["unchanged_sites"] == len(kinds)


def test_cookiecloud_mteam_sibling_domain_imports_disabled_placeholder_only(tmp_path: Path) -> None:
    uuid = "mteam-cookie-uuid"
    encrypted = _fixed_encrypt(
        uuid,
        _COOKIECLOUD_PASSWORD,
        {
            "cookie_data": {
                "https://www.m-team.cc/": [
                    {
                        "name": "session",
                        "value": "SECRET_BROWSER_COOKIE_DO_NOT_PROMOTE",
                        "domain": ".www.m-team.cc",
                        "path": "/",
                    }
                ]
            },
            "local_storage_data": {},
        },
    )
    transport = httpx2.MockTransport(
        lambda _request: httpx2.Response(
            200, json={"encrypted": encrypted, "crypto_type": "aes-128-cbc-fixed"}
        )
    )
    app = create_app(settings=_settings(tmp_path))
    with TestClient(app, base_url="https://testserver") as client:
        headers = _login(client)
        current = client.get("/api/v1/cookiecloud/config")
        response = client.put(
            "/api/v1/cookiecloud/config",
            headers={**headers, "If-Match": current.headers["etag"]},
            json={
                "enabled": True,
                "server_url": "https://cookie.example.test",
                "uuid": uuid,
                "password_action": "SET",
                "password": _COOKIECLOUD_PASSWORD,
                "auto_sync": False,
                "sync_cron_expression": "*/30 * * * *",
            },
        )
        assert response.status_code == 200
        app.state.cookiecloud_service = CookieCloudService(
            app.state.runtime.session_factory,
            app.state.secret_store,
            client=CookieCloudClient(transport=transport),
            site_service=app.state.site_service,
        )
        synced = client.post("/api/v1/cookiecloud/sync", headers=headers)
        assert synced.status_code == 200
        assert synced.json()["created_sites"] == 1
        assert synced.json()["updated_sites"] == 0
        assert synced.json()["matched_sites"] == 0
        assert synced.json()["skipped_api_key_sites"] == ["M-TEAM"]
        assert "SECRET_BROWSER_COOKIE_DO_NOT_PROMOTE" not in synced.text
        sites = client.get("/api/v1/sites").json()["items"]
        assert len(sites) == 1
        assert sites[0]["type"] == "MTEAM"
        assert sites[0]["enabled"] is False
        assert sites[0]["credential_configured"] is False
        repeated = client.post("/api/v1/cookiecloud/sync", headers=headers)
        assert repeated.status_code == 200
        assert repeated.json()["created_sites"] == 0
