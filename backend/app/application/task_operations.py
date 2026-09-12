from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from hashlib import sha256
from typing import Any, Literal, Protocol
from weakref import WeakValueDictionary

from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.downloader_operations import (
    QBITTORRENT_RECONCILABLE_OPERATIONS,
    QbittorrentJournalReconcileResult,
    QbittorrentWriteBindingPort,
)
from backend.app.application.errors import ApplicationError
from backend.app.application.filesystem_operations import (
    CREATE_DIRECTORY_OPERATION,
    CREATE_HARDLINK_OPERATION,
    FilesystemOperationService,
)
from backend.app.application.task_actions import TaskActionActor
from backend.app.application.transmission_operations import (
    TRANSMISSION_RECONCILABLE_OPERATIONS,
    TransmissionJournalReconcileResult,
    TransmissionWriteBindingPort,
)
from backend.app.domain.errors import DomainViolation, ErrorCode
from backend.app.domain.operation import OperationKind, OperationStatus, operation_kind
from backend.app.infrastructure.persistence.models import OperationJournal
from backend.app.infrastructure.persistence.repositories import (
    OperationJournalRepository,
    TaskActionReceiptCreate,
    TaskActionReceiptRepository,
    TaskRepository,
)

_SCHEMA_VERSION = "packbreaker-operation-action-v1"
_FILESYSTEM_RECONCILE_TYPES = frozenset({CREATE_DIRECTORY_OPERATION, CREATE_HARDLINK_OPERATION})
_ATTENTION_STATUSES = frozenset(
    {OperationStatus.RECONCILE_REQUIRED, OperationStatus.ROLLBACK_BLOCKED}
)
_ACTION_LOCKS: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()


@dataclass(frozen=True, slots=True)
class TaskOperationView:
    id: str
    task_id: str
    kind: OperationKind
    status: OperationStatus
    attention_required: bool
    reconcile_supported: bool
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class TaskOperationReconcileResult:
    action: Literal["reconcile"]
    task_id: str
    journal_id: str
    kind: OperationKind
    status: OperationStatus
    operation_replayed: bool
    receipt_id: str
    idempotency_replayed: bool


@dataclass(frozen=True, slots=True)
class _ReceiptView:
    id: str
    state: str
    response_payload: dict[str, Any] | None
    error_payload: dict[str, Any] | None


class DownloaderBindingProvider(Protocol):
    def qbittorrent_write_binding(self, downloader_id: str) -> QbittorrentWriteBindingPort: ...

    def transmission_write_binding(self, downloader_id: str) -> TransmissionWriteBindingPort: ...


class QbittorrentJournalReconcilePort(Protocol):
    async def reconcile(
        self,
        journal_id: str,
        binding: QbittorrentWriteBindingPort,
        *,
        allow_applied_replay: bool = False,
    ) -> QbittorrentJournalReconcileResult: ...


class TransmissionJournalReconcilePort(Protocol):
    async def reconcile(
        self,
        journal_id: str,
        binding: TransmissionWriteBindingPort,
        *,
        allow_applied_replay: bool = False,
    ) -> TransmissionJournalReconcileResult: ...


class TaskOperationService:
    """公开脱敏 journal 摘要，并只通过既有完成证据重新证明文件/下载器状态。"""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        filesystem_operations: FilesystemOperationService,
        downloader_bindings: DownloaderBindingProvider,
        qbit_reconcile: QbittorrentJournalReconcilePort,
        transmission_reconcile: TransmissionJournalReconcilePort,
    ) -> None:
        self._session_factory = session_factory
        self._filesystem_operations = filesystem_operations
        self._downloader_bindings = downloader_bindings
        self._qbit_reconcile = qbit_reconcile
        self._transmission_reconcile = transmission_reconcile

    def list_operations(self, task_id: str) -> tuple[TaskOperationView, ...]:
        with self._session_factory() as session:
            if TaskRepository(session).get(task_id) is None:
                raise _task_not_found()
            journals = OperationJournalRepository(session).list_for_task(task_id)
            return tuple(_operation_view(item) for item in reversed(journals))

    async def reconcile(
        self,
        *,
        task_id: str,
        journal_id: str,
        actor: TaskActionActor,
        idempotency_key: str | None,
        fault_hook: Callable[[str], None] | None = None,
    ) -> TaskOperationReconcileResult:
        key_digest = _idempotency_key_digest(idempotency_key)
        request_digest = _request_digest(task_id=task_id, journal_id=journal_id)
        lock = _action_lock(actor, key_digest)
        async with lock:
            self._require_operation(task_id, journal_id)
            receipt, replayed = self._record_pending(
                task_id=task_id,
                journal_id=journal_id,
                actor=actor,
                key_digest=key_digest,
                request_digest=request_digest,
            )
            completed = _completed_receipt(receipt, idempotency_replayed=True)
            if completed is not None:
                return completed

            try:
                current = self._require_operation(task_id, journal_id)
                current_status = OperationStatus(current.status)
                if not replayed and current_status is not OperationStatus.RECONCILE_REQUIRED:
                    raise ApplicationError(
                        code="OPERATION_RECONCILE_STATE_INVALID",
                        status=409,
                        title="operation journal 当前不需要重新验证",
                        detail="新的对账动作只接受 RECONCILE_REQUIRED 状态",
                    )

                if current.operation_type in _FILESYSTEM_RECONCILE_TYPES:
                    if current.after_snapshot is None:
                        raise ApplicationError(
                            code="OPERATION_RECONCILE_UNPROVABLE",
                            status=409,
                            title="缺少完成后快照，不能自动确认",
                            detail="没有 after snapshot 时禁止根据当前路径存在性猜测副作用是否完成",
                        )
                    reconciled = self._filesystem_operations.reconcile_journal(
                        journal_id,
                        allow_applied_replay=replayed,
                    )
                    result_status = reconciled.status
                    operation_replayed = reconciled.replayed
                elif current.operation_type in QBITTORRENT_RECONCILABLE_OPERATIONS:
                    if current.after_snapshot is None:
                        raise ApplicationError(
                            code="OPERATION_RECONCILE_UNPROVABLE",
                            status=409,
                            title="缺少完成后快照，不能自动确认",
                            detail="qBittorrent 未确认完成的未知结果不能根据当前状态反推历史所有权",
                        )
                    qbit_binding = self._downloader_bindings.qbittorrent_write_binding(
                        _required_downloader_id(current)
                    )
                    reconciled_qbit = await self._qbit_reconcile.reconcile(
                        journal_id,
                        qbit_binding,
                        allow_applied_replay=replayed,
                    )
                    result_status = reconciled_qbit.status
                    operation_replayed = reconciled_qbit.replayed
                elif current.operation_type in TRANSMISSION_RECONCILABLE_OPERATIONS:
                    if current.after_snapshot is None:
                        raise ApplicationError(
                            code="OPERATION_RECONCILE_UNPROVABLE",
                            status=409,
                            title="缺少完成后快照，不能自动确认",
                            detail=(
                                "Transmission 未确认完成的未知结果不能根据当前状态反推历史所有权"
                            ),
                        )
                    transmission_binding = self._downloader_bindings.transmission_write_binding(
                        _required_downloader_id(current)
                    )
                    reconciled_transmission = await self._transmission_reconcile.reconcile(
                        journal_id,
                        transmission_binding,
                        allow_applied_replay=replayed,
                    )
                    result_status = reconciled_transmission.status
                    operation_replayed = reconciled_transmission.replayed
                else:
                    raise ApplicationError(
                        code="OPERATION_RECONCILE_UNSUPPORTED",
                        status=409,
                        title="该操作尚不支持自动对账",
                        detail="该 operation journal 没有可用于只读重新证明的安全对账器",
                    )

                if fault_hook is not None:
                    fault_hook("after_operation_reconciled")
                summary = self._require_operation(task_id, journal_id)
                result = TaskOperationReconcileResult(
                    action="reconcile",
                    task_id=task_id,
                    journal_id=journal_id,
                    kind=operation_kind(summary.operation_type),
                    status=result_status,
                    operation_replayed=operation_replayed,
                    receipt_id=receipt.id,
                    idempotency_replayed=replayed,
                )
                self._finalize_success(receipt.id, result)
                return result
            except ApplicationError as exc:
                self._finalize_failure(receipt.id, exc)
                raise
            except DomainViolation as exc:
                error = _reconcile_domain_error(exc)
                self._finalize_failure(receipt.id, error)
                raise error from exc

    def _require_operation(self, task_id: str, journal_id: str) -> OperationJournal:
        with self._session_factory() as session:
            if TaskRepository(session).get(task_id) is None:
                raise _task_not_found()
            journal = OperationJournalRepository(session).get(journal_id)
            if journal is None or journal.task_id != task_id:
                raise ApplicationError(
                    code="OPERATION_NOT_FOUND",
                    status=404,
                    title="operation journal 不存在",
                    detail="指定 journal 不存在或不属于当前任务",
                )
            session.expunge(journal)
            return journal

    def _record_pending(
        self,
        *,
        task_id: str,
        journal_id: str,
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
                        action="reconcile_operation",
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
                    event_type="OPERATION_RECONCILE_REQUESTED",
                    reason=(
                        f"{actor.kind} 已请求重新验证 operation journal 证据；"
                        "journal ID 与幂等 receipt 已持久化"
                    ),
                )
            session.commit()
            return _receipt_view(receipt), not created

    def _finalize_success(
        self,
        receipt_id: str,
        result: TaskOperationReconcileResult,
    ) -> None:
        with self._session_factory() as session:
            TaskActionReceiptRepository(session).finalize_success(
                receipt_id,
                _result_payload(result),
            )
            TaskRepository(session).append_event(
                task_id=result.task_id,
                event_type="OPERATION_RECONCILE_CONFIRMED",
                reason=(
                    f"{result.kind.value} 已通过当前资源与已登记完成快照重新验证；"
                    "未重新执行文件系统或下载器副作用"
                ),
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


def _operation_view(journal: OperationJournal) -> TaskOperationView:
    status = OperationStatus(journal.status)
    return TaskOperationView(
        id=journal.id,
        task_id=journal.task_id,
        kind=operation_kind(journal.operation_type),
        status=status,
        attention_required=status in _ATTENTION_STATUSES,
        reconcile_supported=(
            status is OperationStatus.RECONCILE_REQUIRED
            and journal.operation_type
            in (
                _FILESYSTEM_RECONCILE_TYPES
                | QBITTORRENT_RECONCILABLE_OPERATIONS
                | TRANSMISSION_RECONCILABLE_OPERATIONS
            )
            and journal.after_snapshot is not None
        ),
        created_at=journal.created_at,
        updated_at=journal.updated_at,
    )


def _receipt_view(receipt: Any) -> _ReceiptView:
    return _ReceiptView(
        id=str(receipt.id),
        state=str(receipt.state),
        response_payload=(
            None if receipt.response_payload is None else dict(receipt.response_payload)
        ),
        error_payload=None if receipt.error_payload is None else dict(receipt.error_payload),
    )


def _completed_receipt(
    receipt: _ReceiptView,
    *,
    idempotency_replayed: bool,
) -> TaskOperationReconcileResult | None:
    if receipt.state == "SUCCEEDED":
        if receipt.response_payload is None:
            raise RuntimeError("SUCCEEDED operation action receipt 缺少 response payload")
        return replace(
            _result_from_payload(receipt.id, receipt.response_payload),
            idempotency_replayed=idempotency_replayed,
        )
    if receipt.state == "FAILED":
        if receipt.error_payload is None:
            raise RuntimeError("FAILED operation action receipt 缺少 error payload")
        raise _error_from_payload(receipt.error_payload)
    if receipt.state != "PENDING":
        raise RuntimeError("operation action receipt 保存了未知状态")
    return None


def _result_payload(result: TaskOperationReconcileResult) -> dict[str, Any]:
    return {
        "action": result.action,
        "journal_id": result.journal_id,
        "kind": result.kind.value,
        "operation_replayed": result.operation_replayed,
        "status": result.status.value,
        "task_id": result.task_id,
    }


def _result_from_payload(
    receipt_id: str,
    payload: dict[str, Any],
) -> TaskOperationReconcileResult:
    if payload.get("action") != "reconcile":
        raise RuntimeError("operation action receipt response action 无效")
    task_id = payload.get("task_id")
    journal_id = payload.get("journal_id")
    kind = payload.get("kind")
    status = payload.get("status")
    operation_replayed = payload.get("operation_replayed")
    if (
        not isinstance(task_id, str)
        or not isinstance(journal_id, str)
        or not isinstance(kind, str)
        or not isinstance(status, str)
        or not isinstance(operation_replayed, bool)
    ):
        raise RuntimeError("operation action receipt response payload 无效")
    return TaskOperationReconcileResult(
        action="reconcile",
        task_id=task_id,
        journal_id=journal_id,
        kind=OperationKind(kind),
        status=OperationStatus(status),
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
        raise RuntimeError("operation action receipt error payload 无效")
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
            detail="operation reconcile 请求必须携带 Idempotency-Key",
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


def _request_digest(*, task_id: str, journal_id: str) -> str:
    encoded = json.dumps(
        {
            "action": "reconcile",
            "journal_id": journal_id,
            "schema_version": _SCHEMA_VERSION,
            "task_id": task_id,
        },
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


def _reconcile_domain_error(exc: DomainViolation) -> ApplicationError:
    return ApplicationError(
        code="OPERATION_RECONCILE_BLOCKED",
        status=409,
        title="当前资源证据不能确认原操作结果",
        detail=(
            "当前资源无法与 operation journal 的完成后快照完全匹配；"
            "journal 状态保持安全阻断，不会强制改写"
        ),
    )


def _required_downloader_id(journal: OperationJournal) -> str:
    downloader_id = journal.target.get("downloader_id")
    if not isinstance(downloader_id, str) or not downloader_id:
        raise ApplicationError(
            code="OPERATION_RECONCILE_UNPROVABLE",
            status=409,
            title="下载器绑定证据无效",
            detail="operation journal 无法安全绑定到目标下载器，禁止自动对账",
        )
    return downloader_id


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
