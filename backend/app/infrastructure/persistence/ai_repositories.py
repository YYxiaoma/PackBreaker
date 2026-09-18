from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from backend.app.infrastructure.persistence.models import (
    AIAgentSetting,
    AIChannelBinding,
    AIConversation,
    AIMessage,
    new_uuid,
    utc_now,
)


class AIAgentSettingRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self) -> AIAgentSetting | None:
        return self._session.get(AIAgentSetting, "default")

    def create_default(self) -> AIAgentSetting:
        existing = self.get()
        if existing is not None:
            return existing
        now = utc_now()
        record = AIAgentSetting(
            id="default",
            enabled=False,
            provider_kind="OPENAI",
            base_url="https://api.openai.com/v1",
            api_key_secret_id=None,
            model="",
            request_timeout_seconds=30,
            max_context_messages=20,
            data_scopes=[
                "SYSTEM_HEALTH",
                "TASK_EXECUTIONS",
                "REDACTED_LOGS",
                "SITE_STATUS",
                "DOWNLOADER_STATUS",
                "VERSION_STATUS",
                "HELP_DOCS",
            ],
            connection_status="UNTESTED",
            last_test_at=None,
            version=1,
            created_at=now,
            updated_at=now,
        )
        self._session.add(record)
        self._session.flush()
        return record

    def update_probe_status(
        self,
        *,
        expected_version: int,
        status: str,
        last_test_at: datetime,
    ) -> bool:
        updated = self._session.scalar(
            update(AIAgentSetting)
            .where(
                AIAgentSetting.id == "default",
                AIAgentSetting.version == expected_version,
            )
            .values(
                connection_status=status,
                last_test_at=last_test_at,
                version=AIAgentSetting.version + 1,
                updated_at=utc_now(),
            )
            .returning(AIAgentSetting.id)
        )
        return updated is not None

    def lock_current(self) -> AIAgentSetting | None:
        return self._session.scalar(
            select(AIAgentSetting).where(AIAgentSetting.id == "default").with_for_update()
        )


class AIChannelBindingRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self) -> AIChannelBinding | None:
        return self._session.get(AIChannelBinding, "telegram")

    def create_default(self) -> AIChannelBinding:
        existing = self.get()
        if existing is not None:
            return existing
        now = utc_now()
        record = AIChannelBinding(
            id="telegram",
            kind="TELEGRAM",
            notification_channel_id=None,
            enabled=False,
            allowed_chat_ids=[],
            allowed_user_ids=[],
            idle_timeout_minutes=60,
            max_context_messages=20,
            last_update_id=0,
            version=1,
            created_at=now,
            updated_at=now,
        )
        self._session.add(record)
        self._session.flush()
        return record

    def update_config(
        self,
        *,
        expected_version: int,
        notification_channel_id: str | None,
        enabled: bool,
        allowed_chat_ids: list[str],
        allowed_user_ids: list[str],
        idle_timeout_minutes: int,
        max_context_messages: int,
    ) -> bool:
        updated = self._session.scalar(
            update(AIChannelBinding)
            .where(
                AIChannelBinding.id == "telegram",
                AIChannelBinding.version == expected_version,
            )
            .values(
                notification_channel_id=notification_channel_id,
                enabled=enabled,
                allowed_chat_ids=allowed_chat_ids,
                allowed_user_ids=allowed_user_ids,
                idle_timeout_minutes=idle_timeout_minutes,
                max_context_messages=max_context_messages,
                version=AIChannelBinding.version + 1,
                updated_at=utc_now(),
            )
            .returning(AIChannelBinding.id)
        )
        return updated is not None

    def advance_cursor(self, update_id: int) -> bool:
        updated = self._session.scalar(
            update(AIChannelBinding)
            .where(
                AIChannelBinding.id == "telegram",
                AIChannelBinding.last_update_id < update_id,
            )
            .values(last_update_id=update_id, updated_at=utc_now())
            .returning(AIChannelBinding.id)
        )
        return updated is not None


class AIConversationRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def find_recent(
        self,
        *,
        binding_id: str,
        chat_id: str,
        user_id: str | None,
        idle_timeout_minutes: int,
        now: datetime,
    ) -> AIConversation | None:
        cutoff = now - timedelta(minutes=idle_timeout_minutes)
        statement = (
            select(AIConversation)
            .where(
                AIConversation.binding_id == binding_id,
                AIConversation.chat_id == chat_id,
                AIConversation.last_message_at >= cutoff,
            )
            .order_by(AIConversation.last_message_at.desc(), AIConversation.id.desc())
            .limit(1)
        )
        if user_id is None:
            statement = statement.where(AIConversation.user_id.is_(None))
        else:
            statement = statement.where(AIConversation.user_id == user_id)
        return self._session.scalar(statement)

    def create(
        self,
        *,
        binding_id: str,
        chat_id: str,
        user_id: str | None,
        now: datetime,
    ) -> AIConversation:
        record = AIConversation(
            id=new_uuid(),
            binding_id=binding_id,
            chat_id=chat_id,
            user_id=user_id,
            started_at=now,
            last_message_at=now,
            context_truncated=False,
            created_at=now,
            updated_at=now,
        )
        self._session.add(record)
        self._session.flush()
        return record

    def touch(self, conversation_id: str, *, at: datetime, truncated: bool) -> None:
        self._session.execute(
            update(AIConversation)
            .where(AIConversation.id == conversation_id)
            .values(
                last_message_at=at,
                context_truncated=(AIConversation.context_truncated | truncated),
                updated_at=at,
            )
        )


class AIMessageRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def list_recent(self, conversation_id: str, *, limit: int) -> list[AIMessage]:
        records = list(
            self._session.scalars(
                select(AIMessage)
                .where(AIMessage.conversation_id == conversation_id)
                .order_by(AIMessage.created_at.desc(), AIMessage.id.desc())
                .limit(limit)
            )
        )
        records.reverse()
        return records

    def append(
        self,
        *,
        conversation_id: str,
        role: str,
        content: str,
        telegram_message_id: str | None,
        tool_summary: list[str],
        created_at: datetime,
    ) -> AIMessage:
        record = AIMessage(
            id=new_uuid(),
            conversation_id=conversation_id,
            role=role,
            content=content,
            telegram_message_id=telegram_message_id,
            tool_summary=tool_summary,
            created_at=created_at,
        )
        self._session.add(record)
        self._session.flush()
        return record

    def prune(self, conversation_id: str, *, keep: int) -> int:
        keep_ids = list(
            self._session.scalars(
                select(AIMessage.id)
                .where(AIMessage.conversation_id == conversation_id)
                .order_by(AIMessage.created_at.desc(), AIMessage.id.desc())
                .limit(keep)
            )
        )
        if not keep_ids:
            return 0
        deleted_ids = list(
            self._session.scalars(
                delete(AIMessage)
                .where(
                    AIMessage.conversation_id == conversation_id,
                    AIMessage.id.not_in(keep_ids),
                )
                .returning(AIMessage.id)
            )
        )
        return len(deleted_ids)
