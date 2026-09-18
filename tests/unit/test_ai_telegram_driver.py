from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from backend.app.application.ai_agent import AIAgentService
from backend.app.application.ai_runtime import AIHistoryMessage, AIReadOnlyAgent, AIReadOnlyAnswer
from backend.app.application.ai_telegram import AITelegramService
from backend.app.application.ai_telegram_driver import AITelegramDriver
from backend.app.application.notifications import NotificationChannelCreate, NotificationService
from backend.app.application.secrets import SecretStore
from backend.app.application.task_telegram_approvals import (
    TelegramApprovalCallbackResult,
    TelegramApprovalRequest,
)
from backend.app.config import AppSettings
from backend.app.domain.notification import NotificationChannelKind, TelegramCredential
from backend.app.infrastructure.adapters.telegram_ai import (
    TelegramAIClient,
    TelegramInboundUpdate,
    TelegramSentMessage,
)
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

    async def send_message(
        self,
        *,
        chat_id: str,
        text: str,
        reply_markup: dict[str, object] | None = None,
    ) -> TelegramSentMessage:
        self.sent.append((chat_id, text))
        return TelegramSentMessage(chat_id=chat_id, message_id="900")

    async def answer_callback_query(
        self,
        *,
        callback_query_id: str,
        text: str,
        show_alert: bool = False,
    ) -> None:
        self.sent.append(("callback:" + callback_query_id, text))

    async def clear_inline_keyboard(self, *, chat_id: str, message_id: str) -> None:
        self.sent.append((chat_id, "clear:" + message_id))


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
                approval_enabled=False,
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


class FakeApprovalService:
    def __init__(self) -> None:
        self.notified = False
        self.callbacks: list[str] = []

    def list_pending_requests(self, *, limit: int = 20) -> tuple[TelegramApprovalRequest, ...]:
        if self.notified:
            return ()
        return (
            TelegramApprovalRequest(
                approval_id="11111111-1111-1111-1111-111111111111",
                execution_id="execution-1",
                item_id="item-1",
                task_name="高风险任务",
                site_name="M-Team",
                execution_plan_id="plan-1",
                plan_digest="a" * 64,
                risk_level="HIGH",
                reason_codes=("HIGH_RISK_ACTION:DELETE_SOURCE",),
                action_kinds=("DELETE_SOURCE",),
                hardlink_count=0,
                client_fetch_count=0,
                create_directory_count=0,
                estimated_download_bytes_upper_bound=0,
                message_text="审批请求",
                reply_markup={"inline_keyboard": []},
            ),
        )

    def mark_notified(
        self,
        approval_id: str,
        *,
        chat_id: str,
        message_id: str | None,
    ) -> bool:
        assert approval_id == "11111111-1111-1111-1111-111111111111"
        assert chat_id == "123"
        assert message_id == "900"
        self.notified = True
        return True

    async def handle_callback(self, **kwargs: Any) -> TelegramApprovalCallbackResult:
        self.callbacks.append(cast(str, kwargs["callback_data"]))
        return TelegramApprovalCallbackResult("已批准本次执行", remove_keyboard=True)


@pytest.mark.asyncio
async def test_driver_processes_approval_when_ai_is_disabled(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    try:
        secret_store = SecretStore(runtime.session_factory, runtime.secret_cipher)
        notification_service = NotificationService(runtime.session_factory, secret_store)
        channel = notification_service.create(
            NotificationChannelCreate(
                name="Approval Telegram",
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
                enabled=False,
                approval_enabled=True,
                allowed_chat_ids=["123"],
                allowed_user_ids=["8"],
                idle_timeout_minutes=60,
                max_context_messages=10,
            )
            session.commit()

        callback_data = "pb1:A:11111111-1111-1111-1111-111111111111:aaaaaaaa"
        fake_client = FakeTelegramClient(
            (
                TelegramInboundUpdate(
                    1,
                    "900",
                    "123",
                    "8",
                    None,
                    callback_query_id="callback-1",
                    callback_data=callback_data,
                ),
                TelegramInboundUpdate(2, "2", "123", "8", "AI 不应处理这条消息"),
            )
        )
        approval_service = FakeApprovalService()
        driver = AITelegramDriver(
            service,
            notification_service,
            interval_seconds=1,
            poll_timeout_seconds=5,
            client_factory=FakeTelegramFactory(fake_client),
            approval_service=cast(Any, approval_service),
        )

        report = await driver.run_once()

        assert report is not None
        assert report.approval_requested_count == 1
        assert report.approval_processed_count == 1
        assert report.authorized_count == 1
        assert approval_service.callbacks == [callback_data]
        assert agent.calls == []
        assert ("123", "审批请求") in fake_client.sent
        assert ("callback:callback-1", "已批准本次执行") in fake_client.sent
        assert ("123", "clear:900") in fake_client.sent
        assert service.get().last_update_id == 2
    finally:
        runtime.stop()
