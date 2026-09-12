from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from threading import Lock
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from backend.app.domain.errors import DomainViolation, ErrorCode
from backend.app.domain.idempotency import file_operation_key
from backend.app.domain.operation import OperationStatus
from backend.app.domain.verification import FileSnapshot
from backend.app.infrastructure.persistence.repositories import (
    OperationIntent,
    OperationJournalRepository,
)
from backend.app.infrastructure.safe_filesystem import (
    FilesystemSnapshot,
    RepairIsolationApplyResult,
    SafeFilesystemGateway,
)

FILESYSTEM_OPERATION_SCHEMA_VERSION = "packbreaker-filesystem-operation-v1"
REPAIR_ISOLATION_SCHEMA_VERSION = "packbreaker-repair-isolation-v1"
REPAIR_ISOLATION_PROGRESS_SCHEMA_VERSION = "packbreaker-repair-isolation-progress-v1"
REPAIR_CLEANUP_SCHEMA_VERSION = "packbreaker-repair-cleanup-v1"
CREATE_DIRECTORY_OPERATION = "CREATE_DIRECTORY"
CREATE_HARDLINK_OPERATION = "CREATE_HARDLINK"
ISOLATE_REPAIR_TARGET_OPERATION = "ISOLATE_REPAIR_TARGET"
CLEANUP_REPAIR_TARGET_OPERATION = "CLEANUP_REPAIR_TARGET"
_FILESYSTEM_OPERATION_LOCKS = tuple(Lock() for _ in range(64))


@dataclass(frozen=True, slots=True)
class HardlinkExecutionRequest:
    task_id: str
    candidate_key: str
    source_relative_path: str
    target_root_relative_path: str
    target_relative_path: str
    expected_source_snapshot: FileSnapshot


@dataclass(frozen=True, slots=True)
class HardlinkExecutionResult:
    directory_journal_ids: tuple[str, ...]
    hardlink_journal_id: str
    target_snapshot: FilesystemSnapshot
    replayed: bool


@dataclass(frozen=True, slots=True)
class RepairIsolationExecutionRequest:
    task_id: str
    hardlink_journal_id: str


@dataclass(frozen=True, slots=True)
class RepairIsolationExecutionResult:
    isolation_journal_id: str
    target_snapshot: FilesystemSnapshot
    replayed: bool
    recovered_after_unknown_result: bool


@dataclass(frozen=True, slots=True)
class RepairTargetCleanupRequest:
    task_id: str
    execution_plan_id: str
    isolation_journal_id: str
    remove_journal_id: str


@dataclass(frozen=True, slots=True)
class RepairTargetCleanupResult:
    cleanup_journal_id: str
    isolation_journal_id: str
    removed: bool
    replayed: bool
    recovered_after_unknown_result: bool


@dataclass(frozen=True, slots=True)
class RollbackResult:
    journal_id: str
    operation_type: str
    removed: bool
    status: OperationStatus


@dataclass(frozen=True, slots=True)
class FilesystemReconcileResult:
    journal_id: str
    operation_type: str
    status: OperationStatus
    replayed: bool


@dataclass(frozen=True, slots=True)
class _JournalView:
    id: str
    task_id: str
    idempotency_key: str
    operation_type: str
    target: dict[str, Any]
    intent: dict[str, Any]
    status: OperationStatus
    before_snapshot: dict[str, Any] | None
    after_snapshot: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class _PreparedRepairIsolation:
    operation_key: str
    task_id: str
    hardlink_journal_id: str
    source_relative_path: str
    target_root_relative_path: str
    target_relative_path: str
    expected_source_snapshot: FileSnapshot
    expected_target_snapshot: FilesystemSnapshot
    temporary_name: str


@dataclass(frozen=True, slots=True)
class _PreparedRepairCleanup:
    operation_key: str
    task_id: str
    execution_plan_id: str
    isolation_journal_id: str
    remove_journal_id: str
    hardlink_journal_id: str
    target_root_relative_path: str
    target_relative_path: str
    expected_isolation_snapshot: FilesystemSnapshot


class FilesystemOperationService:
    """把 operation journal 持久化提交点与单个文件系统原子动作串联起来。"""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        gateway: SafeFilesystemGateway,
    ) -> None:
        self._session_factory = session_factory
        self._gateway = gateway

    def execute_hardlink(
        self,
        request: HardlinkExecutionRequest,
        *,
        fault_hook: Callable[[str], None] | None = None,
    ) -> HardlinkExecutionResult:
        source_relative = self._gateway.normalize_relative_path(request.source_relative_path)
        target_root_relative = self._gateway.normalize_relative_path(
            request.target_root_relative_path,
            allow_root=True,
        )
        target_relative = self._gateway.normalize_relative_path(request.target_relative_path)
        target_parts = target_relative.split("/")
        directory_journal_ids = self._validate_existing_directory_journals(
            request,
            target_root_relative_path=target_root_relative,
            target_parts=target_parts,
        )
        hardlink_key = file_operation_key(
            candidate_key=request.candidate_key,
            operation_type=CREATE_HARDLINK_OPERATION,
            normalized_target_path=_target_key_path(target_root_relative, target_relative),
        )
        existing_hardlink = self._load_by_key(hardlink_key)
        if existing_hardlink is not None and existing_hardlink.status is OperationStatus.APPLIED:
            self._assert_same_hardlink_intent(
                existing_hardlink,
                request,
                target_root_relative,
                source_relative,
                target_relative,
            )
            self._gateway.assert_source_matches(
                source_relative_path=source_relative,
                expected_source_snapshot=request.expected_source_snapshot,
            )
            snapshot = self._assert_applied_hardlink(existing_hardlink)
            return HardlinkExecutionResult(
                directory_journal_ids=tuple(directory_journal_ids),
                hardlink_journal_id=existing_hardlink.id,
                target_snapshot=snapshot,
                replayed=True,
            )

        try:
            initial = self._gateway.inspect_hardlink(
                source_relative_path=source_relative,
                target_root_relative_path=target_root_relative,
                target_relative_path=target_relative,
                expected_source_snapshot=request.expected_source_snapshot,
            )
        except DomainViolation as exc:
            if (
                existing_hardlink is not None
                and existing_hardlink.status is OperationStatus.INTENT_RECORDED
                and exc.code is ErrorCode.TARGET_CONFLICT
            ):
                self._transition_if_current(
                    existing_hardlink.id,
                    OperationStatus.INTENT_RECORDED,
                    OperationStatus.RECONCILE_REQUIRED,
                )
            raise
        missing = set(initial.missing_directories)

        for depth in range(1, len(target_parts)):
            directory_relative = "/".join(target_parts[:depth])
            key = file_operation_key(
                candidate_key=request.candidate_key,
                operation_type=CREATE_DIRECTORY_OPERATION,
                normalized_target_path=_target_key_path(
                    initial.target_root_relative_path,
                    directory_relative,
                ),
            )
            existing = self._load_by_key(key)
            if existing is not None:
                if existing.status is OperationStatus.APPLIED:
                    continue
                if existing.status is not OperationStatus.INTENT_RECORDED:
                    raise _journal_not_executable(existing)
                if directory_relative not in missing:
                    self._transition(
                        existing.id,
                        OperationStatus.INTENT_RECORDED,
                        OperationStatus.RECONCILE_REQUIRED,
                    )
                    raise DomainViolation(
                        ErrorCode.TARGET_CONFLICT,
                        "目录已出现但 journal 尚未证明其由本次操作创建，必须对账",
                    )
                parent_snapshot = _parent_snapshot_from_before(existing.before_snapshot)
                journal = existing
            elif directory_relative not in missing:
                continue
            else:
                inspection = self._gateway.inspect_directory_creation(
                    target_root_relative_path=initial.target_root_relative_path,
                    directory_relative_path=directory_relative,
                )
                parent_snapshot = inspection.parent_snapshot
                journal, _ = self._record_intent(
                    OperationIntent(
                        task_id=request.task_id,
                        idempotency_key=key,
                        operation_type=CREATE_DIRECTORY_OPERATION,
                        target={
                            "target_root": inspection.target_root_relative_path,
                            "relative_path": inspection.directory_relative_path,
                        },
                        intent={
                            "schema_version": FILESYSTEM_OPERATION_SCHEMA_VERSION,
                            "resource_kind": "directory",
                        },
                        before_snapshot={
                            "parent": inspection.parent_snapshot.to_payload(),
                            "target_absent": True,
                        },
                    )
                )
                directory_journal_ids.append(journal.id)

            created = self._gateway.create_directory(
                target_root_relative_path=initial.target_root_relative_path,
                directory_relative_path=directory_relative,
                expected_parent_snapshot=parent_snapshot,
            )
            _call_fault_hook(fault_hook, f"after_directory_created:{directory_relative}")
            self._transition(
                journal.id,
                OperationStatus.INTENT_RECORDED,
                OperationStatus.APPLIED,
                after_snapshot=created.to_payload(),
            )

        ready = self._gateway.inspect_hardlink(
            source_relative_path=source_relative,
            target_root_relative_path=target_root_relative,
            target_relative_path=target_relative,
            expected_source_snapshot=request.expected_source_snapshot,
        )
        if ready.missing_directories:
            raise DomainViolation(ErrorCode.PATH_MAPPING_INVALID, "硬链接目标父目录未完整创建")

        existing_hardlink = self._load_by_key(hardlink_key)
        before_snapshot: dict[str, Any]
        if existing_hardlink is not None:
            self._assert_same_hardlink_intent(
                existing_hardlink,
                request,
                ready.target_root_relative_path,
                ready.source_relative_path,
                ready.target_relative_path,
            )
            if existing_hardlink.status is OperationStatus.APPLIED:
                snapshot = self._assert_applied_hardlink(existing_hardlink)
                return HardlinkExecutionResult(
                    directory_journal_ids=tuple(directory_journal_ids),
                    hardlink_journal_id=existing_hardlink.id,
                    target_snapshot=snapshot,
                    replayed=True,
                )
            if existing_hardlink.status is not OperationStatus.INTENT_RECORDED:
                raise _journal_not_executable(existing_hardlink)
            before_snapshot = deepcopy(existing_hardlink.before_snapshot or {})
            target_parent_snapshot = _target_parent_snapshot_from_before(before_snapshot)
            hardlink_journal = existing_hardlink
            replayed = True
        else:
            before_snapshot = {
                "target_parent": ready.target_parent_snapshot.to_payload(),
                "target_absent": True,
            }
            target_parent_snapshot = ready.target_parent_snapshot
            hardlink_journal, _ = self._record_intent(
                OperationIntent(
                    task_id=request.task_id,
                    idempotency_key=hardlink_key,
                    operation_type=CREATE_HARDLINK_OPERATION,
                    target={
                        "target_root": ready.target_root_relative_path,
                        "relative_path": ready.target_relative_path,
                    },
                    intent={
                        "schema_version": FILESYSTEM_OPERATION_SCHEMA_VERSION,
                        "resource_kind": "hardlink",
                        "source_relative_path": ready.source_relative_path,
                        "source_snapshot": _file_snapshot_payload(request.expected_source_snapshot),
                        "temporary_name": f".packbreaker-link-{hardlink_key}.tmp",
                    },
                    before_snapshot=before_snapshot,
                )
            )
            replayed = False

        try:
            target_snapshot = self._gateway.create_hardlink_atomic(
                source_relative_path=ready.source_relative_path,
                target_root_relative_path=ready.target_root_relative_path,
                target_relative_path=ready.target_relative_path,
                expected_source_snapshot=request.expected_source_snapshot,
                expected_target_parent_snapshot=target_parent_snapshot,
                operation_token=hardlink_key,
                fault_hook=fault_hook,
            )
        except DomainViolation as exc:
            if exc.code is ErrorCode.TARGET_CONFLICT:
                self._transition_if_current(
                    hardlink_journal.id,
                    OperationStatus.INTENT_RECORDED,
                    OperationStatus.RECONCILE_REQUIRED,
                )
            raise

        self._transition(
            hardlink_journal.id,
            OperationStatus.INTENT_RECORDED,
            OperationStatus.APPLIED,
            after_snapshot=target_snapshot.to_payload(),
        )
        return HardlinkExecutionResult(
            directory_journal_ids=tuple(directory_journal_ids),
            hardlink_journal_id=hardlink_journal.id,
            target_snapshot=target_snapshot,
            replayed=replayed,
        )

    def execute_repair_isolation(
        self,
        request: RepairIsolationExecutionRequest,
        *,
        fault_hook: Callable[[str], None] | None = None,
    ) -> RepairIsolationExecutionResult:
        """把已登记 hardlink 目标复制为独立 inode；本方法不执行任何 piece 修复。"""

        prepared = self._prepare_repair_isolation(request)
        with _filesystem_operation_lock(prepared.operation_key):
            existing = self._load_by_key(prepared.operation_key)
            if existing is not None:
                self._assert_same_repair_isolation_intent(existing, prepared)
                if existing.status is OperationStatus.APPLIED:
                    snapshot = self._gateway.assert_repair_isolation_matches(
                        source_relative_path=prepared.source_relative_path,
                        target_root_relative_path=prepared.target_root_relative_path,
                        target_relative_path=prepared.target_relative_path,
                        expected_source_snapshot=prepared.expected_source_snapshot,
                        expected_target_snapshot=_filesystem_snapshot_from_payload(
                            existing.after_snapshot
                        ),
                    )
                    return RepairIsolationExecutionResult(
                        isolation_journal_id=existing.id,
                        target_snapshot=snapshot,
                        replayed=True,
                        recovered_after_unknown_result=False,
                    )
                if existing.status is not OperationStatus.INTENT_RECORDED:
                    raise _journal_not_executable(existing)
                target_parent_snapshot = _target_parent_snapshot_from_before(
                    existing.before_snapshot
                )
                target_before_snapshot = _target_snapshot_from_before(existing.before_snapshot)
                owned_temporary_snapshot = _repair_isolation_progress_snapshot(
                    existing.after_snapshot
                )
                journal = existing
                replayed = True
            else:
                inspection = self._gateway.inspect_repair_isolation_target(
                    source_relative_path=prepared.source_relative_path,
                    target_root_relative_path=prepared.target_root_relative_path,
                    target_relative_path=prepared.target_relative_path,
                    expected_source_snapshot=prepared.expected_source_snapshot,
                    expected_target_snapshot=prepared.expected_target_snapshot,
                    operation_token=prepared.operation_key,
                )
                before_snapshot = {
                    "target_parent": inspection.target_parent_snapshot.to_payload(),
                    "target": inspection.target_snapshot.to_payload(),
                    "temporary_absent": True,
                }
                journal, _ = self._record_intent(
                    OperationIntent(
                        task_id=prepared.task_id,
                        idempotency_key=prepared.operation_key,
                        operation_type=ISOLATE_REPAIR_TARGET_OPERATION,
                        target={
                            "target_root": prepared.target_root_relative_path,
                            "relative_path": prepared.target_relative_path,
                        },
                        intent=_repair_isolation_intent_payload(prepared),
                        before_snapshot=before_snapshot,
                    )
                )
                target_parent_snapshot = inspection.target_parent_snapshot
                target_before_snapshot = inspection.target_snapshot
                owned_temporary_snapshot = None
                replayed = False

            def record_progress(snapshot: FilesystemSnapshot) -> None:
                self._record_progress(
                    journal.id,
                    _repair_isolation_progress_payload(snapshot),
                )

            try:
                applied: RepairIsolationApplyResult = self._gateway.isolate_repair_target_atomic(
                    source_relative_path=prepared.source_relative_path,
                    target_root_relative_path=prepared.target_root_relative_path,
                    target_relative_path=prepared.target_relative_path,
                    expected_source_snapshot=prepared.expected_source_snapshot,
                    expected_target_snapshot=target_before_snapshot,
                    expected_target_parent_snapshot=target_parent_snapshot,
                    operation_token=prepared.operation_key,
                    owned_temporary_snapshot=owned_temporary_snapshot,
                    progress_hook=record_progress,
                    fault_hook=fault_hook,
                )
            except DomainViolation:
                self._transition_if_current(
                    journal.id,
                    OperationStatus.INTENT_RECORDED,
                    OperationStatus.RECONCILE_REQUIRED,
                )
                raise

            completed = self._transition(
                journal.id,
                OperationStatus.INTENT_RECORDED,
                OperationStatus.APPLIED,
                after_snapshot=applied.target_snapshot.to_payload(),
            )
            return RepairIsolationExecutionResult(
                isolation_journal_id=completed.id,
                target_snapshot=applied.target_snapshot,
                replayed=replayed,
                recovered_after_unknown_result=applied.recovered_after_replace,
            )

    def rollback_journal(self, journal_id: str) -> RollbackResult:
        journal = self._load_by_id(journal_id)
        if journal.operation_type not in {
            CREATE_DIRECTORY_OPERATION,
            CREATE_HARDLINK_OPERATION,
        }:
            raise DomainViolation(
                ErrorCode.INVALID_STATE_TRANSITION,
                "该 operation journal 不属于允许自动回滚的文件系统资源",
            )
        if journal.status is OperationStatus.ROLLED_BACK:
            return RollbackResult(journal.id, journal.operation_type, False, journal.status)
        if journal.status is OperationStatus.APPLIED:
            journal = self._transition(
                journal.id,
                OperationStatus.APPLIED,
                OperationStatus.ROLLBACK_PENDING,
            )
        elif journal.status is not OperationStatus.ROLLBACK_PENDING:
            raise _journal_not_rollbackable(journal)

        snapshot = _filesystem_snapshot_from_payload(journal.after_snapshot)
        target_root = _required_text(journal.target, "target_root")
        relative_path = _required_text(journal.target, "relative_path")
        try:
            if journal.operation_type == CREATE_HARDLINK_OPERATION:
                removed = self._gateway.remove_hardlink_if_matches(
                    target_root_relative_path=target_root,
                    target_relative_path=relative_path,
                    expected_snapshot=snapshot,
                )
            elif journal.operation_type == CREATE_DIRECTORY_OPERATION:
                removed = self._gateway.remove_directory_if_matches(
                    target_root_relative_path=target_root,
                    directory_relative_path=relative_path,
                    expected_snapshot=snapshot,
                )
            else:
                raise DomainViolation(
                    ErrorCode.INVALID_STATE_TRANSITION,
                    "未知文件系统 operation journal 类型不能自动回滚",
                )
        except DomainViolation as exc:
            if exc.code is ErrorCode.ROLLBACK_BLOCKED:
                self._transition_if_current(
                    journal.id,
                    OperationStatus.ROLLBACK_PENDING,
                    OperationStatus.ROLLBACK_BLOCKED,
                )
            raise

        rolled_back = self._transition(
            journal.id,
            OperationStatus.ROLLBACK_PENDING,
            OperationStatus.ROLLED_BACK,
        )
        return RollbackResult(
            journal_id=rolled_back.id,
            operation_type=rolled_back.operation_type,
            removed=removed,
            status=rolled_back.status,
        )

    def cleanup_repair_target(
        self,
        request: RepairTargetCleanupRequest,
        *,
        fault_hook: Callable[[str], None] | None = None,
    ) -> RepairTargetCleanupResult:
        """在下载器 remove 已确认后，删除仍由 isolation journal 拥有的独立 target。"""

        prepared = self._prepare_repair_cleanup(request)
        with _filesystem_operation_lock(prepared.operation_key):
            existing = self._load_by_key(prepared.operation_key)
            if existing is not None:
                self._assert_same_repair_cleanup_intent(existing, prepared)
                if existing.status is OperationStatus.APPLIED:
                    self._gateway.assert_repair_target_absent(
                        target_root_relative_path=prepared.target_root_relative_path,
                        target_relative_path=prepared.target_relative_path,
                    )
                    return RepairTargetCleanupResult(
                        cleanup_journal_id=existing.id,
                        isolation_journal_id=prepared.isolation_journal_id,
                        removed=True,
                        replayed=True,
                        recovered_after_unknown_result=False,
                    )
                if existing.status is not OperationStatus.INTENT_RECORDED:
                    raise _journal_not_executable(existing)
                journal = existing
                replayed = True
            else:
                journal, _ = self._record_intent(
                    OperationIntent(
                        task_id=prepared.task_id,
                        idempotency_key=prepared.operation_key,
                        operation_type=CLEANUP_REPAIR_TARGET_OPERATION,
                        target={
                            "target_root": prepared.target_root_relative_path,
                            "relative_path": prepared.target_relative_path,
                        },
                        intent=_repair_cleanup_intent_payload(prepared),
                        before_snapshot=prepared.expected_isolation_snapshot.to_payload(),
                    )
                )
                replayed = False

            try:
                removed = self._gateway.remove_repair_target_if_matches(
                    target_root_relative_path=prepared.target_root_relative_path,
                    target_relative_path=prepared.target_relative_path,
                    expected_isolation_snapshot=prepared.expected_isolation_snapshot,
                )
            except DomainViolation:
                self._transition_if_current(
                    journal.id,
                    OperationStatus.INTENT_RECORDED,
                    OperationStatus.RECONCILE_REQUIRED,
                )
                raise
            _call_fault_hook(fault_hook, "after_repair_target_cleanup")
            completed = self._transition(
                journal.id,
                OperationStatus.INTENT_RECORDED,
                OperationStatus.APPLIED,
                after_snapshot={"target_absent": True},
            )
            return RepairTargetCleanupResult(
                cleanup_journal_id=completed.id,
                isolation_journal_id=prepared.isolation_journal_id,
                removed=removed,
                replayed=replayed,
                recovered_after_unknown_result=replayed and not removed,
            )

    def reconcile_journal(
        self,
        journal_id: str,
        *,
        allow_applied_replay: bool = False,
    ) -> FilesystemReconcileResult:
        """只重新证明已有完成快照；不创建、删除、覆盖任何文件系统资源。"""

        journal = self._load_by_id(journal_id)
        if journal.operation_type not in {
            CREATE_DIRECTORY_OPERATION,
            CREATE_HARDLINK_OPERATION,
        }:
            raise DomainViolation(
                ErrorCode.INVALID_STATE_TRANSITION,
                "该 operation journal 不是可重新验证的文件系统操作",
            )

        if journal.status is OperationStatus.APPLIED:
            if not allow_applied_replay:
                raise DomainViolation(
                    ErrorCode.INVALID_STATE_TRANSITION,
                    "operation journal 已完成，不接受新的对账请求",
                )
            self._assert_reconcile_snapshot(journal)
            return FilesystemReconcileResult(
                journal_id=journal.id,
                operation_type=journal.operation_type,
                status=OperationStatus.APPLIED,
                replayed=True,
            )

        if journal.status is not OperationStatus.RECONCILE_REQUIRED:
            raise DomainViolation(
                ErrorCode.INVALID_STATE_TRANSITION,
                "只有 RECONCILE_REQUIRED 的文件系统 journal 可以重新验证",
            )

        snapshot = self._assert_reconcile_snapshot(journal)
        reconciled = self._transition(
            journal.id,
            OperationStatus.RECONCILE_REQUIRED,
            OperationStatus.APPLIED,
            after_snapshot=snapshot.to_payload(),
        )
        return FilesystemReconcileResult(
            journal_id=reconciled.id,
            operation_type=reconciled.operation_type,
            status=reconciled.status,
            replayed=False,
        )

    def _assert_reconcile_snapshot(self, journal: _JournalView) -> FilesystemSnapshot:
        snapshot = _filesystem_snapshot_from_payload(journal.after_snapshot)
        target_root = _required_text(journal.target, "target_root")
        relative_path = _required_text(journal.target, "relative_path")
        if journal.operation_type == CREATE_HARDLINK_OPERATION:
            return self._gateway.assert_hardlink_matches(
                target_root_relative_path=target_root,
                target_relative_path=relative_path,
                expected_snapshot=snapshot,
            )
        if journal.operation_type == CREATE_DIRECTORY_OPERATION:
            return self._gateway.assert_directory_matches(
                target_root_relative_path=target_root,
                directory_relative_path=relative_path,
                expected_snapshot=snapshot,
            )
        raise AssertionError("未覆盖的文件系统 operation type")

    def _assert_applied_directory(self, journal: _JournalView) -> FilesystemSnapshot:
        snapshot = _filesystem_snapshot_from_payload(journal.after_snapshot)
        try:
            return self._gateway.assert_directory_matches(
                target_root_relative_path=_required_text(journal.target, "target_root"),
                directory_relative_path=_required_text(journal.target, "relative_path"),
                expected_snapshot=snapshot,
            )
        except DomainViolation:
            self._transition_if_current(
                journal.id,
                OperationStatus.APPLIED,
                OperationStatus.RECONCILE_REQUIRED,
            )
            raise

    def _assert_applied_hardlink(self, journal: _JournalView) -> FilesystemSnapshot:
        snapshot = _filesystem_snapshot_from_payload(journal.after_snapshot)
        try:
            return self._gateway.assert_hardlink_matches(
                target_root_relative_path=_required_text(journal.target, "target_root"),
                target_relative_path=_required_text(journal.target, "relative_path"),
                expected_snapshot=snapshot,
            )
        except DomainViolation:
            self._transition_if_current(
                journal.id,
                OperationStatus.APPLIED,
                OperationStatus.RECONCILE_REQUIRED,
            )
            raise

    def _assert_same_directory_intent(
        self,
        journal: _JournalView,
        *,
        request: HardlinkExecutionRequest,
        target_root_relative_path: str,
        directory_relative_path: str,
    ) -> None:
        self._record_intent(
            OperationIntent(
                task_id=request.task_id,
                idempotency_key=journal.idempotency_key,
                operation_type=CREATE_DIRECTORY_OPERATION,
                target={
                    "target_root": target_root_relative_path,
                    "relative_path": directory_relative_path,
                },
                intent={
                    "schema_version": FILESYSTEM_OPERATION_SCHEMA_VERSION,
                    "resource_kind": "directory",
                },
                before_snapshot=deepcopy(journal.before_snapshot),
            )
        )

    def _assert_same_hardlink_intent(
        self,
        journal: _JournalView,
        request: HardlinkExecutionRequest,
        target_root_relative_path: str,
        source_relative_path: str,
        target_relative_path: str,
    ) -> None:
        self._record_intent(
            OperationIntent(
                task_id=request.task_id,
                idempotency_key=journal.idempotency_key,
                operation_type=CREATE_HARDLINK_OPERATION,
                target={
                    "target_root": target_root_relative_path,
                    "relative_path": target_relative_path,
                },
                intent={
                    "schema_version": FILESYSTEM_OPERATION_SCHEMA_VERSION,
                    "resource_kind": "hardlink",
                    "source_relative_path": source_relative_path,
                    "source_snapshot": _file_snapshot_payload(request.expected_source_snapshot),
                    "temporary_name": f".packbreaker-link-{journal.idempotency_key}.tmp",
                },
                before_snapshot=deepcopy(journal.before_snapshot),
            )
        )

    def _prepare_repair_isolation(
        self,
        request: RepairIsolationExecutionRequest,
    ) -> _PreparedRepairIsolation:
        hardlink = self._load_by_id(request.hardlink_journal_id)
        if (
            hardlink.task_id != request.task_id
            or hardlink.operation_type != CREATE_HARDLINK_OPERATION
            or hardlink.status is not OperationStatus.APPLIED
            or hardlink.intent.get("schema_version") != FILESYSTEM_OPERATION_SCHEMA_VERSION
            or hardlink.intent.get("resource_kind") != "hardlink"
        ):
            raise DomainViolation(
                ErrorCode.INVALID_STATE_TRANSITION,
                "repair isolation 只能由同任务 APPLIED hardlink journal 授权",
            )
        target_root = self._gateway.normalize_relative_path(
            _required_text(hardlink.target, "target_root"),
            allow_root=True,
        )
        target_relative = self._gateway.normalize_relative_path(
            _required_text(hardlink.target, "relative_path")
        )
        source_relative = self._gateway.normalize_relative_path(
            _required_text(hardlink.intent, "source_relative_path")
        )
        source_snapshot = _file_snapshot_from_payload(hardlink.intent.get("source_snapshot"))
        target_snapshot = _filesystem_snapshot_from_payload(hardlink.after_snapshot)
        if (
            source_snapshot.device != target_snapshot.device
            or source_snapshot.inode != target_snapshot.inode
            or source_snapshot.size != target_snapshot.size
            or source_snapshot.mtime_ns != target_snapshot.mtime_ns
            or target_snapshot.file_type != "regular"
        ):
            raise DomainViolation(
                ErrorCode.SOURCE_NOT_STABLE,
                "hardlink journal 的 source/target 快照无法证明同一 inode",
            )
        operation_key = file_operation_key(
            candidate_key=hardlink.idempotency_key,
            operation_type=ISOLATE_REPAIR_TARGET_OPERATION,
            normalized_target_path=_target_key_path(target_root, target_relative),
        )
        return _PreparedRepairIsolation(
            operation_key=operation_key,
            task_id=request.task_id,
            hardlink_journal_id=hardlink.id,
            source_relative_path=source_relative,
            target_root_relative_path=target_root,
            target_relative_path=target_relative,
            expected_source_snapshot=source_snapshot,
            expected_target_snapshot=target_snapshot,
            temporary_name=f".packbreaker-repair-{operation_key}.tmp",
        )

    def _prepare_repair_cleanup(
        self,
        request: RepairTargetCleanupRequest,
    ) -> _PreparedRepairCleanup:
        isolation = self._load_by_id(request.isolation_journal_id)
        if (
            isolation.task_id != request.task_id
            or isolation.operation_type != ISOLATE_REPAIR_TARGET_OPERATION
            or isolation.status is not OperationStatus.APPLIED
            or isolation.intent.get("schema_version") != REPAIR_ISOLATION_SCHEMA_VERSION
            or isolation.intent.get("resource_kind") != "repair_isolation"
        ):
            raise DomainViolation(
                ErrorCode.INVALID_STATE_TRANSITION,
                "repair cleanup 只能由同任务 APPLIED isolation journal 授权",
            )
        hardlink_journal_id = _required_text(isolation.intent, "hardlink_journal_id")
        hardlink = self._load_by_id(hardlink_journal_id)
        if (
            hardlink.task_id != request.task_id
            or hardlink.operation_type != CREATE_HARDLINK_OPERATION
            or hardlink.status is not OperationStatus.APPLIED
        ):
            raise DomainViolation(
                ErrorCode.INVALID_STATE_TRANSITION,
                "repair cleanup 的 isolation 已无法绑定原 APPLIED hardlink journal",
            )
        remove = self._load_by_id(request.remove_journal_id)
        if (
            remove.task_id != request.task_id
            or remove.operation_type not in {"QBITTORRENT_REMOVE", "TRANSMISSION_REMOVE"}
            or remove.status not in {OperationStatus.APPLIED, OperationStatus.NOOP}
            or remove.intent.get("execution_plan_id") != request.execution_plan_id
        ):
            raise DomainViolation(
                ErrorCode.INVALID_STATE_TRANSITION,
                "repair cleanup 前必须由同 execution plan 的 downloader remove journal "
                "证明停止写入",
            )
        target_root = self._gateway.normalize_relative_path(
            _required_text(isolation.target, "target_root"),
            allow_root=True,
        )
        target_relative = self._gateway.normalize_relative_path(
            _required_text(isolation.target, "relative_path")
        )
        snapshot = _filesystem_snapshot_from_payload(isolation.after_snapshot)
        if snapshot.file_type != "regular" or snapshot.link_count != 1:
            raise DomainViolation(
                ErrorCode.SOURCE_NOT_STABLE,
                "repair cleanup 的 isolation after snapshot 不是独立普通 inode",
            )
        operation_key = file_operation_key(
            candidate_key=isolation.idempotency_key,
            operation_type=CLEANUP_REPAIR_TARGET_OPERATION,
            normalized_target_path=_target_key_path(target_root, target_relative),
        )
        return _PreparedRepairCleanup(
            operation_key=operation_key,
            task_id=request.task_id,
            execution_plan_id=request.execution_plan_id,
            isolation_journal_id=isolation.id,
            remove_journal_id=remove.id,
            hardlink_journal_id=hardlink.id,
            target_root_relative_path=target_root,
            target_relative_path=target_relative,
            expected_isolation_snapshot=snapshot,
        )

    def _assert_same_repair_isolation_intent(
        self,
        journal: _JournalView,
        prepared: _PreparedRepairIsolation,
    ) -> None:
        self._record_intent(
            OperationIntent(
                task_id=prepared.task_id,
                idempotency_key=journal.idempotency_key,
                operation_type=ISOLATE_REPAIR_TARGET_OPERATION,
                target={
                    "target_root": prepared.target_root_relative_path,
                    "relative_path": prepared.target_relative_path,
                },
                intent=_repair_isolation_intent_payload(prepared),
                before_snapshot=deepcopy(journal.before_snapshot),
            )
        )

    def _assert_same_repair_cleanup_intent(
        self,
        journal: _JournalView,
        prepared: _PreparedRepairCleanup,
    ) -> None:
        self._record_intent(
            OperationIntent(
                task_id=prepared.task_id,
                idempotency_key=journal.idempotency_key,
                operation_type=CLEANUP_REPAIR_TARGET_OPERATION,
                target={
                    "target_root": prepared.target_root_relative_path,
                    "relative_path": prepared.target_relative_path,
                },
                intent=_repair_cleanup_intent_payload(prepared),
                before_snapshot=prepared.expected_isolation_snapshot.to_payload(),
            )
        )

    def _validate_existing_directory_journals(
        self,
        request: HardlinkExecutionRequest,
        *,
        target_root_relative_path: str,
        target_parts: list[str],
    ) -> list[str]:
        journal_ids: list[str] = []
        for depth in range(1, len(target_parts)):
            directory_relative = "/".join(target_parts[:depth])
            key = file_operation_key(
                candidate_key=request.candidate_key,
                operation_type=CREATE_DIRECTORY_OPERATION,
                normalized_target_path=_target_key_path(
                    target_root_relative_path,
                    directory_relative,
                ),
            )
            journal = self._load_by_key(key)
            if journal is None:
                continue
            journal_ids.append(journal.id)
            self._assert_same_directory_intent(
                journal,
                request=request,
                target_root_relative_path=target_root_relative_path,
                directory_relative_path=directory_relative,
            )
            if journal.status is OperationStatus.APPLIED:
                self._assert_applied_directory(journal)
        return journal_ids

    def _load_by_key(self, key: str) -> _JournalView | None:
        with self._session_factory() as session:
            journal = OperationJournalRepository(session).get_by_idempotency_key(key)
            return None if journal is None else _journal_view(journal)

    def _load_by_id(self, journal_id: str) -> _JournalView:
        with self._session_factory() as session:
            journal = OperationJournalRepository(session).get(journal_id)
            if journal is None:
                raise DomainViolation(ErrorCode.TASK_NOT_FOUND, "operation journal 不存在")
            return _journal_view(journal)

    def _record_intent(self, request: OperationIntent) -> tuple[_JournalView, bool]:
        with self._session_factory() as session:
            journal, created = OperationJournalRepository(session).record_intent(request)
            session.commit()
            return _journal_view(journal), created

    def _record_progress(self, journal_id: str, progress_snapshot: dict[str, Any]) -> _JournalView:
        with self._session_factory() as session:
            journal = OperationJournalRepository(session).record_intent_progress(
                journal_id=journal_id,
                progress_snapshot=progress_snapshot,
            )
            session.commit()
            return _journal_view(journal)

    def _transition(
        self,
        journal_id: str,
        expected_status: OperationStatus,
        to_status: OperationStatus,
        *,
        after_snapshot: dict[str, Any] | None = None,
    ) -> _JournalView:
        with self._session_factory() as session:
            journal = OperationJournalRepository(session).transition_status(
                journal_id=journal_id,
                expected_status=expected_status,
                to_status=to_status,
                after_snapshot=after_snapshot,
            )
            session.commit()
            return _journal_view(journal)

    def _transition_if_current(
        self,
        journal_id: str,
        expected_status: OperationStatus,
        to_status: OperationStatus,
    ) -> None:
        current = self._load_by_id(journal_id)
        if current.status is expected_status:
            self._transition(journal_id, expected_status, to_status)


def _journal_view(journal: Any) -> _JournalView:
    return _JournalView(
        id=str(journal.id),
        task_id=str(journal.task_id),
        idempotency_key=str(journal.idempotency_key),
        operation_type=str(journal.operation_type),
        target=deepcopy(journal.target),
        intent=deepcopy(journal.intent),
        status=OperationStatus(journal.status),
        before_snapshot=deepcopy(journal.before_snapshot),
        after_snapshot=deepcopy(journal.after_snapshot),
    )


def _target_key_path(target_root: str, relative_path: str) -> str:
    return relative_path if target_root == "." else f"{target_root}/{relative_path}"


def _file_snapshot_payload(snapshot: FileSnapshot) -> dict[str, int | str]:
    return {
        "device": snapshot.device,
        "inode": snapshot.inode,
        "size": snapshot.size,
        "mtime_ns": snapshot.mtime_ns,
        "file_type": snapshot.file_type,
    }


def _file_snapshot_from_payload(payload: object) -> FileSnapshot:
    if not isinstance(payload, dict):
        raise DomainViolation(ErrorCode.SOURCE_NOT_STABLE, "operation journal 缺少 source snapshot")
    try:
        return FileSnapshot(
            device=int(payload["device"]),
            inode=int(payload["inode"]),
            size=int(payload["size"]),
            mtime_ns=int(payload["mtime_ns"]),
            file_type=str(payload.get("file_type", "regular")),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise DomainViolation(
            ErrorCode.SOURCE_NOT_STABLE,
            "operation journal source snapshot 格式无效",
        ) from exc


def _filesystem_snapshot_from_payload(payload: dict[str, Any] | None) -> FilesystemSnapshot:
    if payload is None:
        raise DomainViolation(ErrorCode.SOURCE_NOT_STABLE, "operation journal 缺少 after snapshot")
    try:
        return FilesystemSnapshot(
            device=int(payload["device"]),
            inode=int(payload["inode"]),
            size=int(payload["size"]),
            mtime_ns=int(payload["mtime_ns"]),
            file_type=str(payload["file_type"]),
            link_count=int(payload["link_count"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise DomainViolation(
            ErrorCode.SOURCE_NOT_STABLE, "operation journal 快照格式无效"
        ) from exc


def _parent_snapshot_from_before(payload: dict[str, Any] | None) -> FilesystemSnapshot:
    return _nested_filesystem_snapshot(payload, "parent")


def _target_parent_snapshot_from_before(payload: dict[str, Any] | None) -> FilesystemSnapshot:
    return _nested_filesystem_snapshot(payload, "target_parent")


def _target_snapshot_from_before(payload: dict[str, Any] | None) -> FilesystemSnapshot:
    return _nested_filesystem_snapshot(payload, "target")


def _nested_filesystem_snapshot(
    payload: dict[str, Any] | None,
    key: str,
) -> FilesystemSnapshot:
    if payload is None or not isinstance(payload.get(key), dict):
        raise DomainViolation(ErrorCode.SOURCE_NOT_STABLE, "operation journal 缺少父目录快照")
    nested = payload[key]
    assert isinstance(nested, dict)
    return _filesystem_snapshot_from_payload(nested)


def _required_text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise DomainViolation(ErrorCode.PATH_MAPPING_INVALID, "operation journal 目标路径格式无效")
    return value


def _journal_not_executable(journal: _JournalView) -> DomainViolation:
    return DomainViolation(
        ErrorCode.INVALID_STATE_TRANSITION,
        f"operation journal 处于 {journal.status.value}，不能继续执行",
    )


def _journal_not_rollbackable(journal: _JournalView) -> DomainViolation:
    return DomainViolation(
        ErrorCode.INVALID_STATE_TRANSITION,
        f"operation journal 处于 {journal.status.value}，不能自动回滚",
    )


def _repair_isolation_intent_payload(prepared: _PreparedRepairIsolation) -> dict[str, Any]:
    return {
        "schema_version": REPAIR_ISOLATION_SCHEMA_VERSION,
        "resource_kind": "repair_isolation",
        "hardlink_journal_id": prepared.hardlink_journal_id,
        "source_relative_path": prepared.source_relative_path,
        "source_snapshot": _file_snapshot_payload(prepared.expected_source_snapshot),
        "temporary_name": prepared.temporary_name,
    }


def _repair_isolation_progress_payload(snapshot: FilesystemSnapshot) -> dict[str, Any]:
    return {
        "schema_version": REPAIR_ISOLATION_PROGRESS_SCHEMA_VERSION,
        "stage": "TEMP_OWNED",
        "temporary": snapshot.to_payload(),
    }


def _repair_cleanup_intent_payload(prepared: _PreparedRepairCleanup) -> dict[str, Any]:
    return {
        "schema_version": REPAIR_CLEANUP_SCHEMA_VERSION,
        "resource_kind": "repair_cleanup",
        "execution_plan_id": prepared.execution_plan_id,
        "isolation_journal_id": prepared.isolation_journal_id,
        "hardlink_journal_id": prepared.hardlink_journal_id,
        "remove_journal_id": prepared.remove_journal_id,
    }


def _repair_isolation_progress_snapshot(
    payload: dict[str, Any] | None,
) -> FilesystemSnapshot | None:
    if payload is None:
        return None
    if (
        payload.get("schema_version") != REPAIR_ISOLATION_PROGRESS_SCHEMA_VERSION
        or payload.get("stage") != "TEMP_OWNED"
        or not isinstance(payload.get("temporary"), dict)
    ):
        raise DomainViolation(
            ErrorCode.SOURCE_NOT_STABLE,
            "repair isolation journal 中间进度证据格式无效",
        )
    temporary = payload["temporary"]
    assert isinstance(temporary, dict)
    return _filesystem_snapshot_from_payload(temporary)


def _filesystem_operation_lock(operation_key: str) -> Lock:
    try:
        index = int(operation_key[:8], 16) % len(_FILESYSTEM_OPERATION_LOCKS)
    except ValueError as exc:
        raise DomainViolation(
            ErrorCode.PATH_MAPPING_INVALID, "文件系统 operation key 无效"
        ) from exc
    return _FILESYSTEM_OPERATION_LOCKS[index]


def _call_fault_hook(hook: Callable[[str], None] | None, checkpoint: str) -> None:
    if hook is not None:
        hook(checkpoint)
