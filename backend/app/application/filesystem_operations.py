from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
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
from backend.app.infrastructure.safe_filesystem import FilesystemSnapshot, SafeFilesystemGateway

FILESYSTEM_OPERATION_SCHEMA_VERSION = "packbreaker-filesystem-operation-v1"
CREATE_DIRECTORY_OPERATION = "CREATE_DIRECTORY"
CREATE_HARDLINK_OPERATION = "CREATE_HARDLINK"


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
class RollbackResult:
    journal_id: str
    operation_type: str
    removed: bool
    status: OperationStatus


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

    def rollback_journal(self, journal_id: str) -> RollbackResult:
        journal = self._load_by_id(journal_id)
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


def _call_fault_hook(hook: Callable[[str], None] | None, checkpoint: str) -> None:
    if hook is not None:
        hook(checkpoint)
