from __future__ import annotations

from pathlib import Path

import pytest

from backend.app.application.ai_agent import AIAgentService
from backend.app.application.ai_runtime import AIHistoryMessage, AIReadOnlyAgent, AIReadOnlyAnswer
from backend.app.application.ai_telegram import AITelegramService
from backend.app.application.ai_telegram_driver import AITelegramDriver
from backend.app.application.notifications import NotificationChannelCreate, NotificationService
from backend.app.application.secrets import SecretStore
from backend.app.config import AppSettings
from backend.app.domain.notification import NotificationChannelKind, TelegramCredential
from backend.app.infrastructure.adapters.telegram_ai import TelegramAIClient, TelegramInboundUpdate
from backend.app.infrastructure.persistence.ai_repositories import AIChannelBindingRepository
from backend.app.infrastructure.runtime import RuntimeManager


class StubReadOnlyAgent(AIReadOnlyAgent):
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def respond(
        self,
        *,
        history: tuple[AIHistoryMessage, ...],
        user_message: str,
    ) -> AIReadOnlyAnswer:
        self.calls.append(user_message)
        return AIReadOnlyAnswer("driver reply", ("get_system_health",), False)


class FakeTelegramClient(TelegramAIClient):
    def __init__(self, updates: tuple[TelegramInboundUpdate, ...]) -> None:
        self.updates = updates
        self.offsets: list[int] = []
        self.sent: list[tuple[str, str]] = []

    async def poll(
        self,
        *,
        offset: int,
        timeout_seconds: int = 20,
        limit: int = 20,
    ) -> tuple[TelegramInboundUpdate, ...]:
        self.offsets.append(offset)
        return tuple(item for item in self.updates if item.update_id >= offset)[:limit]

    async def send_message(self, *, chat_id: str, text: str) -> None:
        self.sent.append((chat_id, text))


class FakeTelegramFactory:
    def __init__(self, client: FakeTelegramClient) -> None:
        self.client = client

    def create(self, *, bot_token: str, proxy_url: str | None) -> TelegramAIClient:
        assert bot_token == "123456:driver-synthetic"
        assert proxy_url is None
        return self.client


def _runtime(tmp_path: Path) -> RuntimeManager:
    runtime = RuntimeManager(
        AppSettings(
            config_dir=(tmp_path / "config").resolve(),
            data_dir=(tmp_path / "data").resolve(),
        )
    )
    runtime.start()
    return runtime


@pytest.mark.asyncio
async def test_driver_filters_allowlist_replies_and_persists_cursor(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    try:
        secret_store = SecretStore(runtime.session_factory, runtime.secret_cipher)
        notification_service = NotificationService(runtime.session_factory, secret_store)
        channel = notification_service.create(
            NotificationChannelCreate(
                name="AI Telegram",
                kind=NotificationChannelKind.TELEGRAM,
                credential=TelegramCredential("123456:driver-synthetic", "123"),
            )
        )
        agent = StubReadOnlyAgent()
        service = AITelegramService(
            runtime.session_factory,
            notification_service=notification_service,
            ai_agent_service=AIAgentService(runtime.session_factory, secret_store),
            agent=agent,
        )
        service.ensure_default()
        with runtime.session_factory() as session:
            assert AIChannelBindingRepository(session).update_config(
                expected_version=1,
                notification_channel_id=channel.id,
                enabled=True,
                allowed_chat_ids=["123"],
                allowed_user_ids=[],
                idle_timeout_minutes=60,
                max_context_messages=10,
            )
            session.commit()

        fake_client = FakeTelegramClient(
            (
                TelegramInboundUpdate(1, "1", "999", "8", "未授权"),
                TelegramInboundUpdate(2, "2", "123", "8", "系统状态？"),
            )
        )
        driver = AITelegramDriver(
            service,
            notification_service,
            interval_seconds=1,
            poll_timeout_seconds=5,
            client_factory=FakeTelegramFactory(fake_client),
        )
        first = await driver.run_once()
        second = await driver.run_once()

        assert first is not None
        assert first.scanned_count == 2
        assert first.authorized_count == 1
        assert first.replied_count == 1
        assert first.last_update_id == 2
        assert fake_client.sent == [("123", "driver reply")]
        assert agent.calls == ["系统状态？"]
        assert fake_client.offsets == [1, 3]
        assert second is not None and second.scanned_count == 0
        assert service.get().last_update_id == 2
    finally:
        runtime.stop()
