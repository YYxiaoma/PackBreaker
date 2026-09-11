from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from backend.app.domain.notification import (
    NotificationDeliveryState,
    notification_message_for_task_event,
)
from backend.app.infrastructure.persistence.models import (
    NotificationChannel,
    NotificationOutbox,
    TaskEvent,
    new_uuid,
    utc_now,
)


class NotificationChannelRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def list_all(self) -> list[NotificationChannel]:
        return list(
            self._session.scalars(
                select(NotificationChannel).order_by(
                    NotificationChannel.name, NotificationChannel.id
                )
            )
        )

    def list_enabled(self) -> list[NotificationChannel]:
        return list(
            self._session.scalars(
                select(NotificationChannel)
                .where(NotificationChannel.enabled.is_(True))
                .order_by(NotificationChannel.id)
            )
        )

    def get(self, channel_id: str) -> NotificationChannel | None:
        return self._session.get(NotificationChannel, channel_id)

    def create(
        self,
        *,
        name: str,
        kind: str,
        secret_id: str | None,
        task_link_base_url: str | None,
        aggregation_window_seconds: int,
    ) -> NotificationChannel:
        now = utc_now()
        record = NotificationChannel(
            id=new_uuid(),
            name=name,
            type=kind,
            secret_id=secret_id,
            task_link_base_url=task_link_base_url,
            aggregation_window_seconds=aggregation_window_seconds,
            connection_status="UNTESTED",
            enabled=False,
            version=1,
            last_test_at=None,
            created_at=now,
            updated_at=now,
        )
        self._session.add(record)
        self._session.flush()
        return record

    def update_config(
        self,
        channel_id: str,
        *,
        expected_version: int,
        values: dict[str, Any],
    ) -> bool:
        payload = dict(values)
        payload["version"] = expected_version + 1
        payload["updated_at"] = utc_now()
        updated = self._session.scalar(
            update(NotificationChannel)
            .where(
                NotificationChannel.id == channel_id,
                NotificationChannel.version == expected_version,
            )
            .values(**payload)
            .returning(NotificationChannel.id)
        )
        return updated is not None

    def update_probe(
        self,
        channel_id: str,
        *,
        expected_version: int,
        status: str,
        tested_at: datetime,
    ) -> bool:
        updated = self._session.scalar(
            update(NotificationChannel)
            .where(
                NotificationChannel.id == channel_id,
                NotificationChannel.version == expected_version,
            )
            .values(
                connection_status=status,
                last_test_at=tested_at,
                updated_at=utc_now(),
            )
            .returning(NotificationChannel.id)
        )
        return updated is not None

    def delete(self, channel_id: str, *, expected_version: int) -> NotificationChannel | None:
        record = self.get(channel_id)
        if record is None or record.version != expected_version:
            return None
        self._session.delete(record)
        self._session.flush()
        return record


class NotificationOutboxRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def project_event(self, event: TaskEvent) -> int:
        channels = NotificationChannelRepository(self._session).list_enabled()
        projected = 0
        now = event.created_at
        for channel in channels:
            link = _task_link(channel.task_link_base_url, event.task_id)
            message = notification_message_for_task_event(
                task_id=event.task_id,
                event_type=event.event_type,
                to_status=event.to_status,
                link=link,
            )
            if message is None:
                continue
            existing = self._session.scalar(
                select(NotificationOutbox).where(
                    NotificationOutbox.channel_id == channel.id,
                    NotificationOutbox.task_id == event.task_id,
                    NotificationOutbox.event_key == message.event_key,
                )
            )
            if existing is None:
                self._session.add(
                    NotificationOutbox(
                        id=new_uuid(),
                        channel_id=channel.id,
                        task_id=event.task_id,
                        last_event_id=event.id,
                        channel_version=channel.version,
                        event_key=message.event_key,
                        title=message.title,
                        body=message.body,
                        severity=message.severity.value,
                        link=message.link,
                        state=NotificationDeliveryState.PENDING.value,
                        pending_count=1,
                        attempt_count=0,
                        next_attempt_at=now,
                        last_sent_at=None,
                        delivered_at=None,
                        last_error_code=None,
                        created_at=now,
                        updated_at=now,
                    )
                )
                projected += 1
                continue

            existing.last_event_id = event.id
            existing.channel_version = channel.version
            existing.title = message.title
            existing.body = message.body
            existing.severity = message.severity.value
            existing.link = message.link
            existing.updated_at = now
            if existing.state == NotificationDeliveryState.DEAD.value:
                existing.state = NotificationDeliveryState.PENDING.value
                existing.pending_count = 1
                existing.attempt_count = 0
                existing.next_attempt_at = now
                existing.last_error_code = None
            else:
                existing.pending_count += 1
                if existing.state == NotificationDeliveryState.DELIVERED.value:
                    last_sent = existing.last_sent_at or now
                    due_at = last_sent + timedelta(seconds=channel.aggregation_window_seconds)
                    existing.next_attempt_at = max(now, due_at)
            projected += 1
        self._session.flush()
        return projected

    def list_due(self, *, now: datetime, limit: int) -> list[NotificationOutbox]:
        if limit <= 0:
            raise ValueError("通知投递 limit 必须大于 0")
        return list(
            self._session.scalars(
                select(NotificationOutbox)
                .where(
                    NotificationOutbox.pending_count > 0,
                    NotificationOutbox.next_attempt_at.is_not(None),
                    NotificationOutbox.next_attempt_at <= now,
                    or_(
                        NotificationOutbox.state == NotificationDeliveryState.PENDING.value,
                        NotificationOutbox.state == NotificationDeliveryState.RETRY.value,
                        NotificationOutbox.state == NotificationDeliveryState.DELIVERED.value,
                    ),
                )
                .order_by(NotificationOutbox.next_attempt_at, NotificationOutbox.id)
                .limit(limit)
            )
        )

    def get(self, outbox_id: str) -> NotificationOutbox | None:
        return self._session.get(NotificationOutbox, outbox_id)

    def mark_delivered(
        self,
        outbox_id: str,
        *,
        delivered_at: datetime,
        sent_count: int,
        aggregation_window_seconds: int,
        channel_version: int,
    ) -> None:
        record = self.get(outbox_id)
        if record is None:
            return
        record.state = NotificationDeliveryState.DELIVERED.value
        remaining = max(0, record.pending_count - sent_count)
        record.pending_count = remaining
        record.attempt_count = 0
        record.next_attempt_at = (
            delivered_at + timedelta(seconds=aggregation_window_seconds) if remaining else None
        )
        record.last_sent_at = delivered_at
        record.delivered_at = delivered_at
        record.channel_version = channel_version
        record.last_error_code = None
        record.updated_at = delivered_at
        self._session.flush()

    def mark_failure(
        self,
        outbox_id: str,
        *,
        error_code: str,
        retryable: bool,
        max_attempts: int,
        retry_at: datetime,
        occurred_at: datetime,
        channel_version: int | None = None,
    ) -> None:
        record = self.get(outbox_id)
        if record is None:
            return
        attempts = record.attempt_count + 1
        record.attempt_count = attempts
        record.last_error_code = error_code[:64]
        record.updated_at = occurred_at
        if channel_version is not None:
            record.channel_version = channel_version
        if retryable and attempts < max_attempts:
            record.state = NotificationDeliveryState.RETRY.value
            record.next_attempt_at = retry_at
        else:
            record.state = NotificationDeliveryState.DEAD.value
            record.next_attempt_at = None
        self._session.flush()


def persist_task_event(session: Session, event: TaskEvent) -> TaskEvent:
    """在同一事务中持久化 TaskEvent，并为已启用通知渠道投影脱敏 outbox。"""

    session.add(event)
    session.flush()
    NotificationOutboxRepository(session).project_event(event)
    return event


def _task_link(base_url: str | None, task_id: str) -> str | None:
    if base_url is None:
        return None
    return f"{base_url.rstrip('/')}/?task_id={task_id}"
