from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.ai_agent import AIAgentService
from backend.app.application.ai_runtime import AIHistoryMessage, AIReadOnlyAgent
from backend.app.application.errors import ApplicationError
from backend.app.application.notifications import NotificationService
from backend.app.infrastructure.adapters.telegram_ai import TelegramInboundUpdate
from backend.app.infrastructure.app_logging import sanitize_message
from backend.app.infrastructure.persistence.ai_repositories import (
    AIChannelBindingRepository,
    AIConversationRepository,
    AIMessageRepository,
)
from backend.app.infrastructure.persistence.models import AIChannelBinding

_CHAT_ID_RE = re.compile(r"^-?\d{1,20}$")
_USER_ID_RE = re.compile(r"^\d{1,20}$")
_MAX_TELEGRAM_MESSAGE_CHARS = 4096


@dataclass(frozen=True, slots=True)
class AITelegramBindingView:
    id: str
    notification_channel_id: str | None
    enabled: bool
    allowed_chat_ids: tuple[str, ...]
    allowed_user_ids: tuple[str, ...]
    idle_timeout_minutes: int
    max_context_messages: int
    last_update_id: int
    version: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class AITelegramBindingUpdate:
    notification_channel_id: str | None
    enabled: bool
    allowed_chat_ids: tuple[str, ...]
    allowed_user_ids: tuple[str, ...]
    idle_timeout_minutes: int
    max_context_messages: int


@dataclass(frozen=True, slots=True)
class AITelegramProcessResult:
    authorized: bool
    reply_text: str | None
    error_code: str | None = None
    conversation_id: str | None = None


class AITelegramService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        notification_service: NotificationService,
        ai_agent_service: AIAgentService,
        agent: AIReadOnlyAgent,
    ) -> None:
        self._session_factory = session_factory
        self._notification_service = notification_service
        self._ai_agent_service = ai_agent_service
        self._agent = agent

    def ensure_default(self) -> AITelegramBindingView:
        with self._session_factory() as session:
            record = AIChannelBindingRepository(session).create_default()
            session.commit()
            return self._view(record)

    def get(self) -> AITelegramBindingView:
        with self._session_factory() as session:
            repository = AIChannelBindingRepository(session)
            record = repository.get() or repository.create_default()
            session.commit()
            return self._view(record)

    def update(
        self,
        change: AITelegramBindingUpdate,
        *,
        expected_version: int,
    ) -> AITelegramBindingView:
        chat_ids = self._normalize_ids(change.allowed_chat_ids, chat=True)
        user_ids = self._normalize_ids(change.allowed_user_ids, chat=False)
        if not 5 <= change.idle_timeout_minutes <= 10080:
            raise self._invalid("会话空闲超时必须在 5～10080 分钟之间")
        if not 2 <= change.max_context_messages <= 100:
            raise self._invalid("Telegram 最大上下文消息数必须在 2～100 之间")
        channel_id = (
            change.notification_channel_id.strip()
            if change.notification_channel_id is not None
            else None
        )
        if channel_id == "":
            channel_id = None
        if channel_id is not None:
            self._notification_service.telegram_ai_runtime(channel_id)
        if change.enabled:
            if channel_id is None:
                raise self._invalid("启用 Telegram AI 前必须绑定 Telegram 通知渠道")
            if not chat_ids and not user_ids:
                raise self._invalid("启用 Telegram AI 前至少配置一个 Chat ID 或 User ID")
            self._ai_agent_service.runtime_config()

        with self._session_factory() as session:
            repository = AIChannelBindingRepository(session)
            repository.create_default()
            if not repository.update_config(
                expected_version=expected_version,
                notification_channel_id=channel_id,
                enabled=change.enabled,
                allowed_chat_ids=list(chat_ids),
                allowed_user_ids=list(user_ids),
                idle_timeout_minutes=change.idle_timeout_minutes,
                max_context_messages=change.max_context_messages,
            ):
                session.rollback()
                raise self._version_conflict()
            session.commit()
            refreshed = repository.get()
            if refreshed is None:
                raise self._invalid("Telegram AI 绑定保存失败")
            return self._view(refreshed)

    def advance_cursor(self, update_id: int) -> None:
        if update_id < 0:
            raise self._invalid("Telegram update_id 无效")
        with self._session_factory() as session:
            AIChannelBindingRepository(session).advance_cursor(update_id)
            session.commit()

    def is_authorized(
        self,
        binding: AITelegramBindingView,
        update: TelegramInboundUpdate,
    ) -> bool:
        if update.chat_id is None:
            return False
        if binding.allowed_chat_ids and update.chat_id not in binding.allowed_chat_ids:
            return False
        if binding.allowed_user_ids and (
            update.user_id is None or update.user_id not in binding.allowed_user_ids
        ):
            return False
        return bool(binding.allowed_chat_ids or binding.allowed_user_ids)

    async def process_update(
        self,
        binding: AITelegramBindingView,
        update: TelegramInboundUpdate,
    ) -> AITelegramProcessResult:
        if not self.is_authorized(binding, update):
            return AITelegramProcessResult(authorized=False, reply_text=None)
        if update.text is None or update.chat_id is None:
            return AITelegramProcessResult(authorized=True, reply_text=None)
        raw_text = update.text.strip()
        if not raw_text:
            return AITelegramProcessResult(authorized=True, reply_text=None)
        if len(raw_text) > _MAX_TELEGRAM_MESSAGE_CHARS:
            return AITelegramProcessResult(
                authorized=True,
                reply_text="消息过长，请将问题缩短到 4096 个字符以内。",
                error_code="AI_MESSAGE_TOO_LONG",
            )
        safe_text = sanitize_message(raw_text)
        now = datetime.now(UTC)
        conversation_id, history, history_truncated = self._begin_turn(
            binding=binding,
            update=update,
            safe_text=safe_text,
            now=now,
        )
        try:
            answer = await self._agent.respond(history=history, user_message=safe_text)
        except ApplicationError as exc:
            reply = "AI 助手暂时无法完成本次请求，请稍后重试。"
            self._finish_turn(
                conversation_id=conversation_id,
                reply=reply,
                tool_summary=(f"ERROR:{exc.code}",),
                truncated=history_truncated,
                keep_messages=binding.max_context_messages,
            )
            return AITelegramProcessResult(
                authorized=True,
                reply_text=reply,
                error_code=exc.code,
                conversation_id=conversation_id,
            )
        self._finish_turn(
            conversation_id=conversation_id,
            reply=answer.content,
            tool_summary=answer.tool_summary,
            truncated=history_truncated or answer.context_truncated,
            keep_messages=binding.max_context_messages,
        )
        return AITelegramProcessResult(
            authorized=True,
            reply_text=answer.content,
            conversation_id=conversation_id,
        )

    def _begin_turn(
        self,
        *,
        binding: AITelegramBindingView,
        update: TelegramInboundUpdate,
        safe_text: str,
        now: datetime,
    ) -> tuple[str, tuple[AIHistoryMessage, ...], bool]:
        assert update.chat_id is not None
        with self._session_factory() as session:
            conversations = AIConversationRepository(session)
            messages = AIMessageRepository(session)
            conversation = conversations.find_recent(
                binding_id=binding.id,
                chat_id=update.chat_id,
                user_id=update.user_id,
                idle_timeout_minutes=binding.idle_timeout_minutes,
                now=now,
            )
            if conversation is None:
                conversation = conversations.create(
                    binding_id=binding.id,
                    chat_id=update.chat_id,
                    user_id=update.user_id,
                    now=now,
                )
            recent = messages.list_recent(
                conversation.id,
                limit=binding.max_context_messages + 1,
            )
            truncated = len(recent) > binding.max_context_messages
            bounded = recent[-binding.max_context_messages :]
            history = tuple(
                AIHistoryMessage(role=item.role.casefold(), content=item.content)
                for item in bounded
            )
            messages.append(
                conversation_id=conversation.id,
                role="USER",
                content=safe_text,
                telegram_message_id=update.message_id,
                tool_summary=[],
                created_at=now,
            )
            conversations.touch(conversation.id, at=now, truncated=truncated)
            session.commit()
            return conversation.id, history, truncated

    def _finish_turn(
        self,
        *,
        conversation_id: str,
        reply: str,
        tool_summary: tuple[str, ...],
        truncated: bool,
        keep_messages: int,
    ) -> None:
        now = datetime.now(UTC)
        with self._session_factory() as session:
            messages = AIMessageRepository(session)
            conversations = AIConversationRepository(session)
            messages.append(
                conversation_id=conversation_id,
                role="ASSISTANT",
                content=reply[:8192],
                telegram_message_id=None,
                tool_summary=list(tool_summary),
                created_at=now,
            )
            pruned = messages.prune(conversation_id, keep=max(keep_messages, 2))
            conversations.touch(
                conversation_id,
                at=now,
                truncated=truncated or pruned > 0,
            )
            session.commit()

    @staticmethod
    def _normalize_ids(values: tuple[str, ...], *, chat: bool) -> tuple[str, ...]:
        pattern = _CHAT_ID_RE if chat else _USER_ID_RE
        normalized: list[str] = []
        for value in values:
            item = value.strip()
            if not pattern.fullmatch(item):
                raise AITelegramService._invalid(
                    "Chat ID 必须是整数" if chat else "User ID 必须是正整数"
                )
            if item not in normalized:
                normalized.append(item)
        if len(normalized) > 100:
            raise AITelegramService._invalid("Telegram allowlist 最多包含 100 个条目")
        return tuple(normalized)

    @staticmethod
    def _view(record: AIChannelBinding) -> AITelegramBindingView:
        return AITelegramBindingView(
            id=record.id,
            notification_channel_id=record.notification_channel_id,
            enabled=record.enabled,
            allowed_chat_ids=tuple(str(value) for value in record.allowed_chat_ids),
            allowed_user_ids=tuple(str(value) for value in record.allowed_user_ids),
            idle_timeout_minutes=record.idle_timeout_minutes,
            max_context_messages=record.max_context_messages,
            last_update_id=int(record.last_update_id),
            version=record.version,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )

    @staticmethod
    def _invalid(detail: str) -> ApplicationError:
        return ApplicationError(
            code="AI_TELEGRAM_CONFIG_INVALID",
            status=422,
            title="Telegram AI 配置无效",
            detail=detail,
        )

    @staticmethod
    def _version_conflict() -> ApplicationError:
        return ApplicationError(
            code="AI_TELEGRAM_VERSION_CONFLICT",
            status=412,
            title="Telegram AI 配置版本冲突",
            detail="Telegram AI 配置已经变化，请刷新后重试",
        )
