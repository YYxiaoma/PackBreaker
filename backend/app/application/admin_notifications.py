from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.errors import ApplicationError
from backend.app.infrastructure.persistence.models import AdminNotification
from backend.app.infrastructure.persistence.notification_repositories import (
    AdminNotificationRepository,
)


@dataclass(frozen=True, slots=True)
class AdminNotificationView:
    id: str
    event_type: str
    title: str
    message: str
    severity: str
    read_at: datetime | None
    created_at: datetime


class AdminNotificationService:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def list_recent(
        self,
        *,
        unread_only: bool = False,
        limit: int = 100,
    ) -> list[AdminNotificationView]:
        with self._session_factory() as session:
            return [
                self._view(record)
                for record in AdminNotificationRepository(session).list_recent(
                    unread_only=unread_only,
                    limit=limit,
                )
            ]

    def unread_count(self) -> int:
        with self._session_factory() as session:
            return AdminNotificationRepository(session).unread_count()

    def mark_read(self, notification_id: str) -> AdminNotificationView:
        with self._session_factory() as session:
            repository = AdminNotificationRepository(session)
            record = repository.get(notification_id)
            if record is None:
                raise self._not_found()
            repository.mark_read(notification_id)
            session.commit()
            refreshed = repository.get(notification_id)
            if refreshed is None:
                raise self._not_found()
            return self._view(refreshed)

    def mark_all_read(self) -> int:
        with self._session_factory() as session:
            count = AdminNotificationRepository(session).mark_all_read()
            session.commit()
            return count

    def record_version_update(self, *, current_version: str, latest_version: str) -> None:
        with self._session_factory() as session:
            AdminNotificationRepository(session).create(
                event_type="VERSION_UPDATE_AVAILABLE",
                title="发现 PackBreaker 新版本",
                message=f"当前版本 v{current_version}，发现可更新版本 v{latest_version}。",
                severity="INFO",
                dedup_key=f"version-update:{latest_version}",
            )
            session.commit()

    @staticmethod
    def _view(record: AdminNotification) -> AdminNotificationView:
        return AdminNotificationView(
            id=record.id,
            event_type=record.event_type,
            title=record.title,
            message=record.message,
            severity=record.severity,
            read_at=record.read_at,
            created_at=record.created_at,
        )

    @staticmethod
    def _not_found() -> ApplicationError:
        return ApplicationError(
            code="ADMIN_NOTIFICATION_NOT_FOUND",
            status=404,
            title="站内通知不存在",
            detail="指定站内通知不存在或已经删除",
        )
