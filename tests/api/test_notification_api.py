from __future__ import annotations

from pathlib import Path

import httpx2
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from backend.app.api.dependencies import CSRF_COOKIE
from backend.app.config import AppSettings
from backend.app.infrastructure.adapters.notifications import NotificationProviderFactory
from backend.app.infrastructure.persistence.models import NotificationChannel, SecretRecord
from backend.app.main import create_app

_PASSWORD = "synthetic correct horse battery staple"


def _client(tmp_path: Path) -> tuple[TestClient, FastAPI]:
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


def test_notification_channel_secret_is_encrypted_and_never_returned(tmp_path: Path) -> None:
    client, app = _client(tmp_path)
    token = "123456:telegram-canary-secret"
    try:
        without_csrf = client.post(
            "/api/v1/notification-channels",
            json={
                "name": "主 Telegram",
                "type": "TELEGRAM",
                "telegram": {"bot_token": token, "chat_id": "-100123"},
            },
        )
        assert without_csrf.status_code == 403

        created = client.post(
            "/api/v1/notification-channels",
            headers=_csrf(client),
            json={
                "name": "主 Telegram",
                "type": "TELEGRAM",
                "telegram": {"bot_token": token, "chat_id": "-100123"},
                "task_link_base_url": "https://packbreaker.invalid",
                "aggregation_window_seconds": 120,
            },
        )
        assert created.status_code == 201
        payload = created.json()
        assert payload["credential_configured"] is True
        assert set(payload["event_types"]) == {
            "AUTH_LOGIN_SUCCESS",
            "AUTH_PASSWORD_CHANGED",
            "TASK_EXECUTION_RESULT",
            "DOWNLOADER_CREATED",
            "SITE_CREATED",
        }
        assert "task_link_base_url" not in payload
        assert "aggregation_window_seconds" not in payload
        assert token not in created.text

        listed = client.get("/api/v1/notification-channels")
        assert listed.status_code == 200
        assert token not in listed.text

        with app.state.runtime.session_factory() as session:
            secret = session.scalar(
                select(SecretRecord).where(SecretRecord.kind == "notification-credential")
            )
            assert secret is not None
            assert token not in secret.ciphertext
    finally:
        client.__exit__(None, None, None)


def test_notification_proxy_password_is_encrypted_and_never_returned(tmp_path: Path) -> None:
    client, app = _client(tmp_path)
    proxy_password = "notification-proxy-canary-p@ss"
    try:
        created = client.post(
            "/api/v1/notification-channels",
            headers=_csrf(client),
            json={
                "name": "代理 Telegram",
                "type": "TELEGRAM",
                "telegram": {"bot_token": "123456:synthetic", "chat_id": "-100123"},
                "event_types": ["TASK_EXECUTION_RESULT", "SITE_CREATED"],
                "proxy": {
                    "enabled": True,
                    "host": "proxy.example",
                    "port": 8080,
                    "username": "proxy-user",
                    "password": proxy_password,
                },
            },
        )

        assert created.status_code == 201
        payload = created.json()
        assert payload["event_types"] == ["TASK_EXECUTION_RESULT", "SITE_CREATED"]
        assert payload["proxy_enabled"] is True
        assert payload["proxy_host"] == "proxy.example"
        assert payload["proxy_port"] == 8080
        assert payload["proxy_username"] == "proxy-user"
        assert payload["proxy_credential_configured"] is True
        assert proxy_password not in created.text

        with app.state.runtime.session_factory() as session:
            proxy_secret = session.scalar(
                select(SecretRecord).where(SecretRecord.kind == "notification-proxy-password")
            )
            assert proxy_secret is not None
            assert proxy_password not in proxy_secret.ciphertext
    finally:
        client.__exit__(None, None, None)


def test_unsaved_notification_probe_does_not_persist_channel_or_secrets(tmp_path: Path) -> None:
    client, app = _client(tmp_path)
    token = "123456:temporary-probe-canary"
    proxy_password = "temporary-proxy-canary"
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json={"ok": True, "result": {"message_id": 1}})

    try:
        app.state.notification_service._provider_factory = NotificationProviderFactory(  # noqa: SLF001
            transport=httpx2.MockTransport(handler)
        )
        with app.state.runtime.session_factory() as session:
            before_channels = session.scalar(select(func.count()).select_from(NotificationChannel))
            before_secrets = session.scalar(select(func.count()).select_from(SecretRecord))

        probed = client.post(
            "/api/v1/notification-channels/probe",
            headers=_csrf(client),
            json={
                "type": "TELEGRAM",
                "telegram": {"bot_token": token, "chat_id": "-100456"},
                "proxy": {
                    "enabled": False,
                    "host": "proxy.example",
                    "port": 8080,
                    "username": "temporary-user",
                    "password": proxy_password,
                },
            },
        )

        assert probed.status_code == 200
        assert probed.json()["status"] == "ok"
        assert token not in probed.text
        assert proxy_password not in probed.text
        assert len(seen) == 1
        body = seen[0].content.decode("utf-8")
        assert "PackBreaker" in body
        assert token not in body
        assert proxy_password not in body

        with app.state.runtime.session_factory() as session:
            assert (
                session.scalar(select(func.count()).select_from(NotificationChannel))
                == before_channels
            )
            assert session.scalar(select(func.count()).select_from(SecretRecord)) == before_secrets
    finally:
        client.__exit__(None, None, None)


def test_notification_channel_test_and_enable_require_real_probe(tmp_path: Path) -> None:
    client, app = _client(tmp_path)
    try:
        created = client.post(
            "/api/v1/notification-channels",
            headers=_csrf(client),
            json={
                "name": "Server酱",
                "type": "SERVERCHAN",
                "serverchan": {"send_key": "SCTsynthetic"},
            },
        )
        assert created.status_code == 201
        channel_id = created.json()["id"]

        blocked = client.post(
            f"/api/v1/notification-channels/{channel_id}/actions",
            headers={**_csrf(client), "If-Match": '"1"'},
            json={"action": "enable"},
        )
        assert blocked.status_code == 409

        def handler(_request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(200, json={"code": 0})

        app.state.notification_service._provider_factory = NotificationProviderFactory(  # noqa: SLF001
            transport=httpx2.MockTransport(handler)
        )
        tested = client.post(
            f"/api/v1/notification-channels/{channel_id}/test",
            headers=_csrf(client),
        )
        assert tested.status_code == 200

        enabled = client.post(
            f"/api/v1/notification-channels/{channel_id}/actions",
            headers={**_csrf(client), "If-Match": '"1"'},
            json={"action": "enable"},
        )
        assert enabled.status_code == 200
        assert enabled.json()["enabled"] is True
    finally:
        client.__exit__(None, None, None)
