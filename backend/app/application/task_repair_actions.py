from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, replace
from hashlib import sha256
from typing import Any, Literal, Protocol
from weakref import WeakValueDictionary

from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.errors import ApplicationError
from backend.app.application.task_actions import TaskActionActor
from backend.app.application.task_repairs import (
    REPAIR_DOWNLOAD_CHECKPOINT_SCHEMA_VERSION,
    REPAIR_STAGE_DOWNLOAD_PENDING,
    REPAIR_STAGE_DOWNLOADING,
    REPAIR_STAGE_INCOMPLETE,
    REPAIR_STAGE_RECHECK_PENDING,
    REPAIR_STAGE_RECHECKING,
    REPAIR_STAGE_VERIFIED,
    TaskRepairExecutionResult,
)
from backend.app.domain.errors import DomainViolation, ErrorCode
from backend.app.domain.task_state import TaskStatus
from backend.app.infrastructure.persistence.repositories import (
    TaskActionReceiptCreate,
    TaskActionReceiptRepository,
    TaskRepository,
)
from backend.app.infrastructure.persistence.task_analysis_repositories import (
    TaskExecutionPlanRepository,
    TaskUnitRepository,
)

REPAIR_ACTION_SCHEMA_VERSION = "packbreaker-repair-action-v1"
_REPAIR_ACTION_LOCKS: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()


@dataclass(frozen=True, slots=True)
class TaskRepairActionResult:
    action: Literal["execute"]
    task_id: str
    task_unit_id: str
    status: TaskStatus
    task_version: int
    execution_plan_id: str
    operation_replayed: bool
    receipt_id: str
    idempotency_replayed: bool


class RepairExecutionPort(Protocol):
    async def execute(self, unit_id: str) -> TaskRepairExecutionResult: ...


@dataclass(frozen=True, slots=True)
class _ReceiptView:
    id: str
    state: str
    response_payload: dict[str, Any] | None
    error_payload: dict[str, Any] | None


class TaskRepairActionService:
    """公开 repair execute 的幂等/审计边界；客户端只选择 unit，不提交安全事实。"""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        coordinator: RepairExecutionPort,
    ) -> None:
        self._session_factory = session_factory
        self._coordinator = coordinator

    async def execute(
        self,
        unit_id: str,
        *,
        actor: TaskActionActor,
        idempotency_key: str | None,
        fault_hook: Callable[[str], None] | None = None,
    ) -> TaskRepairActionResult:
        key_digest = _idempotency_key_digest(idempotency_key)
        request_digest = _request_digest(unit_id)
        task_id = self._task_id_for_unit(unit_id)
        lock = _repair_action_lock(actor, key_digest)
        async with lock:
            receipt, replayed = self._record_pending(
                task_id=task_id,
                actor=actor,
                key_digest=key_digest,
                request_digest=request_digest,
            )
            completed = _completed_receipt(receipt, idempotency_replayed=True)
            if completed is not None:
                return completed
            try:
                if replayed:
                    recovered = self._recover_advanced(unit_id, task_id, receipt.id)
                    if recovered is not None:
                        self._finalize_success(receipt.id, recovered)
                        return recovered
                execution = await self._coordinator.execute(unit_id)
                result = TaskRepairActionResult(
                    action="execute",
                    task_id=execution.task_id,
                    task_unit_id=execution.task_unit_id,
                    status=execution.status,
                    task_version=execution.task_version,
                    execution_plan_id=execution.execution_plan_id,
                    operation_replayed=execution.replayed,
                    receipt_id=receipt.id,
                    idempotency_replayed=replayed,
                )
                if fault_hook is not None:
                    fault_hook("after_repair_execution_prepared")
                self._finalize_success(receipt.id, result)
                return result
            except ApplicationError as exc:
                self._finalize_failure(receipt.id, exc)
                raise
            except DomainViolation as exc:
                error = ApplicationError(
                    code=exc.code.value,
                    status=409,
                    title="修复动作安全检查失败",
                    detail=str(exc),
                )
                self._finalize_failure(receipt.id, error)
                raise error from exc

    def _task_id_for_unit(self, unit_id: str) -> str:
        with self._session_factory() as session:
            unit = TaskUnitRepository(session).get(unit_id)
            if unit is None:
                raise ApplicationError(
                    code="REPAIR_ACTION_UNIT_NOT_FOUND",
                    status=404,
                    title="修复处理单元不存在",
                    detail="指定 task unit 不存在或已失效",
                )
            if TaskRepository(session).get(unit.task_id) is None:
                raise ApplicationError(
                    code="TASK_NOT_FOUND",
                    status=404,
                    title="任务不存在",
                    detail="repair unit 关联任务不存在或已删除",
                )
            return unit.task_id

    def _recover_advanced(
        self,
        unit_id: str,
        task_id: str,
        receipt_id: str,
    ) -> TaskRepairActionResult | None:
        with self._session_factory() as session:
            plan = TaskExecutionPlanRepository(session).latest(unit_id)
            task = TaskRepository(session).get(task_id)
            if plan is None or task is None or plan.task_id != task_id:
                return None
            checkpoint = deepcopy(task.checkpoint)
            if checkpoint.get("repair_schema_version") != REPAIR_DOWNLOAD_CHECKPOINT_SCHEMA_VERSION:
                return None
            if (
                checkpoint.get("execution_plan_id") != plan.id
                or checkpoint.get("execution_plan_digest") != plan.plan_digest
                or checkpoint.get("stage") != task.status
            ):
                raise _repair_action_recovery_unproven(
                    "repair checkpoint 已不能与 latest execution plan/current task 状态绑定"
                )
            try:
                status = TaskStatus(task.status)
            except ValueError as exc:
                raise _repair_action_recovery_unproven("任务保存了未知状态") from exc
            stage = checkpoint.get("repair_stage")
            allowed_stages: dict[TaskStatus, frozenset[str]] = {
                TaskStatus.CLIENT_VERIFYING: frozenset(
                    {
                        REPAIR_STAGE_DOWNLOAD_PENDING,
                        REPAIR_STAGE_DOWNLOADING,
                        REPAIR_STAGE_RECHECK_PENDING,
                        REPAIR_STAGE_RECHECKING,
                    }
                ),
                TaskStatus.RETRY: frozenset({REPAIR_STAGE_INCOMPLETE}),
                TaskStatus.SEEDING: frozenset({REPAIR_STAGE_VERIFIED}),
                TaskStatus.DONE: frozenset({REPAIR_STAGE_VERIFIED}),
            }
            if status not in allowed_stages or stage not in allowed_stages[status]:
                return None
            _required_digest(checkpoint, "repair_candidate_key")
            _required_digest(checkpoint, "repair_evidence_digest")
            _required_text(checkpoint, "repair_source_verification_journal_id")
            _required_string_list(checkpoint, "repair_isolation_journal_ids")
            _required_int_list(checkpoint, "repair_affected_piece_indexes")
            _required_nonnegative_int(checkpoint, "repair_affected_file_count")
            return TaskRepairActionResult(
                action="execute",
                task_id=task.id,
                task_unit_id=unit_id,
                status=status,
                task_version=task.version,
                execution_plan_id=plan.id,
                operation_replayed=True,
                receipt_id=receipt_id,
                idempotency_replayed=True,
            )

    def _record_pending(
        self,
        *,
        task_id: str,
        actor: TaskActionActor,
        key_digest: str,
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
                        action="repair_execute",
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
                    event_type="REPAIR_EXECUTE_REQUESTED",
                    reason=(
                        f"{actor.kind} 已提交 repair execute 请求；"
                        "服务端将重新证明 repair plan 与所有权后再执行"
                    ),
                )
            session.commit()
            return _receipt_view(receipt), not created

    def _finalize_success(self, receipt_id: str, result: TaskRepairActionResult) -> None:
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
) -> TaskRepairActionResult | None:
    if receipt.state == "SUCCEEDED":
        if receipt.response_payload is None:
            raise RuntimeError("SUCCEEDED repair action receipt 缺少 response payload")
        return replace(
            _result_from_payload(receipt.id, receipt.response_payload),
            idempotency_replayed=idempotency_replayed,
        )
    if receipt.state == "FAILED":
        if receipt.error_payload is None:
            raise RuntimeError("FAILED repair action receipt 缺少 error payload")
        raise _error_from_payload(receipt.error_payload)
    if receipt.state != "PENDING":
        raise RuntimeError("repair action receipt 保存了未知状态")
    return None


def _result_payload(result: TaskRepairActionResult) -> dict[str, Any]:
    return {
        "action": result.action,
        "execution_plan_id": result.execution_plan_id,
        "operation_replayed": result.operation_replayed,
        "status": result.status.value,
        "task_id": result.task_id,
        "task_unit_id": result.task_unit_id,
        "task_version": result.task_version,
    }


def _result_from_payload(receipt_id: str, payload: dict[str, Any]) -> TaskRepairActionResult:
    if payload.get("action") != "execute":
        raise RuntimeError("repair action receipt response action 无效")
    task_id = payload.get("task_id")
    unit_id = payload.get("task_unit_id")
    task_version = payload.get("task_version")
    execution_plan_id = payload.get("execution_plan_id")
    status = payload.get("status")
    operation_replayed = payload.get("operation_replayed")
    if (
        not isinstance(task_id, str)
        or not isinstance(unit_id, str)
        or not isinstance(task_version, int)
        or isinstance(task_version, bool)
        or not isinstance(execution_plan_id, str)
        or not isinstance(status, str)
        or not isinstance(operation_replayed, bool)
    ):
        raise RuntimeError("repair action receipt response payload 无效")
    return TaskRepairActionResult(
        action="execute",
        task_id=task_id,
        task_unit_id=unit_id,
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
        raise RuntimeError("repair action receipt error payload 无效")
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
            detail="repair execute 请求必须携带 Idempotency-Key",
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


def _request_digest(unit_id: str) -> str:
    encoded = json.dumps(
        {
            "action": "repair_execute",
            "schema_version": REPAIR_ACTION_SCHEMA_VERSION,
            "task_unit_id": unit_id,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _repair_action_lock(actor: TaskActionActor, key_digest: str) -> asyncio.Lock:
    lock_key = f"{actor.kind}:{actor.subject_id}:{key_digest}"
    lock = _REPAIR_ACTION_LOCKS.get(lock_key)
    if lock is None:
        lock = asyncio.Lock()
        _REPAIR_ACTION_LOCKS[lock_key] = lock
    return lock


def _required_text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise _repair_action_recovery_unproven(f"repair checkpoint 缺少有效 {key}")
    return value


def _required_digest(payload: dict[str, Any], key: str) -> str:
    value = _required_text(payload, key)
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise _repair_action_recovery_unproven(f"repair checkpoint 包含无效 {key}")
    return value


def _required_string_list(payload: dict[str, Any], key: str) -> tuple[str, ...]:
    value = payload.get(key)
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise _repair_action_recovery_unproven(f"repair checkpoint 包含无效 {key}")
    return tuple(value)


def _required_int_list(payload: dict[str, Any], key: str) -> tuple[int, ...]:
    value = payload.get(key)
    if not isinstance(value, list) or any(
        not isinstance(item, int) or isinstance(item, bool) or item < 0 for item in value
    ):
        raise _repair_action_recovery_unproven(f"repair checkpoint 包含无效 {key}")
    return tuple(value)


def _required_nonnegative_int(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise _repair_action_recovery_unproven(f"repair checkpoint 包含无效 {key}")
    return value


def _repair_action_recovery_unproven(detail: str) -> ApplicationError:
    return ApplicationError(
        code="REPAIR_ACTION_RECOVERY_UNPROVEN",
        status=409,
        title="无法证明 repair execute 已安全推进",
        detail=detail,
    )


def _invalid_idempotency_key() -> ApplicationError:
    return ApplicationError(
        code="IDEMPOTENCY_KEY_INVALID",
        status=422,
        title="幂等键无效",
        detail="Idempotency-Key 必须是 1..200 个不含空白/控制字符的可见 ASCII 字符",
    )
