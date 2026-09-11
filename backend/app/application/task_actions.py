from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, replace
from hashlib import sha256
from typing import Any, Literal, Protocol
from weakref import WeakValueDictionary

from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.errors import ApplicationError
from backend.app.application.task_cancellation import (
    TaskCancellationRequest,
    TaskCancellationResult,
)
from backend.app.application.task_linking import TaskLinkingResult
from backend.app.domain.errors import DomainViolation, ErrorCode
from backend.app.domain.task_state import TaskStatus
from backend.app.infrastructure.persistence.repositories import (
    TaskActionReceiptCreate,
    TaskActionReceiptRepository,
    TaskRepository,
)
from backend.app.infrastructure.persistence.task_analysis_repositories import (
    TaskExecutionPlanRepository,
)

TASK_ACTION_SCHEMA_VERSION = "packbreaker-task-action-v1"
_ACTION_LOCKS: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()


@dataclass(frozen=True, slots=True)
class TaskActionActor:
    kind: str
    subject_id: str


@dataclass(frozen=True, slots=True)
class ExecuteTaskAction:
    task_id: str
    execution_plan_id: str


@dataclass(frozen=True, slots=True)
class CancelTaskAction:
    task_id: str
    remove_downloader_task: bool
    rollback_created_resources: bool


@dataclass(frozen=True, slots=True)
class TaskMutationActionResult:
    action: Literal["execute", "cancel"]
    task_id: str
    status: TaskStatus
    task_version: int
    execution_plan_id: str
    operation_replayed: bool
    receipt_id: str
    idempotency_replayed: bool


class LinkingPort(Protocol):
    def execute(self, unit_id: str, *, execution_plan_id: str) -> TaskLinkingResult: ...


class CancellationPort(Protocol):
    async def execute(self, request: TaskCancellationRequest) -> TaskCancellationResult: ...


class TaskActionService:
    """公开副作用动作的外层鉴权后幂等/审计边界；真实副作用仍由既有 coordinator 承担。"""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        linking: LinkingPort,
        cancellation: CancellationPort,
    ) -> None:
        self._session_factory = session_factory
        self._linking = linking
        self._cancellation = cancellation

    async def execute(
        self,
        request: ExecuteTaskAction,
        *,
        actor: TaskActionActor,
        idempotency_key: str | None,
    ) -> TaskMutationActionResult:
        key_digest = _idempotency_key_digest(idempotency_key)
        request_digest = _request_digest(
            {
                "action": "execute",
                "execution_plan_id": request.execution_plan_id,
                "task_id": request.task_id,
            }
        )
        lock = _action_lock(actor, key_digest)
        async with lock:
            self._require_task(request.task_id)
            receipt, replayed = self._record_pending(
                task_id=request.task_id,
                actor=actor,
                key_digest=key_digest,
                action="execute",
                request_digest=request_digest,
            )
            completed = _completed_receipt(receipt, idempotency_replayed=True)
            if completed is not None:
                return completed
            try:
                if replayed:
                    recovered = self._recover_advanced_execution(
                        request.task_id,
                        request.execution_plan_id,
                        receipt.id,
                    )
                    if recovered is not None:
                        self._finalize_success(receipt.id, recovered)
                        return recovered
                unit_id = self._load_execution_unit(request.task_id, request.execution_plan_id)
                linked = self._linking.execute(
                    unit_id,
                    execution_plan_id=request.execution_plan_id,
                )
                result = TaskMutationActionResult(
                    action="execute",
                    task_id=linked.task_id,
                    status=TaskStatus.ADDING,
                    task_version=linked.task_version,
                    execution_plan_id=linked.execution_plan_id,
                    operation_replayed=linked.replayed,
                    receipt_id=receipt.id,
                    idempotency_replayed=replayed,
                )
                self._finalize_success(receipt.id, result)
                return result
            except ApplicationError as exc:
                self._finalize_failure(receipt.id, exc)
                raise
            except DomainViolation as exc:
                error = _domain_application_error(exc)
                self._finalize_failure(receipt.id, error)
                raise error from exc

    async def cancel(
        self,
        request: CancelTaskAction,
        *,
        actor: TaskActionActor,
        idempotency_key: str | None,
    ) -> TaskMutationActionResult:
        key_digest = _idempotency_key_digest(idempotency_key)
        request_digest = _request_digest(
            {
                "action": "cancel",
                "remove_downloader_task": request.remove_downloader_task,
                "rollback_created_resources": request.rollback_created_resources,
                "task_id": request.task_id,
            }
        )
        lock = _action_lock(actor, key_digest)
        async with lock:
            self._require_task(request.task_id)
            receipt, replayed = self._record_pending(
                task_id=request.task_id,
                actor=actor,
                key_digest=key_digest,
                action="cancel",
                request_digest=request_digest,
            )
            completed = _completed_receipt(receipt, idempotency_replayed=True)
            if completed is not None:
                return completed
            try:
                cancelled = await self._cancellation.execute(
                    TaskCancellationRequest(
                        task_id=request.task_id,
                        remove_downloader_task=request.remove_downloader_task,
                        rollback_created_resources=request.rollback_created_resources,
                    )
                )
                result = TaskMutationActionResult(
                    action="cancel",
                    task_id=cancelled.task_id,
                    status=cancelled.status,
                    task_version=cancelled.task_version,
                    execution_plan_id=cancelled.execution_plan_id,
                    operation_replayed=cancelled.replayed,
                    receipt_id=receipt.id,
                    idempotency_replayed=replayed,
                )
                self._finalize_success(receipt.id, result)
                return result
            except ApplicationError as exc:
                self._finalize_failure(receipt.id, exc)
                raise
            except DomainViolation as exc:
                error = _domain_application_error(exc)
                self._finalize_failure(receipt.id, error)
                raise error from exc

    def _load_execution_unit(self, task_id: str, plan_id: str) -> str:
        with self._session_factory() as session:
            task = TaskRepository(session).get(task_id)
            if task is None:
                raise _task_not_found()
            plan = TaskExecutionPlanRepository(session).get(plan_id)
            if plan is None or plan.task_id != task.id:
                raise ApplicationError(
                    code="TASK_EXECUTION_PLAN_MISMATCH",
                    status=409,
                    title="执行计划与任务不匹配",
                    detail="公开执行动作只能使用属于当前任务的 execution plan",
                )
            return plan.task_unit_id

    def _recover_advanced_execution(
        self,
        task_id: str,
        plan_id: str,
        receipt_id: str,
    ) -> TaskMutationActionResult | None:
        with self._session_factory() as session:
            task = TaskRepository(session).get(task_id)
            plan = TaskExecutionPlanRepository(session).get(plan_id)
            if task is None or plan is None or plan.task_id != task.id:
                return None
            try:
                status = TaskStatus(task.status)
            except ValueError:
                return None
            if status in {
                TaskStatus.AWAITING_CONFIRMATION,
                TaskStatus.LINKING,
                TaskStatus.ADDING,
            }:
                return None
            checkpoint = task.checkpoint
            if (
                checkpoint.get("execution_plan_id") != plan.id
                or checkpoint.get("execution_plan_digest") != plan.plan_digest
            ):
                return None
            return TaskMutationActionResult(
                action="execute",
                task_id=task.id,
                status=status,
                task_version=task.version,
                execution_plan_id=plan.id,
                operation_replayed=True,
                receipt_id=receipt_id,
                idempotency_replayed=True,
            )

    def _require_task(self, task_id: str) -> None:
        with self._session_factory() as session:
            if TaskRepository(session).get(task_id) is None:
                raise _task_not_found()

    def _record_pending(
        self,
        *,
        task_id: str,
        actor: TaskActionActor,
        key_digest: str,
        action: Literal["execute", "cancel"],
        request_digest: str,
    ) -> tuple[_ReceiptView, bool]:
        with self._session_factory() as session:
            repository = TaskActionReceiptRepository(session)
            try:
                receipt, created = repository.record_pending(
                    TaskActionReceiptCreate(
                        task_id=task_id,
                        actor_kind=actor.kind,
                        actor_id=actor.subject_id,
                        idempotency_key_digest=key_digest,
                        action=action,
                        request_digest=request_digest,
                    )
                )
            except DomainViolation as exc:
                if exc.code is ErrorCode.IDEMPOTENCY_CONFLICT:
                    raise ApplicationError(
                        code="IDEMPOTENCY_CONFLICT",
                        status=409,
                        title="幂等键冲突",
                        detail="相同调用方的 Idempotency-Key 已绑定其他任务动作请求",
                    ) from exc
                raise
            if created:
                TaskRepository(session).append_event(
                    task_id=task_id,
                    event_type=(
                        "TASK_EXECUTE_REQUESTED" if action == "execute" else "TASK_CANCEL_REQUESTED"
                    ),
                    reason=f"{actor.kind} 已提交 {action} 动作请求，幂等 receipt 已持久化",
                )
            session.commit()
            return _receipt_view(receipt), not created

    def _finalize_success(self, receipt_id: str, result: TaskMutationActionResult) -> None:
        with self._session_factory() as session:
            TaskActionReceiptRepository(session).finalize_success(
                receipt_id,
                _result_payload(result),
            )
            session.commit()

    def _finalize_failure(self, receipt_id: str, error: ApplicationError) -> None:
        with self._session_factory() as session:
            TaskActionReceiptRepository(session).finalize_failure(
                receipt_id,
                {
                    "code": error.code,
                    "detail": error.detail,
                    "retry_after": error.retry_after,
                    "status": error.status,
                    "title": error.title,
                },
            )
            session.commit()


@dataclass(frozen=True, slots=True)
class _ReceiptView:
    id: str
    state: str
    response_payload: dict[str, Any] | None
    error_payload: dict[str, Any] | None


def _receipt_view(receipt: Any) -> _ReceiptView:
    return _ReceiptView(
        id=receipt.id,
        state=receipt.state,
        response_payload=None
        if receipt.response_payload is None
        else dict(receipt.response_payload),
        error_payload=None if receipt.error_payload is None else dict(receipt.error_payload),
    )


def _completed_receipt(
    receipt: _ReceiptView,
    *,
    idempotency_replayed: bool,
) -> TaskMutationActionResult | None:
    if receipt.state == "SUCCEEDED":
        if receipt.response_payload is None:
            raise RuntimeError("SUCCEEDED task action receipt 缺少 response payload")
        return replace(
            _result_from_payload(receipt.id, receipt.response_payload),
            idempotency_replayed=idempotency_replayed,
        )
    if receipt.state == "FAILED":
        if receipt.error_payload is None:
            raise RuntimeError("FAILED task action receipt 缺少 error payload")
        raise _error_from_payload(receipt.error_payload)
    if receipt.state != "PENDING":
        raise RuntimeError("task action receipt 保存了未知状态")
    return None


def _result_payload(result: TaskMutationActionResult) -> dict[str, Any]:
    return {
        "action": result.action,
        "execution_plan_id": result.execution_plan_id,
        "operation_replayed": result.operation_replayed,
        "status": result.status.value,
        "task_id": result.task_id,
        "task_version": result.task_version,
    }


def _result_from_payload(receipt_id: str, payload: dict[str, Any]) -> TaskMutationActionResult:
    action = payload.get("action")
    if action not in {"execute", "cancel"}:
        raise RuntimeError("task action receipt response action 无效")
    task_id = payload.get("task_id")
    task_version = payload.get("task_version")
    execution_plan_id = payload.get("execution_plan_id")
    status = payload.get("status")
    operation_replayed = payload.get("operation_replayed")
    if (
        not isinstance(task_id, str)
        or not isinstance(task_version, int)
        or isinstance(task_version, bool)
        or not isinstance(execution_plan_id, str)
        or not isinstance(status, str)
        or not isinstance(operation_replayed, bool)
    ):
        raise RuntimeError("task action receipt response payload 无效")
    return TaskMutationActionResult(
        action=action,
        task_id=task_id,
        status=TaskStatus(status),
        task_version=task_version,
        execution_plan_id=execution_plan_id,
        operation_replayed=operation_replayed,
        receipt_id=receipt_id,
        idempotency_replayed=True,
    )


def _error_from_payload(payload: dict[str, Any]) -> ApplicationError:
    code = payload.get("code")
    status = payload.get("status")
    title = payload.get("title")
    detail = payload.get("detail")
    retry_after = payload.get("retry_after")
    if (
        not isinstance(code, str)
        or not isinstance(status, int)
        or isinstance(status, bool)
        or not isinstance(title, str)
        or not isinstance(detail, str)
        or (retry_after is not None and not isinstance(retry_after, int))
    ):
        raise RuntimeError("task action receipt error payload 无效")
    return ApplicationError(
        code=code,
        status=status,
        title=title,
        detail=detail,
        retry_after=retry_after,
    )


def _idempotency_key_digest(value: str | None) -> str:
    if value is None:
        raise ApplicationError(
            code="IDEMPOTENCY_KEY_REQUIRED",
            status=428,
            title="缺少幂等键",
            detail="execute/cancel 请求必须携带 Idempotency-Key",
        )
    if not value or len(value) > 200 or value.strip() != value:
        raise _invalid_idempotency_key()
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise _invalid_idempotency_key() from exc
    if any(byte < 0x21 or byte > 0x7E for byte in encoded):
        raise _invalid_idempotency_key()
    return sha256(encoded).hexdigest()


def _request_digest(payload: dict[str, Any]) -> str:
    canonical = {
        "schema_version": TASK_ACTION_SCHEMA_VERSION,
        **payload,
    }
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _action_lock(actor: TaskActionActor, key_digest: str) -> asyncio.Lock:
    lock_key = f"{actor.kind}:{actor.subject_id}:{key_digest}"
    lock = _ACTION_LOCKS.get(lock_key)
    if lock is None:
        lock = asyncio.Lock()
        _ACTION_LOCKS[lock_key] = lock
    return lock


def _domain_application_error(exc: DomainViolation) -> ApplicationError:
    return ApplicationError(
        code=exc.code.value,
        status=409,
        title="任务动作安全检查失败",
        detail=str(exc),
    )


def _task_not_found() -> ApplicationError:
    return ApplicationError(
        code="TASK_NOT_FOUND",
        status=404,
        title="任务不存在",
        detail="任务不存在或已被删除",
    )


def _invalid_idempotency_key() -> ApplicationError:
    return ApplicationError(
        code="IDEMPOTENCY_KEY_INVALID",
        status=422,
        title="幂等键无效",
        detail="Idempotency-Key 必须是 1..200 个不含空白/控制字符的可见 ASCII 字符",
    )
