from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.domain.errors import DomainViolation, ErrorCode
from backend.app.domain.idempotency import task_idempotency_key
from backend.app.domain.operation import OperationStatus
from backend.app.domain.task_state import (
    TaskStatus,
    TaskTransition,
    transition,
    transition_after_add,
)
from backend.app.domain.verification import DownloaderKind, VerificationLevel
from backend.app.infrastructure.persistence.models import (
    OperationJournal,
    TaskEvent,
    UnpackTask,
    new_uuid,
    utc_now,
)


@dataclass(frozen=True, slots=True)
class TaskCreate:
    task_type: str
    source_downloader_id: str
    source_hash: str
    normalized_unit_key: str
    trace_id: str


@dataclass(frozen=True, slots=True)
class OperationIntent:
    task_id: str
    idempotency_key: str
    operation_type: str
    target: dict[str, Any]
    intent: dict[str, Any]
    before_snapshot: dict[str, Any] | None = None


class TaskRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, task_id: str) -> UnpackTask | None:
        return self._session.get(UnpackTask, task_id)

    def get_by_idempotency_key(self, key: str) -> UnpackTask | None:
        return self._session.scalar(select(UnpackTask).where(UnpackTask.idempotency_key == key))

    def list_recent(
        self, *, status: TaskStatus | None = None, limit: int = 100
    ) -> list[UnpackTask]:
        statement = select(UnpackTask)
        if status is not None:
            statement = statement.where(UnpackTask.status == status.value)
        statement = statement.order_by(UnpackTask.updated_at.desc(), UnpackTask.id.desc()).limit(
            limit
        )
        return list(self._session.scalars(statement))

    def create_or_get(self, request: TaskCreate) -> tuple[UnpackTask, bool]:
        key = task_idempotency_key(
            task_type=request.task_type,
            source_downloader_id=request.source_downloader_id,
            source_hash=request.source_hash,
            normalized_unit_key=request.normalized_unit_key,
        )
        existing = self.get_by_idempotency_key(key)
        if existing is not None:
            return existing, False

        now = utc_now()
        task = UnpackTask(
            id=new_uuid(),
            type=request.task_type,
            source_downloader_id=request.source_downloader_id,
            source_hash=request.source_hash,
            normalized_unit_key=request.normalized_unit_key,
            idempotency_key=key,
            status=TaskStatus.PENDING.value,
            trace_id=request.trace_id,
            checkpoint={},
            version=1,
            created_at=now,
            updated_at=now,
        )
        event = TaskEvent(
            id=new_uuid(),
            task_id=task.id,
            from_status=None,
            to_status=TaskStatus.PENDING.value,
            event_type="TASK_CREATED",
            reason="任务已创建",
            created_at=now,
        )
        try:
            with self._session.begin_nested():
                self._session.add(task)
                self._session.flush()
                self._session.add(event)
                self._session.flush()
        except IntegrityError:
            concurrent = self.get_by_idempotency_key(key)
            if concurrent is None:
                raise
            return concurrent, False
        return task, True

    def transition(
        self,
        *,
        task_id: str,
        expected_version: int,
        to_status: TaskStatus,
        event_type: str,
        reason: str,
        occurred_at: datetime | None = None,
    ) -> UnpackTask:
        task = self._require_version(task_id, expected_version)
        task_transition = transition(
            TaskStatus(task.status),
            to_status,
            reason=reason,
            occurred_at=occurred_at,
        )
        return self._apply_transition(task, expected_version, task_transition, event_type)

    def transition_after_add(
        self,
        *,
        task_id: str,
        expected_version: int,
        downloader: DownloaderKind,
        verification_level: VerificationLevel,
        skip_checking_enabled: bool,
        preflight_current: bool,
        event_type: str,
        reason: str,
        occurred_at: datetime | None = None,
    ) -> UnpackTask:
        task = self._require_version(task_id, expected_version)
        if TaskStatus(task.status) is not TaskStatus.ADDING:
            raise DomainViolation(
                ErrorCode.INVALID_STATE_TRANSITION,
                "下载器添加后的安全门只能从 ADDING 状态执行",
            )
        task_transition = transition_after_add(
            downloader=downloader,
            verification_level=verification_level,
            skip_checking_enabled=skip_checking_enabled,
            preflight_current=preflight_current,
            reason=reason,
            occurred_at=occurred_at,
        )
        return self._apply_transition(task, expected_version, task_transition, event_type)

    def _require_version(self, task_id: str, expected_version: int) -> UnpackTask:
        task = self.get(task_id)
        if task is None:
            raise DomainViolation(ErrorCode.TASK_NOT_FOUND, "任务不存在")
        if task.version != expected_version:
            raise DomainViolation(ErrorCode.TASK_VERSION_CONFLICT, "任务版本已变化，请重新加载")
        return task

    def _apply_transition(
        self,
        task: UnpackTask,
        expected_version: int,
        task_transition: TaskTransition,
        event_type: str,
    ) -> UnpackTask:
        with self._session.begin_nested():
            updated_task_id = self._session.scalar(
                update(UnpackTask)
                .where(UnpackTask.id == task.id, UnpackTask.version == expected_version)
                .values(
                    status=task_transition.to_status.value,
                    version=expected_version + 1,
                    updated_at=task_transition.occurred_at,
                )
                .returning(UnpackTask.id)
            )
            if updated_task_id is None:
                raise DomainViolation(ErrorCode.TASK_VERSION_CONFLICT, "任务版本已变化，请重新加载")
            self._session.add(
                TaskEvent(
                    id=new_uuid(),
                    task_id=task.id,
                    from_status=task_transition.from_status.value,
                    to_status=task_transition.to_status.value,
                    event_type=event_type,
                    reason=task_transition.reason,
                    created_at=task_transition.occurred_at,
                )
            )
            self._session.flush()
        self._session.expire(task)
        self._session.refresh(task)
        return task


class OperationJournalRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_idempotency_key(self, key: str) -> OperationJournal | None:
        return self._session.scalar(
            select(OperationJournal).where(OperationJournal.idempotency_key == key)
        )

    def record_intent(self, request: OperationIntent) -> tuple[OperationJournal, bool]:
        existing = self.get_by_idempotency_key(request.idempotency_key)
        if existing is not None:
            self._ensure_same_intent(existing, request)
            return existing, False

        now = utc_now()
        journal = OperationJournal(
            id=new_uuid(),
            task_id=request.task_id,
            idempotency_key=request.idempotency_key,
            operation_type=request.operation_type,
            target=deepcopy(request.target),
            intent=deepcopy(request.intent),
            status=OperationStatus.INTENT_RECORDED.value,
            before_snapshot=deepcopy(request.before_snapshot),
            after_snapshot=None,
            created_at=now,
            updated_at=now,
        )
        try:
            with self._session.begin_nested():
                self._session.add(journal)
                self._session.flush()
        except IntegrityError:
            concurrent = self.get_by_idempotency_key(request.idempotency_key)
            if concurrent is None:
                raise
            self._ensure_same_intent(concurrent, request)
            return concurrent, False
        return journal, True

    @staticmethod
    def _ensure_same_intent(existing: OperationJournal, request: OperationIntent) -> None:
        if (
            existing.task_id != request.task_id
            or existing.operation_type != request.operation_type
            or existing.target != request.target
            or existing.intent != request.intent
            or existing.before_snapshot != request.before_snapshot
        ):
            raise DomainViolation(
                ErrorCode.IDEMPOTENCY_CONFLICT,
                "相同操作幂等键对应了不同的执行意图",
            )
