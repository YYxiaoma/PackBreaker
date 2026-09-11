from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.errors import ApplicationError
from backend.app.infrastructure.persistence.repositories import TaskRepository


@dataclass(frozen=True, slots=True)
class TaskEventView:
    id: str
    task_id: str
    from_status: str | None
    to_status: str
    event_type: str
    reason: str
    created_at: datetime


class TaskEventService:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def list_events(
        self,
        task_id: str,
        *,
        after_event_id: str | None = None,
        limit: int = 100,
    ) -> tuple[TaskEventView, ...]:
        bounded_limit = max(1, min(limit, 500))
        with self._session_factory() as session:
            repository = TaskRepository(session)
            if repository.get(task_id) is None:
                raise ApplicationError(
                    code="TASK_NOT_FOUND",
                    status=404,
                    title="任务不存在",
                    detail="无法读取不存在任务的事件流",
                )

            after_created_at: datetime | None = None
            if after_event_id is not None:
                cursor = repository.get_event(after_event_id)
                if cursor is None or cursor.task_id != task_id:
                    raise ApplicationError(
                        code="TASK_EVENT_CURSOR_INVALID",
                        status=409,
                        title="任务事件游标无效",
                        detail="after_event_id 不属于当前任务或事件已不存在",
                    )
                after_created_at = cursor.created_at

            return tuple(
                TaskEventView(
                    id=item.id,
                    task_id=item.task_id,
                    from_status=item.from_status,
                    to_status=item.to_status,
                    event_type=item.event_type,
                    reason=item.reason,
                    created_at=item.created_at,
                )
                for item in repository.list_events(
                    task_id=task_id,
                    after_created_at=after_created_at,
                    after_event_id=after_event_id,
                    limit=bounded_limit,
                )
            )
