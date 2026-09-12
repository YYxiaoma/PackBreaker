from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
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
from backend.app.domain.task_state import (
    ACTIVE_ANALYSIS_STATUSES,
    PRE_SIDE_EFFECT_CANCELLATION_SCHEMA_VERSION,
    TaskCancellationMode,
    TaskStatus,
)
from backend.app.infrastructure.persistence.repositories import (
    OperationJournalRepository,
    TaskActionReceiptCreate,
    TaskActionReceiptRepository,
    TaskRepository,
)
from backend.app.infrastructure.persistence.task_analysis_repositories import (
    TaskExecutionPlanRepository,
)

TASK_ACTION_SCHEMA_VERSION = "packbreaker-task-action-v1"
_ACTION_LOCKS: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
_PRE_SIDE_EFFECT_CANCELLABLE_STATUSES = frozenset(
    {
        TaskStatus.PENDING,
        TaskStatus.PREFLIGHT,
        TaskStatus.AWAITING_CONFIRMATION,
        TaskStatus.PAUSED,
        TaskStatus.RETRY,
    }
)
_SIDE_EFFECT_CANCELLATION_STATUSES = frozenset(
    {
        TaskStatus.LINKING,
        TaskStatus.ADDING,
        TaskStatus.CLIENT_VERIFYING,
        TaskStatus.SEEDING,
        TaskStatus.CANCELLING,
        TaskStatus.ROLLING_BACK,
    }
)


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
    execution_plan_id: str | None
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
        fault_hook: Callable[[str], None] | None = None,
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
                result = self._cancel_before_side_effects(
                    request,
                    receipt_id=receipt.id,
                    idempotency_replayed=replayed,
                )
                if result is None:
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
                if fault_hook is not None:
                    fault_hook("after_cancellation_applied")
                self._finalize_success(receipt.id, result)
                return result
            except ApplicationError as exc:
                self._finalize_failure(receipt.id, exc)
                raise
            except DomainViolation as exc:
                error = _domain_application_error(exc)
                self._finalize_failure(receipt.id, error)
                raise error from exc

    def _cancel_before_side_effects(
        self,
        request: CancelTaskAction,
        *,
        receipt_id: str,
        idempotency_replayed: bool,
    ) -> TaskMutationActionResult | None:
        with self._session_factory() as session:
            repository = TaskRepository(session)
            task = repository.get(request.task_id)
            if task is None:
                raise _task_not_found()
            try:
                status = TaskStatus(task.status)
            except ValueError as exc:
                raise _cancellation_state_invalid("任务包含未知状态") from exc

            if status is TaskStatus.CANCELLED:
                recovered = self._load_pre_side_effect_cancelled(
                    task,
                    request,
                    receipt_id=receipt_id,
                    idempotency_replayed=idempotency_replayed,
                )
                if recovered is not None:
                    return recovered
                return None
            if status is TaskStatus.CANCELLING:
                cooperative = self._load_cooperative_analysis_cancelling(
                    task,
                    request,
                    receipt_id=receipt_id,
                    idempotency_replayed=idempotency_replayed,
                )
                if cooperative is not None:
                    return cooperative
            if status in _SIDE_EFFECT_CANCELLATION_STATUSES:
                return None
            if status not in _PRE_SIDE_EFFECT_CANCELLABLE_STATUSES | ACTIVE_ANALYSIS_STATUSES:
                raise _cancellation_state_invalid(
                    "当前任务已经结束，或不属于可证明零副作用的取消状态"
                )
            journals = OperationJournalRepository(session).list_for_task(task.id)
            if status is TaskStatus.RETRY and journals:
                return None
            if request.remove_downloader_task or request.rollback_created_resources:
                raise ApplicationError(
                    code="CANCELLATION_OPTIONS_NOT_APPLICABLE",
                    status=409,
                    title="零副作用取消不接受资源回收选项",
                    detail=(
                        "任务尚未进入 LINKING；remove_downloader_task 与 "
                        "rollback_created_resources 必须同时为 false"
                    ),
                )
            if journals:
                raise ApplicationError(
                    code="CANCELLATION_EVIDENCE_CONFLICT",
                    status=409,
                    title="任务已有副作用日志，不能按零副作用取消",
                    detail="发现 operation journal；必须使用已进入副作用阶段的安全取消/回滚链对账",
                )

            requested_from_status = status
            if status in ACTIVE_ANALYSIS_STATUSES:
                cooperative_checkpoint = _pre_side_effect_cancellation_checkpoint(
                    requested_from_status,
                    stage=TaskStatus.CANCELLING,
                    mode=TaskCancellationMode.COOPERATIVE_ANALYSIS,
                    analysis_version=task.version,
                )
                try:
                    task = repository.transition(
                        task_id=task.id,
                        expected_version=task.version,
                        to_status=TaskStatus.CANCELLING,
                        event_type="CANCELLATION_STARTED",
                        reason="取消请求已登记；等待只读分析在下一个安全检查点停止",
                        checkpoint=cooperative_checkpoint,
                    )
                except DomainViolation as exc:
                    raise ApplicationError(
                        code="CANCELLATION_TASK_CHANGED",
                        status=409,
                        title="取消期间任务状态已经变化",
                        detail="任务版本或状态发生并发变化，请重新读取后决定取消方式",
                    ) from exc
                session.commit()
                return TaskMutationActionResult(
                    action="cancel",
                    task_id=task.id,
                    status=TaskStatus.CANCELLING,
                    task_version=task.version,
                    execution_plan_id=None,
                    operation_replayed=False,
                    receipt_id=receipt_id,
                    idempotency_replayed=idempotency_replayed,
                )

            cancelling_checkpoint = _pre_side_effect_cancellation_checkpoint(
                requested_from_status,
                stage=TaskStatus.CANCELLING,
                mode=TaskCancellationMode.NO_SIDE_EFFECTS,
            )
            try:
                task = repository.transition(
                    task_id=task.id,
                    expected_version=task.version,
                    to_status=TaskStatus.CANCELLING,
                    event_type="CANCELLATION_STARTED",
                    reason="用户请求取消尚未进入副作用阶段的任务",
                    checkpoint=cancelling_checkpoint,
                )
                task = repository.transition(
                    task_id=task.id,
                    expected_version=task.version,
                    to_status=TaskStatus.CANCELLED,
                    event_type="CANCELLATION_COMPLETED",
                    reason="未发现 operation journal；任务已在副作用开始前安全取消",
                    checkpoint=_pre_side_effect_cancellation_checkpoint(
                        requested_from_status,
                        stage=TaskStatus.CANCELLED,
                        mode=TaskCancellationMode.NO_SIDE_EFFECTS,
                    ),
                )
            except DomainViolation as exc:
                raise ApplicationError(
                    code="CANCELLATION_TASK_CHANGED",
                    status=409,
                    title="取消期间任务状态已经变化",
                    detail="任务版本或状态发生并发变化，请重新读取后决定取消方式",
                ) from exc
            session.commit()
            return TaskMutationActionResult(
                action="cancel",
                task_id=task.id,
                status=TaskStatus.CANCELLED,
                task_version=task.version,
                execution_plan_id=None,
                operation_replayed=False,
                receipt_id=receipt_id,
                idempotency_replayed=idempotency_replayed,
            )

    def _load_pre_side_effect_cancelled(
        self,
        task: Any,
        request: CancelTaskAction,
        *,
        receipt_id: str,
        idempotency_replayed: bool,
    ) -> TaskMutationActionResult | None:
        checkpoint = task.checkpoint
        if checkpoint.get("schema_version") != PRE_SIDE_EFFECT_CANCELLATION_SCHEMA_VERSION:
            return None
        mode = checkpoint.get("mode")
        if checkpoint.get("stage") != TaskStatus.CANCELLED.value or mode not in {
            TaskCancellationMode.NO_SIDE_EFFECTS.value,
            TaskCancellationMode.COOPERATIVE_ANALYSIS.value,
        }:
            raise ApplicationError(
                code="CANCELLATION_EVIDENCE_INVALID",
                status=409,
                title="已取消任务的零副作用证据无效",
                detail="CANCELLED checkpoint 不能证明该任务在副作用开始前完成取消",
            )
        if (
            checkpoint.get("remove_downloader_task") is not False
            or checkpoint.get("rollback_created_resources") is not False
        ):
            raise ApplicationError(
                code="CANCELLATION_EVIDENCE_INVALID",
                status=409,
                title="已取消任务的零副作用证据无效",
                detail="副作用开始前取消 checkpoint 必须明确冻结 remove=false、rollback=false",
            )
        requested_from = checkpoint.get("requested_from_status")
        if mode == TaskCancellationMode.NO_SIDE_EFFECTS.value and requested_from not in {
            status.value for status in _PRE_SIDE_EFFECT_CANCELLABLE_STATUSES
        }:
            raise ApplicationError(
                code="CANCELLATION_EVIDENCE_INVALID",
                status=409,
                title="已取消任务的零副作用证据无效",
                detail="NO_SIDE_EFFECTS checkpoint 缺少有效的原稳定任务状态绑定",
            )
        if mode == TaskCancellationMode.COOPERATIVE_ANALYSIS.value:
            analysis_version = checkpoint.get("analysis_version")
            if (
                requested_from not in {status.value for status in ACTIVE_ANALYSIS_STATUSES}
                or not isinstance(analysis_version, int)
                or isinstance(analysis_version, bool)
                or analysis_version < 1
            ):
                raise ApplicationError(
                    code="CANCELLATION_EVIDENCE_INVALID",
                    status=409,
                    title="已取消任务的协作式分析证据无效",
                    detail="COOPERATIVE_ANALYSIS checkpoint 缺少有效的原分析 stage/version 绑定",
                )
        if request.remove_downloader_task or request.rollback_created_resources:
            raise ApplicationError(
                code="CANCELLATION_OPTION_CONFLICT",
                status=409,
                title="取消重放选项与已完成请求不一致",
                detail="零副作用取消固定使用 remove=false、rollback=false",
            )
        return TaskMutationActionResult(
            action="cancel",
            task_id=task.id,
            status=TaskStatus.CANCELLED,
            task_version=task.version,
            execution_plan_id=None,
            operation_replayed=True,
            receipt_id=receipt_id,
            idempotency_replayed=idempotency_replayed,
        )

    def _load_cooperative_analysis_cancelling(
        self,
        task: Any,
        request: CancelTaskAction,
        *,
        receipt_id: str,
        idempotency_replayed: bool,
    ) -> TaskMutationActionResult | None:
        checkpoint = task.checkpoint
        if checkpoint.get("schema_version") != PRE_SIDE_EFFECT_CANCELLATION_SCHEMA_VERSION:
            return None
        if checkpoint.get("mode") != TaskCancellationMode.COOPERATIVE_ANALYSIS.value:
            return None
        if checkpoint.get("stage") != TaskStatus.CANCELLING.value:
            raise ApplicationError(
                code="CANCELLATION_EVIDENCE_INVALID",
                status=409,
                title="分析取消证据无效",
                detail="协作式分析取消 checkpoint 的 stage 与任务状态不一致",
            )
        if (
            checkpoint.get("remove_downloader_task") is not False
            or checkpoint.get("rollback_created_resources") is not False
        ):
            raise ApplicationError(
                code="CANCELLATION_EVIDENCE_INVALID",
                status=409,
                title="分析取消证据无效",
                detail="协作式分析取消 checkpoint 必须明确冻结 remove=false、rollback=false",
            )
        if request.remove_downloader_task or request.rollback_created_resources:
            raise ApplicationError(
                code="CANCELLATION_OPTION_CONFLICT",
                status=409,
                title="取消重放选项与已登记请求不一致",
                detail="协作式分析取消固定使用 remove=false、rollback=false",
            )
        requested_from = checkpoint.get("requested_from_status")
        analysis_version = checkpoint.get("analysis_version")
        if (
            requested_from not in {status.value for status in ACTIVE_ANALYSIS_STATUSES}
            or not isinstance(analysis_version, int)
            or isinstance(analysis_version, bool)
            or analysis_version < 1
        ):
            raise ApplicationError(
                code="CANCELLATION_EVIDENCE_INVALID",
                status=409,
                title="分析取消证据无效",
                detail="协作式分析取消缺少有效的原分析 stage/version 绑定",
            )
        return TaskMutationActionResult(
            action="cancel",
            task_id=task.id,
            status=TaskStatus.CANCELLING,
            task_version=task.version,
            execution_plan_id=None,
            operation_replayed=True,
            receipt_id=receipt_id,
            idempotency_replayed=idempotency_replayed,
        )

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
        or (execution_plan_id is not None and not isinstance(execution_plan_id, str))
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


def _pre_side_effect_cancellation_checkpoint(
    requested_from_status: TaskStatus,
    *,
    stage: TaskStatus,
    mode: TaskCancellationMode,
    analysis_version: int | None = None,
) -> dict[str, Any]:
    checkpoint: dict[str, Any] = {
        "schema_version": PRE_SIDE_EFFECT_CANCELLATION_SCHEMA_VERSION,
        "stage": stage.value,
        "mode": mode.value,
        "requested_from_status": requested_from_status.value,
        "remove_downloader_task": False,
        "rollback_created_resources": False,
    }
    if analysis_version is not None:
        checkpoint["analysis_version"] = analysis_version
    return checkpoint


def _cancellation_state_invalid(detail: str) -> ApplicationError:
    return ApplicationError(
        code="CANCELLATION_STATE_INVALID",
        status=409,
        title="当前任务状态不允许该取消方式",
        detail=detail,
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
