from __future__ import annotations

from pathlib import Path

import httpx2
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app.api.dependencies import CSRF_COOKIE
from backend.app.config import AppSettings
from backend.app.infrastructure.adapters.notifications import NotificationProviderFactory
from backend.app.infrastructure.persistence.models import SecretRecord
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
    assert client.post("/api/v1/auth/setup", json={"password": _PASSWORD}).status_code == 201
    assert client.post("/api/v1/auth/login", json={"password": _PASSWORD}).status_code == 200
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
