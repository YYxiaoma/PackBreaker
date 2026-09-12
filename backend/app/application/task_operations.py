from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any, Literal, Protocol
from weakref import WeakValueDictionary

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
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
from backend.app.application.repair_downloader_operations import (
    QBITTORRENT_REPAIR_START_OPERATION,
    QBITTORRENT_REPAIR_STOP_OPERATION,
    REPAIR_DOWNLOADER_RECONCILABLE_OPERATIONS,
    TRANSMISSION_REPAIR_START_OPERATION,
    TRANSMISSION_REPAIR_STOP_OPERATION,
    RepairDownloadJournalReconcileResult,
)
from backend.app.application.task_actions import TaskActionActor
from backend.app.application.transmission_operations import (
    TRANSMISSION_RECONCILABLE_OPERATIONS,
    TransmissionJournalReconcileResult,
    TransmissionWriteBindingPort,
)
from backend.app.domain.errors import DomainViolation, ErrorCode
from backend.app.domain.operation import OperationKind, OperationStatus, operation_kind
from backend.app.domain.task_state import TERMINAL_STATUSES, TaskStatus
from backend.app.infrastructure.persistence.models import (
    OperationJournal,
    OperationJournalTombstone,
    TaskActionReceipt,
)
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
_BASE_RECONCILABLE_OPERATION_TYPES = (
    _FILESYSTEM_RECONCILE_TYPES
    | QBITTORRENT_RECONCILABLE_OPERATIONS
    | TRANSMISSION_RECONCILABLE_OPERATIONS
)
_ACTION_LOCKS: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
_PURGE_LOCKS: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()


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


OperationRepairAction = Literal["RECONCILE", "MANUAL_INSPECTION"]
OperationRepairReason = Literal[
    "SAFE_RECONCILE_AVAILABLE",
    "MANUAL_RECONCILE_REQUIRED",
    "ROLLBACK_BLOCKED",
]
OperationCleanupReason = Literal["NO_SIDE_EFFECT", "ROLLBACK_CONFIRMED"]
OperationRetentionReason = Literal[
    "ELIGIBLE",
    "RETENTION_WINDOW_NOT_REACHED",
    "TASK_NOT_TERMINAL",
    "TASK_CHECKPOINT_REFERENCE",
    "ACTION_RECEIPT_REFERENCE",
    "JOURNAL_REFERENCE",
]


@dataclass(frozen=True, slots=True)
class OperationRepairItemView:
    journal_id: str
    task_id: str
    kind: OperationKind
    status: OperationStatus
    reason_code: OperationRepairReason
    reason: str
    recommended_action: str
    action: OperationRepairAction
    reconcile_supported: bool
    manual_required: bool
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class OperationCleanupCandidateView:
    journal_id: str
    task_id: str
    kind: OperationKind
    status: OperationStatus
    reason_code: OperationCleanupReason
    reason: str
    recommendation: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class OperationMaintenanceSummary:
    total_journals: int
    attention_required: int
    reconcile_supported: int
    manual_only: int
    retention_candidates: int
    truncated: bool


@dataclass(frozen=True, slots=True)
class OperationMaintenanceReport:
    generated_at: datetime
    summary: OperationMaintenanceSummary
    repair_items: tuple[OperationRepairItemView, ...]
    cleanup_candidates: tuple[OperationCleanupCandidateView, ...]


@dataclass(frozen=True, slots=True)
class OperationRetentionItemView:
    journal_id: str
    task_id: str
    kind: OperationKind
    status: OperationStatus
    eligible: bool
    reason_code: OperationRetentionReason
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class OperationRetentionSummary:
    candidates: int
    inspected: int
    eligible: int
    blocked: int
    truncated: bool


@dataclass(frozen=True, slots=True)
class OperationRetentionPlan:
    generated_at: datetime
    cutoff: datetime
    retention_days: int
    summary: OperationRetentionSummary
    items: tuple[OperationRetentionItemView, ...]


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
class TaskOperationPurgeResult:
    action: Literal["purge"]
    task_id: str
    journal_id: str
    kind: OperationKind
    final_status: OperationStatus
    purged: bool
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


class RepairDownloadJournalReconcilePort(Protocol):
    async def reconcile_qbittorrent(
        self,
        journal_id: str,
        binding: QbittorrentWriteBindingPort,
        *,
        allow_applied_replay: bool = False,
    ) -> RepairDownloadJournalReconcileResult: ...

    async def reconcile_transmission(
        self,
        journal_id: str,
        binding: TransmissionWriteBindingPort,
        *,
        allow_applied_replay: bool = False,
    ) -> RepairDownloadJournalReconcileResult: ...


class TaskOperationService:
    """公开脱敏 journal 摘要，并只通过既有完成证据重新证明文件/下载器状态。"""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        filesystem_operations: FilesystemOperationService,
        downloader_bindings: DownloaderBindingProvider,
        qbit_reconcile: QbittorrentJournalReconcilePort,
        transmission_reconcile: TransmissionJournalReconcilePort,
        repair_reconcile: RepairDownloadJournalReconcilePort | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._filesystem_operations = filesystem_operations
        self._downloader_bindings = downloader_bindings
        self._qbit_reconcile = qbit_reconcile
        self._transmission_reconcile = transmission_reconcile
        self._repair_reconcile = repair_reconcile
        self._reconcilable_operation_types = _BASE_RECONCILABLE_OPERATION_TYPES | (
            REPAIR_DOWNLOADER_RECONCILABLE_OPERATIONS
            if repair_reconcile is not None
            else frozenset()
        )

    def list_operations(self, task_id: str) -> tuple[TaskOperationView, ...]:
        with self._session_factory() as session:
            if TaskRepository(session).get(task_id) is None:
                raise _task_not_found()
            journals = OperationJournalRepository(session).list_for_task(task_id)
            return tuple(
                _operation_view(item, self._reconcilable_operation_types)
                for item in reversed(journals)
            )

    def maintenance_report(self, *, limit: int = 100) -> OperationMaintenanceReport:
        if limit <= 0 or limit > 500:
            raise ValueError("limit 必须位于 1..500")
        attention_statuses = (
            OperationStatus.RECONCILE_REQUIRED,
            OperationStatus.ROLLBACK_BLOCKED,
        )
        retention_statuses = (OperationStatus.NOOP, OperationStatus.ROLLED_BACK)
        with self._session_factory() as session:
            repository = OperationJournalRepository(session)
            total_journals = repository.count_all()
            attention_required = repository.count_by_statuses(attention_statuses)
            retention_candidates = repository.count_by_statuses(retention_statuses)
            reconcile_supported = repository.count_reconcile_supported(
                tuple(sorted(self._reconcilable_operation_types))
            )
            repair_journals = repository.list_by_statuses(attention_statuses, limit=limit)
            cleanup_journals = repository.list_by_statuses(retention_statuses, limit=limit)

        repair_items = tuple(
            _repair_item(item, self._reconcilable_operation_types) for item in repair_journals
        )
        cleanup_candidates = tuple(_cleanup_candidate(item) for item in cleanup_journals)
        return OperationMaintenanceReport(
            generated_at=datetime.now(UTC),
            summary=OperationMaintenanceSummary(
                total_journals=total_journals,
                attention_required=attention_required,
                reconcile_supported=reconcile_supported,
                manual_only=attention_required - reconcile_supported,
                retention_candidates=retention_candidates,
                truncated=(
                    attention_required > len(repair_items)
                    or retention_candidates > len(cleanup_candidates)
                ),
            ),
            repair_items=repair_items,
            cleanup_candidates=cleanup_candidates,
        )

    def retention_plan(
        self,
        *,
        retention_days: int = 30,
        limit: int = 100,
        now: datetime | None = None,
    ) -> OperationRetentionPlan:
        if retention_days <= 0 or retention_days > 3650:
            raise ValueError("retention_days 必须位于 1..3650")
        if limit <= 0 or limit > 500:
            raise ValueError("limit 必须位于 1..500")
        generated_at = now or datetime.now(UTC)
        cutoff = generated_at - timedelta(days=retention_days)
        retention_statuses = (OperationStatus.NOOP, OperationStatus.ROLLED_BACK)
        with self._session_factory() as session:
            repository = OperationJournalRepository(session)
            candidates = repository.count_by_statuses(retention_statuses)
            journals = repository.list_by_statuses(retention_statuses, limit=limit)
            items = tuple(_retention_item(session, journal, cutoff=cutoff) for journal in journals)
        eligible = sum(item.eligible for item in items)
        return OperationRetentionPlan(
            generated_at=generated_at,
            cutoff=cutoff,
            retention_days=retention_days,
            summary=OperationRetentionSummary(
                candidates=candidates,
                inspected=len(items),
                eligible=eligible,
                blocked=len(items) - eligible,
                truncated=candidates > len(items),
            ),
            items=items,
        )

    async def purge_retained(
        self,
        *,
        task_id: str,
        journal_id: str,
        actor: TaskActionActor,
        idempotency_key: str | None,
        retention_days: int = 30,
        now: datetime | None = None,
        fault_hook: Callable[[str], None] | None = None,
    ) -> TaskOperationPurgeResult:
        if retention_days <= 0 or retention_days > 3650:
            raise ApplicationError(
                code="OPERATION_RETENTION_DAYS_INVALID",
                status=422,
                title="operation journal 保留期无效",
                detail="retention_days 必须位于 1..3650",
            )
        key_digest = _idempotency_key_digest(idempotency_key)
        request_digest = _purge_request_digest(
            task_id=task_id,
            journal_id=journal_id,
            retention_days=retention_days,
        )
        action_lock = _action_lock(actor, key_digest)
        purge_lock = _purge_lock(journal_id)
        async with action_lock, purge_lock:
            receipt, replayed = self._record_purge_pending(
                task_id=task_id,
                actor=actor,
                key_digest=key_digest,
                request_digest=request_digest,
            )
            completed = _completed_purge_receipt(receipt, idempotency_replayed=True)
            if completed is not None:
                return completed
            try:
                result = self._purge_with_receipt(
                    task_id=task_id,
                    journal_id=journal_id,
                    receipt_id=receipt.id,
                    retention_days=retention_days,
                    now=now or datetime.now(UTC),
                    idempotency_replayed=replayed,
                )
            except ApplicationError as exc:
                self._finalize_failure(receipt.id, exc)
                raise
            except DomainViolation as exc:
                error = ApplicationError(
                    code="OPERATION_RETENTION_CONFLICT",
                    status=409,
                    title="operation journal 保留期清理并发冲突",
                    detail="journal 或 tombstone 状态已变化；未强制删除，请重新读取保留期计划",
                )
                self._finalize_failure(receipt.id, error)
                raise error from exc
            if fault_hook is not None:
                fault_hook("after_operation_retention_committed")
            return result

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
                elif current.operation_type in {
                    QBITTORRENT_REPAIR_START_OPERATION,
                    QBITTORRENT_REPAIR_STOP_OPERATION,
                }:
                    if current.after_snapshot is None or self._repair_reconcile is None:
                        raise ApplicationError(
                            code="OPERATION_RECONCILE_UNPROVABLE",
                            status=409,
                            title="repair operation 缺少安全对账能力或完成后快照",
                            detail=(
                                "未知 repair start/stop 结果禁止根据当前下载器状态反推历史副作用"
                            ),
                        )
                    qbit_binding = self._downloader_bindings.qbittorrent_write_binding(
                        _required_downloader_id(current)
                    )
                    reconciled_repair = await self._repair_reconcile.reconcile_qbittorrent(
                        journal_id,
                        qbit_binding,
                        allow_applied_replay=replayed,
                    )
                    result_status = reconciled_repair.status
                    operation_replayed = reconciled_repair.replayed
                elif current.operation_type in {
                    TRANSMISSION_REPAIR_START_OPERATION,
                    TRANSMISSION_REPAIR_STOP_OPERATION,
                }:
                    if current.after_snapshot is None or self._repair_reconcile is None:
                        raise ApplicationError(
                            code="OPERATION_RECONCILE_UNPROVABLE",
                            status=409,
                            title="repair operation 缺少安全对账能力或完成后快照",
                            detail=(
                                "未知 repair start/stop 结果禁止根据当前下载器状态反推历史副作用"
                            ),
                        )
                    transmission_binding = self._downloader_bindings.transmission_write_binding(
                        _required_downloader_id(current)
                    )
                    reconciled_repair = await self._repair_reconcile.reconcile_transmission(
                        journal_id,
                        transmission_binding,
                        allow_applied_replay=replayed,
                    )
                    result_status = reconciled_repair.status
                    operation_replayed = reconciled_repair.replayed
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

    def _record_purge_pending(
        self,
        *,
        task_id: str,
        actor: TaskActionActor,
        key_digest: str,
        request_digest: str,
    ) -> tuple[_ReceiptView, bool]:
        with self._session_factory() as session:
            task_repository = TaskRepository(session)
            if task_repository.get(task_id) is None:
                raise _task_not_found()
            repository = TaskActionReceiptRepository(session)
            try:
                receipt, created = repository.record_pending(
                    TaskActionReceiptCreate(
                        task_id=task_id,
                        actor_kind=actor.kind,
                        actor_id=actor.subject_id,
                        idempotency_key_digest=key_digest,
                        action="purge_operation_journal",
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
                task_repository.append_event(
                    task_id=task_id,
                    event_type="OPERATION_RETENTION_REQUESTED",
                    reason=(
                        f"{actor.kind} 已请求清理超过保留期且无恢复依赖的 operation journal；"
                        "具体 journal 仅保存在幂等请求摘要中"
                    ),
                )
            session.commit()
            return _receipt_view(receipt), not created

    def _purge_with_receipt(
        self,
        *,
        task_id: str,
        journal_id: str,
        receipt_id: str,
        retention_days: int,
        now: datetime,
        idempotency_replayed: bool,
    ) -> TaskOperationPurgeResult:
        cutoff = now - timedelta(days=retention_days)
        with self._session_factory() as session:
            receipt = session.get(TaskActionReceipt, receipt_id)
            if receipt is None or receipt.task_id != task_id or receipt.state != "PENDING":
                raise ApplicationError(
                    code="OPERATION_RETENTION_RECEIPT_INVALID",
                    status=409,
                    title="保留期清理 receipt 状态无效",
                    detail="清理前必须存在同任务的 PENDING action receipt",
                )
            journal_repository = OperationJournalRepository(session)
            journal = journal_repository.get(journal_id)
            if journal is None:
                tombstone = journal_repository.get_tombstone(journal_id)
                if tombstone is None or tombstone.task_id != task_id:
                    raise ApplicationError(
                        code="OPERATION_NOT_FOUND",
                        status=404,
                        title="operation journal 不存在",
                        detail="指定 journal 不存在或不属于当前任务",
                    )
                result = TaskOperationPurgeResult(
                    action="purge",
                    task_id=task_id,
                    journal_id=tombstone.journal_id,
                    kind=operation_kind(tombstone.operation_type),
                    final_status=OperationStatus(tombstone.final_status),
                    purged=True,
                    receipt_id=receipt_id,
                    idempotency_replayed=idempotency_replayed,
                )
                TaskActionReceiptRepository(session).finalize_success(
                    receipt_id,
                    _purge_result_payload(result),
                )
                TaskRepository(session).append_event(
                    task_id=task_id,
                    event_type="OPERATION_RETENTION_CONFIRMED",
                    reason="已存在匹配 tombstone；补记同一保留期清理 receipt 成功",
                )
                session.commit()
                return result
            if journal.task_id != task_id:
                raise ApplicationError(
                    code="OPERATION_NOT_FOUND",
                    status=404,
                    title="operation journal 不存在",
                    detail="指定 journal 不存在或不属于当前任务",
                )
            status = OperationStatus(journal.status)
            if status not in {OperationStatus.NOOP, OperationStatus.ROLLED_BACK}:
                raise ApplicationError(
                    code="OPERATION_RETENTION_STATE_INVALID",
                    status=409,
                    title="operation journal 不能进入保留期清理",
                    detail="只有 NOOP 或 ROLLED_BACK journal 才允许删除 payload",
                )
            reason = _retention_reason(
                session,
                journal,
                cutoff=cutoff,
                current_receipt_id=receipt_id,
            )
            if reason != "ELIGIBLE":
                raise _retention_blocked(reason)

            tombstone = OperationJournalTombstone(
                journal_id=journal.id,
                task_id=journal.task_id,
                idempotency_key_digest=_operation_idempotency_key_digest(journal.idempotency_key),
                operation_type=journal.operation_type,
                final_status=journal.status,
                journal_digest=_journal_digest(journal),
                original_created_at=journal.created_at,
                original_updated_at=journal.updated_at,
                purged_at=now,
            )
            session.add(tombstone)
            try:
                session.flush()
            except IntegrityError as exc:
                raise DomainViolation(
                    ErrorCode.IDEMPOTENCY_CONFLICT,
                    "operation retention tombstone 已并发存在",
                ) from exc
            kind = operation_kind(journal.operation_type)
            session.delete(journal)
            session.flush()
            result = TaskOperationPurgeResult(
                action="purge",
                task_id=task_id,
                journal_id=journal_id,
                kind=kind,
                final_status=status,
                purged=True,
                receipt_id=receipt_id,
                idempotency_replayed=idempotency_replayed,
            )
            TaskActionReceiptRepository(session).finalize_success(
                receipt_id,
                _purge_result_payload(result),
            )
            TaskRepository(session).append_event(
                task_id=task_id,
                event_type="OPERATION_RETENTION_PURGED",
                reason=(
                    f"{kind.value} 的 {status.value} journal 已超过保留期且无恢复引用；"
                    "敏感 operation payload 已删除，最小 tombstone 保留幂等与审计证据"
                ),
            )
            session.commit()
            return result

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


def _operation_view(
    journal: OperationJournal,
    reconcilable_operation_types: frozenset[str],
) -> TaskOperationView:
    status = OperationStatus(journal.status)
    return TaskOperationView(
        id=journal.id,
        task_id=journal.task_id,
        kind=operation_kind(journal.operation_type),
        status=status,
        attention_required=status in _ATTENTION_STATUSES,
        reconcile_supported=_reconcile_supported(journal, reconcilable_operation_types),
        created_at=journal.created_at,
        updated_at=journal.updated_at,
    )


def _reconcile_supported(
    journal: OperationJournal,
    reconcilable_operation_types: frozenset[str],
) -> bool:
    return (
        OperationStatus(journal.status) is OperationStatus.RECONCILE_REQUIRED
        and journal.operation_type in reconcilable_operation_types
        and journal.after_snapshot is not None
    )


def _repair_item(
    journal: OperationJournal,
    reconcilable_operation_types: frozenset[str],
) -> OperationRepairItemView:
    status = OperationStatus(journal.status)
    supported = _reconcile_supported(journal, reconcilable_operation_types)
    if status is OperationStatus.ROLLBACK_BLOCKED:
        reason_code: OperationRepairReason = "ROLLBACK_BLOCKED"
        reason = "回滚因所有权、完成快照或当前资源状态证据不足而被安全门阻断。"
        recommendation = (
            "保留现场并人工核对资源归属；确认前不要删除资源、重放副作用或强制改写 journal 状态。"
        )
        action: OperationRepairAction = "MANUAL_INSPECTION"
    elif supported:
        reason_code = "SAFE_RECONCILE_AVAILABLE"
        reason = "journal 已要求重新验证，且存在只读取当前资源状态的安全对账器。"
        recommendation = "优先执行只读 reconcile；若重新证明失败，继续保留现场并转人工检查。"
        action = "RECONCILE"
    else:
        reason_code = "MANUAL_RECONCILE_REQUIRED"
        reason = "缺少可安全自动证明的完成证据，或该操作类型没有自动对账器。"
        recommendation = "人工核对外部资源与历史记录；不要根据当前资源存在或缺失反推历史副作用。"
        action = "MANUAL_INSPECTION"
    return OperationRepairItemView(
        journal_id=journal.id,
        task_id=journal.task_id,
        kind=operation_kind(journal.operation_type),
        status=status,
        reason_code=reason_code,
        reason=reason,
        recommended_action=recommendation,
        action=action,
        reconcile_supported=supported,
        manual_required=not supported,
        created_at=journal.created_at,
        updated_at=journal.updated_at,
    )


def _cleanup_candidate(journal: OperationJournal) -> OperationCleanupCandidateView:
    status = OperationStatus(journal.status)
    if status is OperationStatus.NOOP:
        reason_code: OperationCleanupReason = "NO_SIDE_EFFECT"
        reason = "journal 已确认没有执行外部副作用，因此没有仍由该 journal 创建并需要保留的资源。"
    elif status is OperationStatus.ROLLED_BACK:
        reason_code = "ROLLBACK_CONFIRMED"
        reason = "journal-owned 副作用已确认完成回滚，资源安全约束不再阻止未来保留期清理。"
    else:
        raise ValueError("只有 NOOP 或 ROLLED_BACK journal 可以进入保留期清理候选")
    return OperationCleanupCandidateView(
        journal_id=journal.id,
        task_id=journal.task_id,
        kind=operation_kind(journal.operation_type),
        status=status,
        reason_code=reason_code,
        reason=reason,
        recommendation=(
            "先通过 retention-plan 重新证明任务终态、保留期与零恢复引用；"
            "只有证明通过后才可使用带 Idempotency-Key 的 purge 动作删除 payload 并保留 tombstone。"
        ),
        created_at=journal.created_at,
        updated_at=journal.updated_at,
    )


def _retention_item(
    session: Session,
    journal: OperationJournal,
    *,
    cutoff: datetime,
) -> OperationRetentionItemView:
    reason = _retention_reason(session, journal, cutoff=cutoff)
    return OperationRetentionItemView(
        journal_id=journal.id,
        task_id=journal.task_id,
        kind=operation_kind(journal.operation_type),
        status=OperationStatus(journal.status),
        eligible=reason == "ELIGIBLE",
        reason_code=reason,
        updated_at=journal.updated_at,
    )


def _retention_reason(
    session: Session,
    journal: OperationJournal,
    *,
    cutoff: datetime,
    current_receipt_id: str | None = None,
) -> OperationRetentionReason:
    task = TaskRepository(session).get(journal.task_id)
    if task is None:
        return "TASK_NOT_TERMINAL"
    try:
        task_status = TaskStatus(task.status)
    except ValueError:
        return "TASK_NOT_TERMINAL"
    if task_status not in TERMINAL_STATUSES:
        return "TASK_NOT_TERMINAL"
    if journal.updated_at > cutoff or task.updated_at > cutoff:
        return "RETENTION_WINDOW_NOT_REACHED"
    if _contains_exact_reference(task.checkpoint, journal.id):
        return "TASK_CHECKPOINT_REFERENCE"

    receipts = session.scalars(
        select(TaskActionReceipt).where(TaskActionReceipt.task_id == journal.task_id)
    )
    for receipt in receipts:
        if receipt.id == current_receipt_id:
            continue
        response_references_journal = _contains_exact_reference(
            receipt.response_payload,
            journal.id,
        )
        error_references_journal = _contains_exact_reference(
            receipt.error_payload,
            journal.id,
        )
        if response_references_journal or error_references_journal:
            return "ACTION_RECEIPT_REFERENCE"

    for other in OperationJournalRepository(session).list_for_task(journal.task_id):
        if other.id == journal.id:
            continue
        if any(
            _contains_exact_reference(payload, journal.id)
            for payload in (
                other.target,
                other.intent,
                other.before_snapshot,
                other.after_snapshot,
            )
        ):
            return "JOURNAL_REFERENCE"
    return "ELIGIBLE"


def _contains_exact_reference(value: object, needle: str) -> bool:
    if isinstance(value, str):
        return value == needle
    if isinstance(value, dict):
        return any(
            _contains_exact_reference(key, needle) or _contains_exact_reference(item, needle)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_exact_reference(item, needle) for item in value)
    return False


def _retention_blocked(reason: OperationRetentionReason) -> ApplicationError:
    details: dict[OperationRetentionReason, tuple[str, str]] = {
        "ELIGIBLE": ("清理资格异常", "已满足清理条件的 journal 不应进入阻断分支"),
        "RETENTION_WINDOW_NOT_REACHED": (
            "operation journal 尚未达到保留期",
            "journal 或所属终态任务的最近更新时间仍晚于本次保留期截止时间",
        ),
        "TASK_NOT_TERMINAL": (
            "任务尚未终态",
            "只有 DONE、FAILED 或 CANCELLED 任务的 journal 才允许进入保留期清理",
        ),
        "TASK_CHECKPOINT_REFERENCE": (
            "任务 checkpoint 仍引用 journal",
            "当前终态 checkpoint 仍依赖该 journal ID，禁止删除恢复证据",
        ),
        "ACTION_RECEIPT_REFERENCE": (
            "任务 action receipt 仍引用 journal",
            "已有幂等动作结果仍依赖该 journal ID，禁止删除审计证据",
        ),
        "JOURNAL_REFERENCE": (
            "其他 operation journal 仍引用候选 journal",
            "同任务其他 journal 的所有权或恢复证据仍依赖该 journal ID",
        ),
    }
    title, detail = details[reason]
    return ApplicationError(
        code=f"OPERATION_RETENTION_{reason}",
        status=409,
        title=title,
        detail=detail,
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


def _completed_purge_receipt(
    receipt: _ReceiptView,
    *,
    idempotency_replayed: bool,
) -> TaskOperationPurgeResult | None:
    if receipt.state == "SUCCEEDED":
        if receipt.response_payload is None:
            raise RuntimeError("SUCCEEDED retention receipt 缺少 response payload")
        return replace(
            _purge_result_from_payload(receipt.id, receipt.response_payload),
            idempotency_replayed=idempotency_replayed,
        )
    if receipt.state == "FAILED":
        if receipt.error_payload is None:
            raise RuntimeError("FAILED retention receipt 缺少 error payload")
        raise _error_from_payload(receipt.error_payload)
    if receipt.state != "PENDING":
        raise RuntimeError("retention receipt 保存了未知状态")
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


def _purge_result_payload(result: TaskOperationPurgeResult) -> dict[str, Any]:
    return {
        "action": result.action,
        "final_status": result.final_status.value,
        "journal_id": result.journal_id,
        "kind": result.kind.value,
        "purged": result.purged,
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


def _purge_result_from_payload(
    receipt_id: str,
    payload: dict[str, Any],
) -> TaskOperationPurgeResult:
    if payload.get("action") != "purge":
        raise RuntimeError("retention receipt response action 无效")
    task_id = payload.get("task_id")
    journal_id = payload.get("journal_id")
    kind = payload.get("kind")
    final_status = payload.get("final_status")
    purged = payload.get("purged")
    if (
        not isinstance(task_id, str)
        or not isinstance(journal_id, str)
        or not isinstance(kind, str)
        or not isinstance(final_status, str)
        or not isinstance(purged, bool)
    ):
        raise RuntimeError("retention receipt response payload 无效")
    return TaskOperationPurgeResult(
        action="purge",
        task_id=task_id,
        journal_id=journal_id,
        kind=OperationKind(kind),
        final_status=OperationStatus(final_status),
        purged=purged,
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


def _purge_request_digest(*, task_id: str, journal_id: str, retention_days: int) -> str:
    encoded = json.dumps(
        {
            "action": "purge",
            "journal_id": journal_id,
            "retention_days": retention_days,
            "schema_version": _SCHEMA_VERSION,
            "task_id": task_id,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _operation_idempotency_key_digest(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _journal_digest(journal: OperationJournal) -> str:
    payload = {
        "after_snapshot": journal.after_snapshot,
        "before_snapshot": journal.before_snapshot,
        "created_at": journal.created_at.isoformat(),
        "final_status": journal.status,
        "idempotency_key_digest": _operation_idempotency_key_digest(journal.idempotency_key),
        "intent": journal.intent,
        "journal_id": journal.id,
        "operation_type": journal.operation_type,
        "target": journal.target,
        "task_id": journal.task_id,
        "updated_at": journal.updated_at.isoformat(),
    }
    encoded = json.dumps(
        payload,
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


def _purge_lock(journal_id: str) -> asyncio.Lock:
    lock = _PURGE_LOCKS.get(journal_id)
    if lock is None:
        lock = asyncio.Lock()
        _PURGE_LOCKS[journal_id] = lock
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
