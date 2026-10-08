from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.errors import ApplicationError
from backend.app.domain.errors import DomainViolation
from backend.app.domain.operation import OperationStatus
from backend.app.domain.task_definition import TaskStorageMode
from backend.app.domain.unpack import UnpackExecutionStatus, UnpackItemStatus
from backend.app.domain.unpack_execution_plan import (
    UnpackExecutionAction,
    UnpackExecutionActionKind,
    UnpackExecutionPlan,
    unpack_execution_plan_from_payload,
)
from backend.app.infrastructure.authorized_paths import AuthorizedPathScope
from backend.app.infrastructure.persistence.database import begin_immediate_write
from backend.app.infrastructure.persistence.models import (
    UnpackExecution,
    UnpackExecutionItem,
    UnpackExternalOperationJournal,
    new_uuid,
    utc_now,
)
from backend.app.infrastructure.safe_filesystem import FilesystemSnapshot, SafeFilesystemGateway

_SCHEMA_VERSION = "packbreaker-unpack-materialize-v1"
_DIRECTORY_OPERATION = "UNPACK_EXEC_CREATE_DIRECTORY"
_HARDLINK_OPERATION = "UNPACK_EXEC_MATERIALIZE_HARDLINK"
_SYMLINK_OPERATION = "UNPACK_EXEC_MATERIALIZE_SYMLINK"
_COPY_OPERATION = "UNPACK_EXEC_MATERIALIZE_COPY"


@dataclass(frozen=True, slots=True)
class UnpackMaterializationReport:
    execution_id: str
    processed_count: int
    materialized_count: int
    error_count: int
    execution_status: UnpackExecutionStatus


class UnpackMaterializationService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        data_root: Path,
        path_scope: AuthorizedPathScope,
    ) -> None:
        self._session_factory = session_factory
        self._path_scope = path_scope
        self._filesystem = SafeFilesystemGateway(data_root, path_scope=path_scope)

    def list_materializable_execution_ids(self, *, limit: int = 20) -> tuple[str, ...]:
        if limit < 1 or limit > 500:
            raise self._invalid("文件落位 execution 查询数量必须位于 1 到 500 之间")
        with self._session_factory() as session:
            return tuple(
                session.scalars(
                    select(UnpackExecution.id)
                    .where(
                        select(UnpackExecutionItem.id)
                        .where(UnpackExecutionItem.execution_id == UnpackExecution.id)
                        .where(
                            UnpackExecutionItem.status.in_(
                                (
                                    UnpackItemStatus.PLAN_PENDING.value,
                                    UnpackItemStatus.EXECUTING.value,
                                )
                            )
                        )
                        .exists()
                    )
                    .order_by(UnpackExecution.updated_at, UnpackExecution.id)
                    .limit(limit)
                ).all()
            )

    def materialize_next_batch(
        self,
        execution_id: str,
        *,
        limit: int = 5,
    ) -> UnpackMaterializationReport:
        if limit < 1 or limit > 100:
            raise self._invalid("文件落位批次必须位于 1 到 100 之间")
        item_ids = self._list_item_ids(execution_id, limit=limit)
        processed = 0
        for item_id in item_ids:
            try:
                plan = self._reserve_and_load_plan(item_id)
                if self._stage(item_id) == "FILES_MATERIALIZED":
                    continue
                self._assert_plan_target_current(plan)
                self._ensure_directories(item_id, plan)
                self._materialize_actions(item_id, plan)
                self._mark_materialized(item_id, plan)
            except (ApplicationError, DomainViolation, ValueError) as exc:
                code, message = _safe_error(exc)
                self._mark_error(item_id, code=code, message=message)
            processed += 1
        return self._report(execution_id, processed_count=processed)

    def _list_item_ids(self, execution_id: str, *, limit: int) -> tuple[str, ...]:
        with self._session_factory() as session:
            execution = session.get(UnpackExecution, execution_id)
            if execution is None:
                raise self._not_found()
            rows = session.scalars(
                select(UnpackExecutionItem)
                .where(UnpackExecutionItem.execution_id == execution_id)
                .where(
                    UnpackExecutionItem.status.in_(
                        (
                            UnpackItemStatus.PLAN_PENDING.value,
                            UnpackItemStatus.EXECUTING.value,
                        )
                    )
                )
                .order_by(UnpackExecutionItem.id)
                .limit(limit * 4)
            ).all()
            ids: list[str] = []
            for item in rows:
                state = dict(item.execution_state or {})
                if state.get("stage") == "FILES_MATERIALIZED":
                    continue
                ids.append(item.id)
                if len(ids) == limit:
                    break
            return tuple(ids)

    def _reserve_and_load_plan(self, item_id: str) -> UnpackExecutionPlan:
        with self._session_factory() as session:
            begin_immediate_write(session)
            item = session.get(UnpackExecutionItem, item_id)
            if item is None:
                raise self._item_not_found()
            if item.status not in {
                UnpackItemStatus.PLAN_PENDING.value,
                UnpackItemStatus.EXECUTING.value,
            }:
                raise self._conflict("影片项当前不允许执行文件落位")
            if item.execution_plan is None or item.execution_plan_digest is None:
                raise self._conflict("影片项缺少冻结执行计划")
            try:
                plan = unpack_execution_plan_from_payload(item.execution_plan)
            except ValueError as exc:
                raise self._conflict("冻结执行计划无法通过领域校验") from exc
            if (
                plan.plan_digest != item.execution_plan_digest
                or plan.item_id != item.id
                or plan.candidate_id != item.selected_candidate_id
                or plan.candidate_generation != item.candidate_generation
            ):
                raise self._conflict("冻结执行计划与当前影片项绑定不一致")
            if not plan.ready:
                raise ApplicationError(
                    code="UNPACK_EXECUTION_PLAN_BLOCKED",
                    status=409,
                    title="执行计划被安全门阻断",
                    detail=",".join(reason.value for reason in plan.blocked_reasons),
                )
            if item.status == UnpackItemStatus.PLAN_PENDING.value:
                if item.version != plan.item_version_before + 1:
                    raise self._conflict("影片项版本已偏离冻结执行计划")
                item.status = UnpackItemStatus.EXECUTING.value
                item.execution_state = {
                    "stage": "MATERIALIZING",
                    "plan_digest": plan.plan_digest,
                }
                item.last_error_code = None
                item.last_error_message = None
                item.updated_at = utc_now()
                item.version += 1
                session.commit()
            return plan

    def _stage(self, item_id: str) -> str | None:
        with self._session_factory() as session:
            item = session.get(UnpackExecutionItem, item_id)
            if item is None:
                return None
            state = dict(item.execution_state or {})
            stage = state.get("stage")
            return stage if isinstance(stage, str) else None

    def _assert_plan_target_current(self, plan: UnpackExecutionPlan) -> None:
        snapshot = self._filesystem.assert_directory(
            relative_path=plan.output_directory,
            expected_device=plan.target_device,
        )
        if snapshot.file_type != "directory":
            raise self._conflict("冻结输出目录已不再是安全目录")

    def _ensure_directories(self, item_id: str, plan: UnpackExecutionPlan) -> None:
        for relative in sorted(
            plan.create_directories,
            key=lambda value: (len(PurePosixPath(value).parts), value),
        ):
            full_path = Path(plan.output_directory).joinpath(*PurePosixPath(relative).parts)
            existing = self._shared_directory_journal(full_path.as_posix())
            if existing is not None:
                self._assert_applied_directory_current(existing, full_path)
                continue

            try:
                inspection = self._filesystem.inspect_directory_creation(
                    target_root_relative_path=plan.output_directory,
                    directory_relative_path=relative,
                )
            except DomainViolation as exc:
                if exc.code.value == "TARGET_CONFLICT":
                    raise self._conflict(
                        "计划生成后目标子目录由非已确认 v2 journal 的操作创建，拒绝接管"
                    ) from exc
                raise
            target = {"path": full_path.as_posix()}
            intent = {
                "schema_version": _SCHEMA_VERSION,
                "plan_digest": plan.plan_digest,
                "directory_relative_path": relative,
                "parent_snapshot": inspection.parent_snapshot.to_payload(),
            }
            journal, _created = self._record_intent(
                item_id,
                _DIRECTORY_OPERATION,
                key=_directory_key(full_path.as_posix()),
                target=target,
                intent=intent,
                before_snapshot={"exists": False},
            )
            if journal.status == OperationStatus.APPLIED.value:
                self._assert_applied_directory_current(journal, full_path)
                continue
            try:
                created = self._filesystem.create_directory(
                    target_root_relative_path=plan.output_directory,
                    directory_relative_path=relative,
                    expected_parent_snapshot=inspection.parent_snapshot,
                )
            except DomainViolation:
                self._mark_reconcile(journal.id, "UNPACK_EXEC_DIRECTORY_RESULT_UNKNOWN")
                raise
            self._mark_applied(journal.id, created.to_payload())

    def _materialize_actions(self, item_id: str, plan: UnpackExecutionPlan) -> None:
        for action in plan.actions:
            if action.kind is not UnpackExecutionActionKind.MATERIALIZE:
                continue
            self._materialize_one(item_id, plan, action)

    def _materialize_one(
        self,
        item_id: str,
        plan: UnpackExecutionPlan,
        action: UnpackExecutionAction,
    ) -> None:
        assert action.source_path is not None and action.source_snapshot is not None
        operation_type = {
            TaskStorageMode.HARDLINK: _HARDLINK_OPERATION,
            TaskStorageMode.SYMLINK: _SYMLINK_OPERATION,
            TaskStorageMode.COPY: _COPY_OPERATION,
        }[plan.storage_mode]
        target_path = Path(plan.output_directory).joinpath(
            *PurePosixPath(action.torrent_path).parts
        )
        key = _materialize_key(item_id, plan.plan_digest, operation_type, action.torrent_path)
        target = {
            "path": target_path.as_posix(),
            "torrent_path": action.torrent_path,
        }
        intent = {
            "schema_version": _SCHEMA_VERSION,
            "plan_digest": plan.plan_digest,
            "storage_mode": plan.storage_mode.value,
            "source_path": action.source_path,
            "source_snapshot": _file_snapshot_payload(action.source_snapshot),
            "operation_token": key,
        }
        journal, created = self._record_intent(
            item_id,
            operation_type,
            key=key,
            target=target,
            intent=intent,
            before_snapshot={"exists": False},
        )

        if journal.status == OperationStatus.APPLIED.value:
            self._assert_applied_materialization_current(journal, plan, action)
            return
        if not created and self._recover_materialization(journal, plan, action):
            return

        parent_path = target_path.parent
        parent_snapshot = self._filesystem.assert_directory(
            relative_path=parent_path.as_posix(),
            expected_device=plan.target_device,
        )
        try:
            if plan.storage_mode is TaskStorageMode.HARDLINK:
                result = self._filesystem.create_hardlink_atomic(
                    source_relative_path=action.source_path,
                    target_root_relative_path=plan.output_directory,
                    target_relative_path=action.torrent_path,
                    expected_source_snapshot=action.source_snapshot,
                    expected_target_parent_snapshot=parent_snapshot,
                    operation_token=key,
                )
            elif plan.storage_mode is TaskStorageMode.SYMLINK:
                result = self._filesystem.create_symlink_atomic(
                    source_relative_path=action.source_path,
                    target_root_relative_path=plan.output_directory,
                    target_relative_path=action.torrent_path,
                    expected_source_snapshot=action.source_snapshot,
                    expected_target_parent_snapshot=parent_snapshot,
                    operation_token=key,
                )
            else:
                result = self._filesystem.create_copy_atomic(
                    source_relative_path=action.source_path,
                    target_root_relative_path=plan.output_directory,
                    target_relative_path=action.torrent_path,
                    expected_source_snapshot=action.source_snapshot,
                    expected_target_parent_snapshot=parent_snapshot,
                    operation_token=key,
                )
        except DomainViolation:
            if not self._recover_materialization(journal, plan, action):
                self._mark_reconcile(journal.id, "UNPACK_EXEC_MATERIALIZE_RESULT_UNKNOWN")
            raise
        self._mark_applied(journal.id, result.to_payload())

    def _recover_materialization(
        self,
        journal: UnpackExternalOperationJournal,
        plan: UnpackExecutionPlan,
        action: UnpackExecutionAction,
    ) -> bool:
        assert action.source_path is not None and action.source_snapshot is not None
        if journal.status == OperationStatus.RECONCILE_REQUIRED.value:
            raise ApplicationError(
                code="UNPACK_EXEC_MATERIALIZE_RECONCILE_REQUIRED",
                status=409,
                title="文件落位需要人工对账",
                detail="上次写入结果无法安全证明，禁止盲目重放",
            )
        if plan.storage_mode is TaskStorageMode.HARDLINK:
            evidence = self._filesystem.inspect_repair_target(
                target_root_relative_path=plan.output_directory,
                target_relative_path=action.torrent_path,
                expected_length=action.length,
                source_relative_path=action.source_path,
                expected_source_snapshot=action.source_snapshot,
            )
            if (
                evidence.target_exists
                and evidence.target_device == evidence.source_device
                and evidence.target_inode == evidence.source_inode
            ):
                self._mark_applied(
                    journal.id,
                    {
                        "device": evidence.target_device,
                        "inode": evidence.target_inode,
                        "size": evidence.target_size,
                        "file_type": "regular",
                    },
                )
                return True
            return False
        if plan.storage_mode is TaskStorageMode.SYMLINK:
            target = Path(plan.output_directory).joinpath(*PurePosixPath(action.torrent_path).parts)
            try:
                link_target = self._filesystem.inspect_symlink_target(
                    relative_path=target.as_posix()
                )
            except DomainViolation:
                return False
            expected_target = self._filesystem.resolve_path(action.source_path).as_posix()
            if link_target == expected_target:
                snapshot = target.stat(follow_symlinks=False)
                self._mark_applied(
                    journal.id,
                    {
                        "device": snapshot.st_dev,
                        "inode": snapshot.st_ino,
                        "size": snapshot.st_size,
                        "mtime_ns": snapshot.st_mtime_ns,
                        "file_type": "symlink",
                        "link_count": snapshot.st_nlink,
                    },
                )
                return True
            return False

        evidence = self._filesystem.inspect_repair_target(
            target_root_relative_path=plan.output_directory,
            target_relative_path=action.torrent_path,
            expected_length=action.length,
        )
        if evidence.target_exists:
            self._mark_reconcile(journal.id, "UNPACK_EXEC_COPY_OWNERSHIP_UNPROVEN")
            raise ApplicationError(
                code="UNPACK_EXEC_COPY_OWNERSHIP_UNPROVEN",
                status=409,
                title="复制结果无法证明归属",
                detail="响应丢失后发现同路径普通文件，但没有足够证据证明由本次操作创建",
            )
        return False

    def _assert_applied_materialization_current(
        self,
        journal: UnpackExternalOperationJournal,
        plan: UnpackExecutionPlan,
        action: UnpackExecutionAction,
    ) -> None:
        assert action.source_path is not None and action.source_snapshot is not None
        if plan.storage_mode is TaskStorageMode.HARDLINK:
            evidence = self._filesystem.inspect_repair_target(
                target_root_relative_path=plan.output_directory,
                target_relative_path=action.torrent_path,
                expected_length=action.length,
                source_relative_path=action.source_path,
                expected_source_snapshot=action.source_snapshot,
            )
            if (
                not evidence.target_exists
                or evidence.target_inode != evidence.source_inode
                or evidence.target_device != evidence.source_device
            ):
                self._mark_reconcile(journal.id, "UNPACK_EXEC_APPLIED_TARGET_CHANGED")
                raise self._conflict("已确认 HARDLINK 与冻结源身份不再一致")
            return
        if plan.storage_mode is TaskStorageMode.SYMLINK:
            target = Path(plan.output_directory).joinpath(*PurePosixPath(action.torrent_path).parts)
            expected = self._filesystem.resolve_path(action.source_path).as_posix()
            try:
                actual = self._filesystem.inspect_symlink_target(relative_path=target.as_posix())
            except DomainViolation as exc:
                self._mark_reconcile(journal.id, "UNPACK_EXEC_APPLIED_TARGET_CHANGED")
                raise self._conflict("已确认 SYMLINK 已变化") from exc
            if actual != expected:
                self._mark_reconcile(journal.id, "UNPACK_EXEC_APPLIED_TARGET_CHANGED")
                raise self._conflict("已确认 SYMLINK 指向发生变化")
            return

        after = _filesystem_snapshot_from_payload(journal.after_snapshot)
        if after is None:
            self._mark_reconcile(journal.id, "UNPACK_EXEC_COPY_AFTER_SNAPSHOT_MISSING")
            raise self._conflict("已确认 COPY 缺少 after snapshot")
        evidence = self._filesystem.inspect_repair_target(
            target_root_relative_path=plan.output_directory,
            target_relative_path=action.torrent_path,
            expected_length=action.length,
        )
        if (
            not evidence.target_exists
            or evidence.target_device != after.device
            or evidence.target_inode != after.inode
            or evidence.target_size != after.size
        ):
            self._mark_reconcile(journal.id, "UNPACK_EXEC_APPLIED_TARGET_CHANGED")
            raise self._conflict("已确认 COPY 文件身份发生变化")

    def _shared_directory_journal(
        self,
        full_path: str,
    ) -> UnpackExternalOperationJournal | None:
        key = _directory_key(full_path)
        with self._session_factory() as session:
            row = session.scalar(
                select(UnpackExternalOperationJournal).where(
                    UnpackExternalOperationJournal.idempotency_key == key
                )
            )
            if row is not None:
                session.expunge(row)
            return row

    def _assert_applied_directory_current(
        self,
        journal: UnpackExternalOperationJournal,
        full_path: Path,
    ) -> None:
        if journal.status is not None and journal.status != OperationStatus.APPLIED.value:
            raise ApplicationError(
                code="UNPACK_EXEC_DIRECTORY_RECONCILE_REQUIRED",
                status=409,
                title="目标目录创建需要对账",
                detail="目录 journal 尚未确认 APPLIED，禁止接管当前目录",
            )
        expected = _filesystem_snapshot_from_payload(journal.after_snapshot)
        if expected is None:
            self._mark_reconcile(journal.id, "UNPACK_EXEC_DIRECTORY_SNAPSHOT_MISSING")
            raise self._conflict("已确认目录缺少 after snapshot")
        try:
            current = self._filesystem.assert_directory(relative_path=full_path.as_posix())
        except DomainViolation as exc:
            self._mark_reconcile(journal.id, "UNPACK_EXEC_DIRECTORY_CHANGED")
            raise self._conflict("已确认目录当前不可用") from exc
        if (
            current.device != expected.device
            or current.inode != expected.inode
            or current.file_type != "directory"
        ):
            self._mark_reconcile(journal.id, "UNPACK_EXEC_DIRECTORY_CHANGED")
            raise self._conflict("已确认目录 inode 已变化")

    def _record_intent(
        self,
        item_id: str,
        operation_type: str,
        *,
        key: str,
        target: dict[str, Any],
        intent: dict[str, Any],
        before_snapshot: dict[str, Any] | None = None,
    ) -> tuple[UnpackExternalOperationJournal, bool]:
        with self._session_factory() as session:
            begin_immediate_write(session)
            existing = session.scalar(
                select(UnpackExternalOperationJournal).where(
                    UnpackExternalOperationJournal.idempotency_key == key
                )
            )
            if existing is not None:
                if (
                    existing.operation_type != operation_type
                    or existing.target != target
                    or existing.intent != intent
                ):
                    raise self._conflict("同一 v2 文件操作幂等键绑定了不同 intent")
                session.expunge(existing)
                return existing, False
            now = utc_now()
            row = UnpackExternalOperationJournal(
                id=new_uuid(),
                item_id=item_id,
                idempotency_key=key,
                operation_type=operation_type,
                target=target,
                intent=intent,
                status=OperationStatus.INTENT_RECORDED.value,
                before_snapshot=before_snapshot,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.commit()
            session.expunge(row)
            return row, True

    def _mark_applied(self, journal_id: str, after_snapshot: dict[str, Any]) -> None:
        with self._session_factory() as session:
            begin_immediate_write(session)
            row = session.get(UnpackExternalOperationJournal, journal_id)
            if row is None:
                raise self._conflict("v2 文件操作 journal 不存在")
            if row.status == OperationStatus.APPLIED.value:
                return
            if row.status not in {
                OperationStatus.INTENT_RECORDED.value,
                OperationStatus.RECONCILE_REQUIRED.value,
            }:
                raise self._conflict("v2 文件操作 journal 当前状态不能确认成功")
            row.status = OperationStatus.APPLIED.value
            row.after_snapshot = after_snapshot
            row.last_error_code = None
            row.updated_at = utc_now()
            session.commit()

    def _mark_reconcile(self, journal_id: str, code: str) -> None:
        with self._session_factory() as session:
            begin_immediate_write(session)
            row = session.get(UnpackExternalOperationJournal, journal_id)
            if row is None or row.status == OperationStatus.RECONCILE_REQUIRED.value:
                return
            row.status = OperationStatus.RECONCILE_REQUIRED.value
            row.last_error_code = code
            row.updated_at = utc_now()
            session.commit()

    def _mark_materialized(self, item_id: str, plan: UnpackExecutionPlan) -> None:
        with self._session_factory() as session:
            begin_immediate_write(session)
            item = session.get(UnpackExecutionItem, item_id)
            if (
                item is None
                or item.status != UnpackItemStatus.EXECUTING.value
                or item.execution_plan_digest != plan.plan_digest
            ):
                raise self._conflict("文件落位完成时 item/plan 已变化")
            item.execution_state = {
                "stage": "FILES_MATERIALIZED",
                "plan_digest": plan.plan_digest,
                "materialized_at": utc_now().isoformat(),
            }
            item.updated_at = utc_now()
            item.version += 1
            session.commit()

    def _mark_error(self, item_id: str, *, code: str, message: str) -> None:
        with self._session_factory() as session:
            begin_immediate_write(session)
            item = session.get(UnpackExecutionItem, item_id)
            if item is None or item.status not in {
                UnpackItemStatus.PLAN_PENDING.value,
                UnpackItemStatus.EXECUTING.value,
            }:
                return
            execution = session.get(UnpackExecution, item.execution_id)
            if execution is None:
                return
            now = utc_now()
            if item.status == UnpackItemStatus.PLAN_PENDING.value:
                item.status = UnpackItemStatus.EXECUTING.value
                session.flush()
            item.status = UnpackItemStatus.EXECUTION_ERROR.value
            item.last_error_code = code
            item.last_error_message = message
            item.updated_at = now
            item.version += 1
            session.flush()
            _refresh_execution_state(session, execution)
            session.commit()

    def _report(
        self,
        execution_id: str,
        *,
        processed_count: int,
    ) -> UnpackMaterializationReport:
        with self._session_factory() as session:
            execution = session.get(UnpackExecution, execution_id)
            if execution is None:
                raise self._not_found()
            materialized = 0
            for item in session.scalars(
                select(UnpackExecutionItem).where(UnpackExecutionItem.execution_id == execution_id)
            ).all():
                if dict(item.execution_state or {}).get("stage") == "FILES_MATERIALIZED":
                    materialized += 1
            error_count = int(
                session.scalar(
                    select(func.count(UnpackExecutionItem.id))
                    .where(UnpackExecutionItem.execution_id == execution_id)
                    .where(UnpackExecutionItem.status == UnpackItemStatus.EXECUTION_ERROR.value)
                )
                or 0
            )
            return UnpackMaterializationReport(
                execution_id=execution_id,
                processed_count=processed_count,
                materialized_count=materialized,
                error_count=error_count,
                execution_status=UnpackExecutionStatus(execution.status),
            )

    @staticmethod
    def _invalid(detail: str) -> ApplicationError:
        return ApplicationError(
            code="UNPACK_MATERIALIZE_INVALID",
            status=422,
            title="文件落位参数无效",
            detail=detail,
        )

    @staticmethod
    def _not_found() -> ApplicationError:
        return ApplicationError(
            code="UNPACK_EXECUTION_NOT_FOUND",
            status=404,
            title="数据拆包执行不存在",
            detail="未找到指定数据拆包执行",
        )

    @staticmethod
    def _item_not_found() -> ApplicationError:
        return ApplicationError(
            code="UNPACK_ITEM_NOT_FOUND",
            status=404,
            title="数据拆包影片项不存在",
            detail="未找到指定影片项",
        )

    @staticmethod
    def _conflict(detail: str) -> ApplicationError:
        return ApplicationError(
            code="UNPACK_MATERIALIZE_CONFLICT",
            status=409,
            title="数据拆包文件落位冲突",
            detail=detail,
        )


def _directory_key(full_path: str) -> str:
    return hashlib.sha256(
        f"{_SCHEMA_VERSION}:{_DIRECTORY_OPERATION}:{full_path}".encode()
    ).hexdigest()


def _materialize_key(
    item_id: str,
    plan_digest: str,
    operation_type: str,
    torrent_path: str,
) -> str:
    return hashlib.sha256(
        f"{_SCHEMA_VERSION}:{item_id}:{plan_digest}:{operation_type}:{torrent_path}".encode()
    ).hexdigest()


def _file_snapshot_payload(snapshot: Any) -> dict[str, Any]:
    return {
        "device": snapshot.device,
        "inode": snapshot.inode,
        "size": snapshot.size,
        "mtime_ns": str(snapshot.mtime_ns),
        "file_type": snapshot.file_type,
    }


def _filesystem_snapshot_from_payload(value: object) -> FilesystemSnapshot | None:
    if not isinstance(value, dict):
        return None
    device = value.get("device")
    inode = value.get("inode")
    size = value.get("size")
    mtime_ns = value.get("mtime_ns")
    file_type = value.get("file_type")
    link_count = value.get("link_count")
    if (
        isinstance(device, bool)
        or not isinstance(device, int)
        or isinstance(inode, bool)
        or not isinstance(inode, int)
        or isinstance(size, bool)
        or not isinstance(size, int)
        or isinstance(link_count, bool)
        or not isinstance(link_count, int)
        or not isinstance(mtime_ns, (int, str))
        or not isinstance(file_type, str)
    ):
        return None
    return FilesystemSnapshot(
        device=device,
        inode=inode,
        size=size,
        mtime_ns=int(mtime_ns),
        file_type=file_type,
        link_count=link_count,
    )


def _refresh_execution_state(session: Session, execution: UnpackExecution) -> None:
    def count(status: UnpackItemStatus) -> int:
        return int(
            session.scalar(
                select(func.count(UnpackExecutionItem.id))
                .where(UnpackExecutionItem.execution_id == execution.id)
                .where(UnpackExecutionItem.status == status.value)
            )
            or 0
        )

    execution.error_count = count(UnpackItemStatus.MATCH_ERROR) + count(
        UnpackItemStatus.EXECUTION_ERROR
    )
    execution.completed_count = count(UnpackItemStatus.COMPLETED)
    active = sum(
        count(status)
        for status in (
            UnpackItemStatus.CONTENT_VERIFIED,
            UnpackItemStatus.PLAN_PENDING,
            UnpackItemStatus.EXECUTING,
            UnpackItemStatus.CLIENT_VERIFYING,
        )
    )
    now = utc_now()
    if active:
        execution.status = UnpackExecutionStatus.EXECUTING.value
        execution.finished_at = None
    elif execution.error_count:
        execution.status = UnpackExecutionStatus.COMPLETED_WITH_ERRORS.value
        execution.finished_at = now
    elif execution.completed_count == execution.total_count:
        execution.status = UnpackExecutionStatus.COMPLETED.value
        execution.finished_at = now
    execution.updated_at = now
    execution.version += 1


def _safe_error(exc: ApplicationError | DomainViolation | ValueError) -> tuple[str, str]:
    if isinstance(exc, ApplicationError):
        return exc.code, exc.detail
    if isinstance(exc, DomainViolation):
        return exc.code.value, "文件落位安全校验失败"
    return "UNPACK_MATERIALIZE_INVALID", str(exc)
