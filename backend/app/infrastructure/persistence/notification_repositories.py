from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from backend.app.domain.notification import (
    DEFAULT_NOTIFICATION_AGGREGATION_SECONDS,
    NotificationDeliveryState,
    NotificationEventType,
    NotificationMessage,
    notification_message_for_site_reliability_event,
    notification_message_for_task_event,
)
from backend.app.infrastructure.persistence.models import (
    AdminNotification,
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
        event_types: list[str],
        proxy_enabled: bool,
        proxy_host: str | None,
        proxy_port: int | None,
        proxy_username: str | None,
        proxy_secret_id: str | None,
    ) -> NotificationChannel:
        now = utc_now()
        record = NotificationChannel(
            id=new_uuid(),
            name=name,
            type=kind,
            secret_id=secret_id,
            task_link_base_url=None,
            aggregation_window_seconds=DEFAULT_NOTIFICATION_AGGREGATION_SECONDS,
            event_types=event_types,
            proxy_enabled=proxy_enabled,
            proxy_host=proxy_host,
            proxy_port=proxy_port,
            proxy_username=proxy_username,
            proxy_secret_id=proxy_secret_id,
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


class AdminNotificationRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, notification_id: str) -> AdminNotification | None:
        return self._session.get(AdminNotification, notification_id)

    def list_recent(
        self, *, unread_only: bool = False, limit: int = 100
    ) -> list[AdminNotification]:
        if not 1 <= limit <= 500:
            raise ValueError("站内通知 limit 必须在 1～500 之间")
        statement = select(AdminNotification)
        if unread_only:
            statement = statement.where(AdminNotification.read_at.is_(None))
        return list(
            self._session.scalars(
                statement.order_by(
                    AdminNotification.created_at.desc(), AdminNotification.id.desc()
                ).limit(limit)
            )
        )

    def unread_count(self) -> int:
        return int(
            self._session.scalar(
                select(func.count())
                .select_from(AdminNotification)
                .where(AdminNotification.read_at.is_(None))
            )
            or 0
        )

    def create(
        self,
        *,
        event_type: str,
        title: str,
        message: str,
        severity: str,
        dedup_key: str | None = None,
    ) -> AdminNotification:
        if dedup_key is not None:
            existing = self._session.scalar(
                select(AdminNotification).where(AdminNotification.dedup_key == dedup_key)
            )
            if existing is not None:
                return existing
        record = AdminNotification(
            id=new_uuid(),
            event_type=event_type,
            title=title,
            message=message,
            severity=severity,
            dedup_key=dedup_key,
            read_at=None,
            created_at=utc_now(),
        )
        self._session.add(record)
        self._session.flush()
        return record

    def mark_read(self, notification_id: str, *, read_at: datetime | None = None) -> bool:
        updated = self._session.scalar(
            update(AdminNotification)
            .where(AdminNotification.id == notification_id, AdminNotification.read_at.is_(None))
            .values(read_at=read_at or utc_now())
            .returning(AdminNotification.id)
        )
        return updated is not None

    def mark_all_read(self, *, read_at: datetime | None = None) -> int:
        updated_ids = self._session.scalars(
            update(AdminNotification)
            .where(AdminNotification.read_at.is_(None))
            .values(read_at=read_at or utc_now())
            .returning(AdminNotification.id)
        ).all()
        return len(updated_ids)


class NotificationOutboxRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def project_event(self, event: TaskEvent) -> int:
        channels = NotificationChannelRepository(self._session).list_enabled()
        projected = 0
        for channel in channels:
            if not _accepts_event(channel, NotificationEventType.TASK_EXECUTION_RESULT):
                continue
            message = notification_message_for_task_event(
                task_id=event.task_id,
                event_type=event.event_type,
                to_status=event.to_status,
                link=None,
            )
            if message is None:
                continue
            self._upsert_message(
                channel=channel,
                subject_kind="TASK",
                subject_id=event.task_id,
                message=message,
                occurred_at=event.created_at,
                task_id=event.task_id,
                last_event_id=event.id,
            )
            projected += 1
        self._session.flush()
        return projected

    def project_site_reliability_event(
        self,
        *,
        site_id: str,
        event_type: str,
        error_code: str | None,
        occurred_at: datetime,
    ) -> int:
        message = notification_message_for_site_reliability_event(
            site_id=site_id,
            event_type=event_type,
            error_code=error_code,
        )
        if message is None:
            return 0
        channels = NotificationChannelRepository(self._session).list_enabled()
        for channel in channels:
            if not _accepts_event(channel, NotificationEventType.SITE_RELIABILITY):
                continue
            self._upsert_message(
                channel=channel,
                subject_kind="SITE",
                subject_id=site_id,
                message=message,
                occurred_at=occurred_at,
                task_id=None,
                last_event_id=None,
            )
        self._session.flush()
        return sum(
            1
            for channel in channels
            if _accepts_event(channel, NotificationEventType.SITE_RELIABILITY)
        )

    def _upsert_message(
        self,
        *,
        channel: NotificationChannel,
        subject_kind: str,
        subject_id: str,
        message: NotificationMessage,
        occurred_at: datetime,
        task_id: str | None,
        last_event_id: str | None,
    ) -> None:
        existing = self._session.scalar(
            select(NotificationOutbox).where(
                NotificationOutbox.channel_id == channel.id,
                NotificationOutbox.subject_kind == subject_kind,
                NotificationOutbox.subject_id == subject_id,
                NotificationOutbox.event_key == message.event_key,
            )
        )
        if existing is None:
            self._session.add(
                NotificationOutbox(
                    id=new_uuid(),
                    channel_id=channel.id,
                    subject_kind=subject_kind,
                    subject_id=subject_id,
                    task_id=task_id,
                    last_event_id=last_event_id,
                    channel_version=channel.version,
                    event_key=message.event_key,
                    title=message.title,
                    body=message.body,
                    severity=message.severity.value,
                    link=message.link,
                    state=NotificationDeliveryState.PENDING.value,
                    pending_count=1,
                    attempt_count=0,
                    next_attempt_at=occurred_at,
                    last_sent_at=None,
                    delivered_at=None,
                    last_error_code=None,
                    created_at=occurred_at,
                    updated_at=occurred_at,
                )
            )
            return

        existing.task_id = task_id
        existing.last_event_id = last_event_id
        existing.channel_version = channel.version
        existing.title = message.title
        existing.body = message.body
        existing.severity = message.severity.value
        existing.link = message.link
        existing.updated_at = occurred_at
        if existing.state == NotificationDeliveryState.DEAD.value:
            existing.state = NotificationDeliveryState.PENDING.value
            existing.pending_count = 1
            existing.attempt_count = 0
            existing.next_attempt_at = occurred_at
            existing.last_error_code = None
            return
        existing.pending_count += 1
        if existing.state == NotificationDeliveryState.DELIVERED.value:
            last_sent = existing.last_sent_at or occurred_at
            due_at = last_sent + timedelta(seconds=DEFAULT_NOTIFICATION_AGGREGATION_SECONDS)
            existing.next_attempt_at = max(occurred_at, due_at)

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


def _accepts_event(channel: NotificationChannel, event_type: NotificationEventType) -> bool:
    # 0025 之前创建的渠道会以空列表迁移。空列表保持旧版“全部事件”语义，避免升级后静默丢通知。
    return not channel.event_types or event_type.value in channel.event_types
