from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.domain.errors import DomainViolation, ErrorCode
from backend.app.domain.idempotency import task_idempotency_key
from backend.app.domain.operation import OperationStatus, transition_operation
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

    def latest_event(self, task_id: str) -> TaskEvent | None:
        return self._session.scalar(
            select(TaskEvent)
            .where(TaskEvent.task_id == task_id)
            .order_by(TaskEvent.created_at.desc(), TaskEvent.id.desc())
            .limit(1)
        )

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
        checkpoint: dict[str, Any] | None = None,
    ) -> UnpackTask:
        task = self._require_version(task_id, expected_version)
        task_transition = transition(
            TaskStatus(task.status),
            to_status,
            reason=reason,
            occurred_at=occurred_at,
        )
        return self._apply_transition(
            task,
            expected_version,
            task_transition,
            event_type,
            checkpoint=checkpoint,
        )

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
        checkpoint: dict[str, Any] | None = None,
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
        return self._apply_transition(
            task,
            expected_version,
            task_transition,
            event_type,
            checkpoint=checkpoint,
        )

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
        *,
        checkpoint: dict[str, Any] | None = None,
    ) -> UnpackTask:
        values: dict[str, Any] = {
            "status": task_transition.to_status.value,
            "version": expected_version + 1,
            "updated_at": task_transition.occurred_at,
        }
        if checkpoint is not None:
            values["checkpoint"] = deepcopy(checkpoint)
        with self._session.begin_nested():
            updated_task_id = self._session.scalar(
                update(UnpackTask)
                .where(UnpackTask.id == task.id, UnpackTask.version == expected_version)
                .values(**values)
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

    def get(self, journal_id: str) -> OperationJournal | None:
        return self._session.get(OperationJournal, journal_id)

    def list_recoverable(self, *, limit: int = 100) -> list[OperationJournal]:
        if limit <= 0:
            raise ValueError("limit 必须大于 0")
        recoverable = (
            OperationStatus.INTENT_RECORDED.value,
            OperationStatus.APPLIED.value,
            OperationStatus.ROLLBACK_PENDING.value,
            OperationStatus.RECONCILE_REQUIRED.value,
            OperationStatus.ROLLBACK_BLOCKED.value,
        )
        return list(
            self._session.scalars(
                select(OperationJournal)
                .where(OperationJournal.status.in_(recoverable))
                .order_by(OperationJournal.updated_at.asc(), OperationJournal.id.asc())
                .limit(limit)
            )
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

    def transition_status(
        self,
        *,
        journal_id: str,
        expected_status: OperationStatus,
        to_status: OperationStatus,
        after_snapshot: dict[str, Any] | None = None,
    ) -> OperationJournal:
        journal = self.get(journal_id)
        if journal is None:
            raise DomainViolation(ErrorCode.TASK_NOT_FOUND, "operation journal 不存在")
        current = OperationStatus(journal.status)
        if current is not expected_status:
            raise DomainViolation(
                ErrorCode.TASK_VERSION_CONFLICT,
                "operation journal 状态已变化，请重新读取后对账",
            )
        transition_operation(current, to_status)
        if to_status is OperationStatus.APPLIED and after_snapshot is None:
            raise DomainViolation(
                ErrorCode.SOURCE_NOT_STABLE,
                "APPLIED 必须保存副作用完成后的目标快照",
            )
        if to_status is not OperationStatus.APPLIED and after_snapshot is not None:
            raise DomainViolation(
                ErrorCode.INVALID_STATE_TRANSITION,
                "只有 APPLIED 转换可以写入 after snapshot",
            )

        values: dict[str, Any] = {"status": to_status.value, "updated_at": utc_now()}
        if after_snapshot is not None:
            values["after_snapshot"] = deepcopy(after_snapshot)
        with self._session.begin_nested():
            updated_id = self._session.scalar(
                update(OperationJournal)
                .where(
                    OperationJournal.id == journal_id,
                    OperationJournal.status == expected_status.value,
                )
                .values(**values)
                .returning(OperationJournal.id)
            )
            if updated_id is None:
                raise DomainViolation(
                    ErrorCode.TASK_VERSION_CONFLICT,
                    "operation journal 状态已变化，请重新读取后对账",
                )
        self._session.expire(journal)
        self._session.refresh(journal)
        return journal

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
