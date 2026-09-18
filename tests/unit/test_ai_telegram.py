from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from backend.app.application.ai_agent import AIAgentRuntimeConfig, AIAgentService
from backend.app.application.ai_runtime import AIHistoryMessage, AIReadOnlyAgent, AIReadOnlyAnswer
from backend.app.application.ai_telegram import AITelegramBindingView, AITelegramService
from backend.app.application.notifications import NotificationService
from backend.app.application.secrets import SecretStore
from backend.app.config import AppSettings
from backend.app.infrastructure.adapters.ai_provider import OpenAICompatibleProvider
from backend.app.infrastructure.adapters.telegram_ai import TelegramInboundUpdate
from backend.app.infrastructure.runtime import RuntimeManager


class StubAIAgentService(AIAgentService):
    def __init__(self) -> None:
        pass

    def runtime_config(self) -> AIAgentRuntimeConfig:
        return AIAgentRuntimeConfig(
            provider=cast(OpenAICompatibleProvider, object()),
            data_scopes=(),
            max_context_messages=20,
        )


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
        return AIReadOnlyAnswer(
            content="只读回复",
            tool_summary=("get_system_health",),
            context_truncated=False,
        )


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
async def test_telegram_service_rejects_unauthorized_before_agent(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    try:
        secret_store = SecretStore(runtime.session_factory, runtime.secret_cipher)
        agent = StubReadOnlyAgent()
        service = AITelegramService(
            runtime.session_factory,
            notification_service=NotificationService(runtime.session_factory, secret_store),
            ai_agent_service=StubAIAgentService(),
            agent=agent,
        )
        service.ensure_default()
        binding = AITelegramBindingView(
            id="telegram",
            notification_channel_id=None,
            enabled=True,
            approval_enabled=False,
            allowed_chat_ids=("123",),
            allowed_user_ids=(),
            idle_timeout_minutes=60,
            max_context_messages=20,
            last_update_id=0,
            version=1,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        result = await service.process_update(
            binding,
            TelegramInboundUpdate(1, "1", "999", "88", "系统状态？"),
        )
        assert result.authorized is False
        assert result.reply_text is None
        assert agent.calls == []
    finally:
        runtime.stop()


@pytest.mark.asyncio
async def test_telegram_service_stores_sanitized_bounded_turn(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    try:
        secret_store = SecretStore(runtime.session_factory, runtime.secret_cipher)
        agent = StubReadOnlyAgent()
        service = AITelegramService(
            runtime.session_factory,
            notification_service=NotificationService(runtime.session_factory, secret_store),
            ai_agent_service=StubAIAgentService(),
            agent=agent,
        )
        binding = service.ensure_default()
        binding = replace(
            binding,
            enabled=True,
            allowed_chat_ids=("123",),
            max_context_messages=2,
        )
        result = await service.process_update(
            binding,
            TelegramInboundUpdate(2, "9", "123", "88", "password=secret-canary 系统状态？"),
        )
        assert result.authorized is True
        assert result.reply_text == "只读回复"
        assert agent.calls == ["password=[REDACTED] 系统状态？"]
    finally:
        runtime.stop()
