from __future__ import annotations

from pathlib import Path

import httpx2
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app.api.dependencies import CSRF_COOKIE
from backend.app.application.ai_agent import AIAgentService
from backend.app.config import AppSettings
from backend.app.infrastructure.adapters.ai_provider import AIProviderFactory
from backend.app.infrastructure.persistence.models import AIAgentSetting, SecretRecord
from backend.app.main import create_app

_PASSWORD = "synthetic correct horse battery staple"
_API_KEY = "sk-synthetic-ai-canary"


def _settings(tmp_path: Path) -> AppSettings:
    return AppSettings(
        config_dir=(tmp_path / "config").resolve(),
        data_dir=(tmp_path / "data").resolve(),
    )


def _login(client: TestClient) -> dict[str, str]:
    assert client.post("/api/v1/auth/setup", json={"password": _PASSWORD}).status_code == 201
    assert (
        client.post(
            "/api/v1/auth/login", json={"username": "admin", "password": _PASSWORD}
        ).status_code
        == 200
    )
    csrf = client.cookies.get(CSRF_COOKIE)
    assert csrf is not None
    return {"X-CSRF-Token": csrf}


def test_ai_agent_settings_store_api_key_as_secret_and_never_echo_it(tmp_path: Path) -> None:
    app = create_app(settings=_settings(tmp_path))
    with TestClient(app, base_url="https://testserver") as client:
        headers = _login(client)
        current = client.get("/api/v1/ai-agent/settings")
        assert current.status_code == 200
        assert current.json()["api_key_configured"] is False

        response = client.put(
            "/api/v1/ai-agent/settings",
            headers={**headers, "If-Match": current.headers["etag"]},
            json={
                "enabled": False,
                "provider_kind": "OPENAI_COMPATIBLE",
                "base_url": "https://provider.example/v1",
                "model": "synthetic-model",
                "request_timeout_seconds": 15,
                "max_context_messages": 20,
                "data_scopes": ["SYSTEM_HEALTH", "VERSION_STATUS"],
                "api_key_action": "SET",
                "api_key": _API_KEY,
            },
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["api_key_configured"] is True
        assert payload["connection_status"] == "UNTESTED"
        assert _API_KEY not in response.text
        with app.state.runtime.session_factory() as session:
            setting = session.get(AIAgentSetting, "default")
            assert setting is not None and setting.api_key_secret_id is not None
            secret = session.get(SecretRecord, setting.api_key_secret_id)
            assert secret is not None
            assert secret.kind == "AI_PROVIDER_API_KEY"
            assert _API_KEY not in secret.ciphertext


def test_ai_agent_temporary_probe_does_not_persist_key_or_config(tmp_path: Path) -> None:
    requests: list[httpx2.Request] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(200, json={"id": "temporary-model", "object": "model"})

    app = create_app(settings=_settings(tmp_path))
    with TestClient(app, base_url="https://testserver") as client:
        headers = _login(client)
        app.state.ai_agent_service = AIAgentService(
            app.state.runtime.session_factory,
            app.state.secret_store,
            provider_factory=AIProviderFactory(transport=httpx2.MockTransport(handler)),
        )
        with app.state.runtime.session_factory() as session:
            before_secrets = len(session.scalars(select(SecretRecord)).all())
            before = session.get(AIAgentSetting, "default")
            assert before is not None
            before_version = before.version

        response = client.post(
            "/api/v1/ai-agent/test",
            headers=headers,
            json={
                "provider_kind": "OPENAI_COMPATIBLE",
                "base_url": "https://temporary.example/v1",
                "model": "temporary-model",
                "request_timeout_seconds": 8,
                "api_key": _API_KEY,
            },
        )

        assert response.status_code == 200
        assert response.json()["model"] == "temporary-model"
        assert requests[0].headers["authorization"] == f"Bearer {_API_KEY}"
        assert _API_KEY not in response.text
        with app.state.runtime.session_factory() as session:
            after_secrets = len(session.scalars(select(SecretRecord)).all())
            after = session.get(AIAgentSetting, "default")
            assert after is not None
            assert after.version == before_version
            assert after.base_url == "https://api.openai.com/v1"
        assert after_secrets == before_secrets


def test_ai_agent_saved_probe_persists_ok_then_allows_enable(tmp_path: Path) -> None:
    async def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"id": "synthetic-model", "object": "model"})

    app = create_app(settings=_settings(tmp_path))
    with TestClient(app, base_url="https://testserver") as client:
        headers = _login(client)
        app.state.ai_agent_service = AIAgentService(
            app.state.runtime.session_factory,
            app.state.secret_store,
            provider_factory=AIProviderFactory(transport=httpx2.MockTransport(handler)),
        )
        current = client.get("/api/v1/ai-agent/settings")
        saved = client.put(
            "/api/v1/ai-agent/settings",
            headers={**headers, "If-Match": current.headers["etag"]},
            json={
                "enabled": False,
                "provider_kind": "OPENAI_COMPATIBLE",
                "base_url": "https://provider.example/v1",
                "model": "synthetic-model",
                "request_timeout_seconds": 15,
                "max_context_messages": 12,
                "data_scopes": ["SYSTEM_HEALTH"],
                "api_key_action": "SET",
                "api_key": _API_KEY,
            },
        )
        assert saved.status_code == 200

        probe = client.post("/api/v1/ai-agent/test", headers=headers, json={"use_saved": True})
        assert probe.status_code == 200

        tested = client.get("/api/v1/ai-agent/settings")
        assert tested.json()["connection_status"] == "OK"
        enabled = client.put(
            "/api/v1/ai-agent/settings",
            headers={**headers, "If-Match": tested.headers["etag"]},
            json={
                "enabled": True,
                "provider_kind": "OPENAI_COMPATIBLE",
                "base_url": "https://provider.example/v1",
                "model": "synthetic-model",
                "request_timeout_seconds": 15,
                "max_context_messages": 12,
                "data_scopes": ["SYSTEM_HEALTH"],
                "api_key_action": "KEEP",
            },
        )
        assert enabled.status_code == 200
        assert enabled.json()["enabled"] is True


def test_ai_agent_connection_changes_disable_and_invalidate_previous_probe(tmp_path: Path) -> None:
    async def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"id": "model-a", "object": "model"})

    app = create_app(settings=_settings(tmp_path))
    with TestClient(app, base_url="https://testserver") as client:
        headers = _login(client)
        app.state.ai_agent_service = AIAgentService(
            app.state.runtime.session_factory,
            app.state.secret_store,
            provider_factory=AIProviderFactory(transport=httpx2.MockTransport(handler)),
        )
        current = client.get("/api/v1/ai-agent/settings")
        saved = client.put(
            "/api/v1/ai-agent/settings",
            headers={**headers, "If-Match": current.headers["etag"]},
            json={
                "enabled": False,
                "provider_kind": "OPENAI_COMPATIBLE",
                "base_url": "https://provider.example/v1",
                "model": "model-a",
                "request_timeout_seconds": 15,
                "max_context_messages": 10,
                "data_scopes": ["SYSTEM_HEALTH"],
                "api_key_action": "SET",
                "api_key": _API_KEY,
            },
        )
        assert saved.status_code == 200
        assert (
            client.post(
                "/api/v1/ai-agent/test", headers=headers, json={"use_saved": True}
            ).status_code
            == 200
        )
        tested = client.get("/api/v1/ai-agent/settings")

        changed = client.put(
            "/api/v1/ai-agent/settings",
            headers={**headers, "If-Match": tested.headers["etag"]},
            json={
                "enabled": False,
                "provider_kind": "OPENAI_COMPATIBLE",
                "base_url": "https://provider.example/v1",
                "model": "model-b",
                "request_timeout_seconds": 15,
                "max_context_messages": 10,
                "data_scopes": ["SYSTEM_HEALTH"],
                "api_key_action": "KEEP",
            },
        )

        assert changed.status_code == 200
        assert changed.json()["connection_status"] == "UNTESTED"
        assert changed.json()["enabled"] is False
        assert changed.json()["last_test_at"] is None


def test_ai_telegram_binding_accepts_only_telegram_channel_and_uses_etag(tmp_path: Path) -> None:
    app = create_app(settings=_settings(tmp_path))
    with TestClient(app, base_url="https://testserver") as client:
        headers = _login(client)
        binding = client.get("/api/v1/ai-agent/telegram")
        assert binding.status_code == 200
        assert binding.json()["enabled"] is False
        assert binding.headers["etag"] == '"1"'

        telegram = client.post(
            "/api/v1/notification-channels",
            headers=headers,
            json={
                "name": "AI Telegram",
                "type": "TELEGRAM",
                "telegram": {"bot_token": "123456:synthetic-ai", "chat_id": "123"},
            },
        )
        assert telegram.status_code == 201
        telegram_id = telegram.json()["id"]

        saved = client.put(
            "/api/v1/ai-agent/telegram",
            headers={**headers, "If-Match": binding.headers["etag"]},
            json={
                "notification_channel_id": telegram_id,
                "enabled": False,
                "allowed_chat_ids": ["123", "-100456"],
                "allowed_user_ids": ["88"],
                "idle_timeout_minutes": 60,
                "max_context_messages": 12,
            },
        )
        assert saved.status_code == 200
        assert saved.json()["notification_channel_id"] == telegram_id
        assert saved.json()["allowed_chat_ids"] == ["123", "-100456"]
        assert "bot_token" not in saved.text

        serverchan = client.post(
            "/api/v1/notification-channels",
            headers=headers,
            json={
                "name": "单向通知",
                "type": "SERVERCHAN",
                "serverchan": {"send_key": "SCTsynthetic-ai"},
            },
        )
        assert serverchan.status_code == 201
        rejected = client.put(
            "/api/v1/ai-agent/telegram",
            headers={**headers, "If-Match": saved.headers["etag"]},
            json={
                "notification_channel_id": serverchan.json()["id"],
                "enabled": False,
                "allowed_chat_ids": ["123"],
                "allowed_user_ids": [],
                "idle_timeout_minutes": 60,
                "max_context_messages": 12,
            },
        )
        assert rejected.status_code == 422
        assert rejected.json()["code"] == "AI_TELEGRAM_CHANNEL_INVALID"


def test_ai_telegram_enable_requires_enabled_tested_ai_agent(tmp_path: Path) -> None:
    app = create_app(settings=_settings(tmp_path))
    with TestClient(app, base_url="https://testserver") as client:
        headers = _login(client)
        telegram = client.post(
            "/api/v1/notification-channels",
            headers=headers,
            json={
                "name": "AI Telegram",
                "type": "TELEGRAM",
                "telegram": {"bot_token": "123456:synthetic-ai", "chat_id": "123"},
            },
        )
        binding = client.get("/api/v1/ai-agent/telegram")
        blocked = client.put(
            "/api/v1/ai-agent/telegram",
            headers={**headers, "If-Match": binding.headers["etag"]},
            json={
                "notification_channel_id": telegram.json()["id"],
                "enabled": True,
                "allowed_chat_ids": ["123"],
                "allowed_user_ids": [],
                "idle_timeout_minutes": 60,
                "max_context_messages": 12,
            },
        )
        assert blocked.status_code == 409
        assert blocked.json()["code"] == "AI_AGENT_UNAVAILABLE"


def test_telegram_approval_can_enable_without_ai_and_requires_channel_chat_allowlist(
    tmp_path: Path,
) -> None:
    app = create_app(settings=_settings(tmp_path))
    with TestClient(app, base_url="https://testserver") as client:
        headers = _login(client)
        telegram = client.post(
            "/api/v1/notification-channels",
            headers=headers,
            json={
                "name": "Approval Telegram",
                "type": "TELEGRAM",
                "telegram": {"bot_token": "123456:synthetic-approval", "chat_id": "123"},
            },
        )
        assert telegram.status_code == 201
        binding = client.get("/api/v1/ai-agent/telegram")

        saved = client.put(
            "/api/v1/ai-agent/telegram",
            headers={**headers, "If-Match": binding.headers["etag"]},
            json={
                "notification_channel_id": telegram.json()["id"],
                "enabled": False,
                "approval_enabled": True,
                "allowed_chat_ids": ["123"],
                "allowed_user_ids": ["88"],
                "idle_timeout_minutes": 60,
                "max_context_messages": 20,
            },
        )
        assert saved.status_code == 200
        assert saved.json()["enabled"] is False
        assert saved.json()["approval_enabled"] is True
        status = client.get("/api/v1/ai-agent/status")
        assert status.status_code == 200
        assert status.json()["telegram_enabled"] is False
        assert status.json()["telegram_approval_enabled"] is True

        rejected = client.put(
            "/api/v1/ai-agent/telegram",
            headers={**headers, "If-Match": saved.headers["etag"]},
            json={
                "notification_channel_id": telegram.json()["id"],
                "enabled": False,
                "approval_enabled": True,
                "allowed_chat_ids": ["999"],
                "allowed_user_ids": ["88"],
                "idle_timeout_minutes": 60,
                "max_context_messages": 20,
            },
        )
        assert rejected.status_code == 422
        assert rejected.json()["code"] == "AI_TELEGRAM_CONFIG_INVALID"
        assert "Chat ID" in rejected.json()["detail"]
