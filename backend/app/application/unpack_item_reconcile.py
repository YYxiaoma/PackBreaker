"""受控对账：把结果无法证明的外部操作收敛成可续跑的证据。

规则是 fail-closed 的：

- 文件落位类：目标不存在 → 确认未生效（归档后移除 journal，落位会重跑）
  目标与冻结证据一致 → APPLIED
  目标存在但身份/内容不符（如复制的归属无法证明）→ 保持人工核对
- 下载器 ADD 类：查询下载器里的 expected_hashes
  不存在 → 确认未生效（归档后移除 journal，ADD 会重发）
  存在且 ownership tag 与保存路径匹配 → APPLIED
  存在但不属于本次操作 → 保持人工核对

任何无法证明的操作都保持 ``RECONCILE_REQUIRED``，条目继续停在错误态，
不会因为“看起来没问题”而被盲目重放。归档证据写入独立审计表，不可变。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.downloaders import (
    QbittorrentWriteBinding,
    TransmissionWriteBinding,
)
from backend.app.application.errors import ApplicationError
from backend.app.application.unpack_acceptance import assert_unmarked_execution_for_write
from backend.app.application.unpack_lifecycle import refresh_unpack_execution_state
from backend.app.domain.errors import DomainViolation
from backend.app.domain.operation import OperationStatus
from backend.app.domain.task_definition import TaskStorageMode
from backend.app.domain.unpack import UnpackItemStatus
from backend.app.domain.unpack_execution_plan import (
    UnpackExecutionAction,
    UnpackExecutionPlan,
    unpack_execution_plan_from_payload,
)
from backend.app.infrastructure.persistence.database import begin_immediate_write
from backend.app.infrastructure.persistence.models import (
    UnpackExecution,
    UnpackExecutionItem,
    UnpackExternalOperationJournal,
    UnpackReconciledOperationAudit,
    new_uuid,
    utc_now,
)
from backend.app.infrastructure.safe_filesystem import SafeFilesystemGateway

_QB_ADD = "UNPACK_EXEC_QBITTORRENT_ADD"
_TR_ADD = "UNPACK_EXEC_TRANSMISSION_ADD"
_DIRECTORY = "UNPACK_EXEC_CREATE_DIRECTORY"
_MATERIALIZE_PREFIX = "UNPACK_EXEC_MATERIALIZE_"
_REUSE = "UNPACK_EXEC_REUSE_EXISTING"

_RESOLVED_STATUSES = frozenset(
    {
        OperationStatus.APPLIED.value,
        OperationStatus.NOOP.value,
        OperationStatus.ROLLED_BACK.value,
    }
)

# 这些阶段说明文件已经落位完成，续跑时应从辅种继续而不是重做落位。
_MATERIALIZED_STAGES = frozenset(
    {
        "FILES_MATERIALIZED",
        "ADD_RESUME_CHECKING",
        "TORRENT_ADDED",
        "CLIENT_VERIFYING",
        "CLIENT_VERIFIED",
        "STARTING",
    }
)


class UnpackItemDownloaderProvider(Protocol):
    def write_binding(
        self, downloader_id: str
    ) -> QbittorrentWriteBinding | TransmissionWriteBinding: ...


class ReconcileResolution(StrEnum):
    APPLIED = "APPLIED"
    NO_EFFECT = "NO_EFFECT"
    MANUAL = "MANUAL"
    # 冻结计划绑定的下载器身份已过期：归档旧 journal 并重建执行计划。
    PLAN_RESET = "PLAN_RESET"


@dataclass(frozen=True, slots=True)
class OperationReconcileOutcome:
    journal_id: str
    operation_type: str
    resolution: ReconcileResolution
    note: str


@dataclass(frozen=True, slots=True)
class UnpackItemReconcileReport:
    item_id: str
    item_status: str
    resumed_stage: str | None
    resolved_count: int
    manual_count: int
    outcomes: tuple[OperationReconcileOutcome, ...]


@dataclass(frozen=True, slots=True)
class UnpackExecutionReconcileReport:
    execution_id: str
    processed_count: int
    resolved_count: int
    manual_count: int
    failed_count: int
    failed_reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _JournalSnapshot:
    id: str
    operation_type: str
    status: str
    target: dict[str, Any]
    intent: dict[str, Any]
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class _ItemSnapshot:
    item_id: str
    item_version: int
    item_status: str
    stage: str | None
    execution_id: str
    definition_id: str
    plan: UnpackExecutionPlan | None


@dataclass(frozen=True, slots=True)
class _Resolved:
    journal_id: str
    operation_type: str
    resolution: ReconcileResolution
    note: str
    after_snapshot: dict[str, Any] | None = None


class UnpackItemReconcileService:
    """按外部可观测证据对账，并把条目重新推回可执行的阶段。"""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        downloader_provider: UnpackItemDownloaderProvider,
        filesystem: SafeFilesystemGateway,
    ) -> None:
        self._session_factory = session_factory
        self._downloaders = downloader_provider
        self._filesystem = filesystem

    async def reconcile_item(
        self,
        item_id: str,
        *,
        expected_item_version: int,
    ) -> UnpackItemReconcileReport:
        if expected_item_version < 1:
            raise self._invalid("If-Match item version 必须大于等于 1")
        snapshot, journals = self._load(item_id, expected_item_version)
        stale_reason = self._stale_plan_reason(snapshot)
        if stale_reason is not None:
            return self._reset_stale_plan(snapshot, expected_item_version, stale_reason)
        resolved: list[_Resolved] = []
        for journal in journals:
            resolved.append(await self._resolve(snapshot, journal))
        return self._apply(snapshot, expected_item_version, resolved)

    async def reconcile_execution(
        self,
        execution_id: str,
        *,
        limit: int = 200,
    ) -> UnpackExecutionReconcileReport:
        """批量对账：只处理执行异常且仍有未决外部操作记录的影片项。"""

        if limit < 1 or limit > 500:
            raise self._invalid("批量对账数量必须位于 1 到 500 之间")
        with self._session_factory() as session:
            if session.get(UnpackExecution, execution_id) is None:
                raise self._execution_not_found()
            rows = session.scalars(
                select(UnpackExecutionItem)
                .where(UnpackExecutionItem.execution_id == execution_id)
                .where(UnpackExecutionItem.status == UnpackItemStatus.EXECUTION_ERROR.value)
                .where(
                    select(UnpackExternalOperationJournal.id)
                    .where(UnpackExternalOperationJournal.item_id == UnpackExecutionItem.id)
                    .exists()
                )
                .order_by(UnpackExecutionItem.id)
                .limit(limit)
            ).all()
            targets = tuple((item.id, item.version) for item in rows)
        resolved = 0
        manual = 0
        failed = 0
        reasons: list[str] = []
        for item_id, version in targets:
            try:
                report = await self.reconcile_item(item_id, expected_item_version=version)
            except ApplicationError as exc:
                failed += 1
                reasons.append(f"{item_id[:8]}: {exc.detail}")
                continue
            resolved += report.resolved_count
            manual += report.manual_count
        return UnpackExecutionReconcileReport(
            execution_id=execution_id,
            processed_count=len(targets),
            resolved_count=resolved,
            manual_count=manual,
            failed_count=failed,
            failed_reasons=tuple(reasons),
        )

    # ---------------------------------------------------------------- 读取

    def _load(
        self, item_id: str, expected_item_version: int
    ) -> tuple[_ItemSnapshot, tuple[_JournalSnapshot, ...]]:
        with self._session_factory() as session:
            item = session.get(UnpackExecutionItem, item_id)
            if item is None:
                raise self._not_found()
            if item.version != expected_item_version:
                raise ApplicationError(
                    code="UNPACK_ITEM_VERSION_CONFLICT",
                    status=409,
                    title="影片项已发生变化",
                    detail="请刷新任务详情后重试",
                )
            if item.status != UnpackItemStatus.EXECUTION_ERROR.value:
                raise ApplicationError(
                    code="UNPACK_RECONCILE_NOT_REQUIRED",
                    status=409,
                    title="当前影片项不需要对账",
                    detail="只有执行异常且存在未决外部操作记录的影片项才需要对账",
                )
            execution = session.get(UnpackExecution, item.execution_id)
            if execution is None:
                raise self._not_found()
            plan: UnpackExecutionPlan | None = None
            if item.execution_plan is not None and item.execution_plan_digest is not None:
                try:
                    candidate = unpack_execution_plan_from_payload(item.execution_plan)
                except ValueError:
                    candidate = None
                if candidate is not None and candidate.plan_digest == item.execution_plan_digest:
                    plan = candidate
            state = dict(item.execution_state or {})
            stage = state.get("stage")
            rows = session.scalars(
                select(UnpackExternalOperationJournal)
                .where(UnpackExternalOperationJournal.item_id == item.id)
                .order_by(
                    UnpackExternalOperationJournal.created_at,
                    UnpackExternalOperationJournal.id,
                )
            ).all()
            journals = tuple(
                _JournalSnapshot(
                    id=row.id,
                    operation_type=row.operation_type,
                    status=row.status,
                    target=dict(row.target or {}),
                    intent=dict(row.intent or {}),
                    idempotency_key=row.idempotency_key,
                )
                for row in rows
            )
            return (
                _ItemSnapshot(
                    item_id=item.id,
                    item_version=item.version,
                    item_status=item.status,
                    stage=stage if isinstance(stage, str) else None,
                    execution_id=item.execution_id,
                    definition_id=execution.definition_id,
                    plan=plan,
                ),
                journals,
            )

    # ---------------------------------------------------------------- 判定

    async def _resolve(self, snapshot: _ItemSnapshot, journal: _JournalSnapshot) -> _Resolved:
        if journal.status in _RESOLVED_STATUSES:
            return _Resolved(
                journal.id,
                journal.operation_type,
                ReconcileResolution.APPLIED,
                "已经确认成功",
            )
        if journal.operation_type in {_QB_ADD, _TR_ADD}:
            return await self._resolve_add(snapshot, journal)
        if journal.operation_type == _DIRECTORY:
            return self._resolve_directory(journal)
        if journal.operation_type.startswith(_MATERIALIZE_PREFIX) or (
            journal.operation_type == _REUSE
        ):
            return self._resolve_file(snapshot, journal)
        return _Resolved(
            journal.id,
            journal.operation_type,
            ReconcileResolution.MANUAL,
            "不支持自动对账的操作类型",
        )

    async def _resolve_add(self, snapshot: _ItemSnapshot, journal: _JournalSnapshot) -> _Resolved:
        plan = snapshot.plan
        if plan is None:
            return self._manual(journal, "影片项缺少可校验的冻结执行计划")
        downloader_id = journal.target.get("downloader_id")
        remote_save_path = journal.target.get("remote_save_path")
        hashes = journal.intent.get("expected_hashes")
        ownership_tag = journal.intent.get("ownership_tag")
        if (
            not isinstance(downloader_id, str)
            or not downloader_id
            or not isinstance(remote_save_path, str)
            or not isinstance(hashes, list)
            or not hashes
            or not all(isinstance(value, str) and value for value in hashes)
            or not isinstance(ownership_tag, str)
            or not ownership_tag
            or downloader_id != plan.target_downloader_id
        ):
            return self._manual(journal, "添加意图缺少可核对的 torrent 身份或归属标签")
        try:
            binding = self._downloaders.write_binding(downloader_id)
        except ApplicationError as exc:
            return self._manual(journal, f"无法读取目标下载器：{exc.detail}")
        expected = set(hashes)
        if isinstance(binding, QbittorrentWriteBinding):
            qb_states = await binding.adapter.get_torrents(tuple(hashes))
            if not qb_states:
                return self._not_added(journal)
            owned_qb = [
                state
                for state in qb_states
                if state.torrent_hash in expected and ownership_tag in state.tags
            ]
            if len(owned_qb) == 1 and owned_qb[0].save_path == remote_save_path:
                state = owned_qb[0]
                return _Resolved(
                    journal.id,
                    journal.operation_type,
                    ReconcileResolution.APPLIED,
                    "下载器中已存在本次操作添加且归属匹配的 torrent",
                    after_snapshot={
                        "torrent_hash": state.torrent_hash,
                        "save_path": state.save_path,
                        "state": state.state,
                        "progress": state.progress,
                        "ownership_tag": ownership_tag,
                        "request_accepted": True,
                        "verification_complete": state.verification_complete,
                    },
                )
            return self._manual(journal, "下载器中存在同 hash 任务，但归属或保存路径不匹配")

        tr_states = await binding.adapter.get_torrents(tuple(hashes))
        if not tr_states:
            return self._not_added(journal)
        owned_tr = [
            state
            for state in tr_states
            if state.torrent_hash in expected and ownership_tag in state.labels
        ]
        if len(owned_tr) == 1 and owned_tr[0].download_dir == remote_save_path:
            tr_state = owned_tr[0]
            return _Resolved(
                journal.id,
                journal.operation_type,
                ReconcileResolution.APPLIED,
                "下载器中已存在本次操作添加且归属匹配的 torrent",
                after_snapshot={
                    "torrent_hash": tr_state.torrent_hash,
                    "save_path": tr_state.download_dir,
                    "status": tr_state.status,
                    "percent_done": tr_state.percent_done,
                    "ownership_tag": ownership_tag,
                },
            )
        return self._manual(journal, "下载器中存在同 hash 任务，但归属或保存路径不匹配")

    def _not_added(self, journal: _JournalSnapshot) -> _Resolved:
        return _Resolved(
            journal.id,
            journal.operation_type,
            ReconcileResolution.NO_EFFECT,
            "下载器中不存在这些 torrent，确认上次添加未生效",
        )

    def _resolve_directory(self, journal: _JournalSnapshot) -> _Resolved:
        raw_path = journal.target.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            return self._manual(journal, "目录操作缺少目标路径")
        path = Path(raw_path)
        try:
            snapshot = path.stat()
        except OSError:
            return _Resolved(
                journal.id,
                journal.operation_type,
                ReconcileResolution.NO_EFFECT,
                "目标目录不存在，确认上次建目录未生效",
            )
        if not path.is_dir():
            return self._manual(journal, "目标路径已存在但不是目录")
        return _Resolved(
            journal.id,
            journal.operation_type,
            ReconcileResolution.APPLIED,
            "目标目录已存在",
            after_snapshot={
                "device": snapshot.st_dev,
                "inode": snapshot.st_ino,
                "file_type": "directory",
            },
        )

    def _resolve_file(self, snapshot: _ItemSnapshot, journal: _JournalSnapshot) -> _Resolved:
        plan = snapshot.plan
        if plan is None:
            return self._manual(journal, "影片项缺少可校验的冻结执行计划")
        torrent_path = journal.target.get("torrent_path")
        if not isinstance(torrent_path, str) or not torrent_path:
            return self._manual(journal, "文件操作缺少目标相对路径")
        action = self._action_for(plan, torrent_path)
        if action is None:
            return self._manual(journal, "目标文件不在冻结执行计划内")
        target = Path(plan.output_directory).joinpath(*PurePosixPath(torrent_path).parts)

        if plan.storage_mode is TaskStorageMode.SYMLINK:
            expected = self._filesystem.resolve_path(action.source_path or "").as_posix()
            try:
                actual = self._filesystem.inspect_symlink_target(relative_path=target.as_posix())
            except DomainViolation:
                if not target.exists() and not target.is_symlink():
                    return self._absent(journal)
                return self._manual(journal, "目标已存在但不是预期软链接")
            if actual != expected:
                return self._manual(journal, "软链接已存在但指向与冻结源不一致")
            link_snapshot = target.stat(follow_symlinks=False)
            return _Resolved(
                journal.id,
                journal.operation_type,
                ReconcileResolution.APPLIED,
                "软链接已指向冻结源文件",
                after_snapshot={
                    "device": link_snapshot.st_dev,
                    "inode": link_snapshot.st_ino,
                    "size": link_snapshot.st_size,
                    "file_type": "symlink",
                },
            )

        if action.reuse_target_snapshot is not None:
            frozen = action.reuse_target_snapshot
            try:
                observed = target.stat()
            except OSError:
                return self._absent(journal)
            if (
                observed.st_dev == frozen.device
                and observed.st_ino == frozen.inode
                and observed.st_size == frozen.size
            ):
                return _Resolved(
                    journal.id,
                    journal.operation_type,
                    ReconcileResolution.APPLIED,
                    "复用目标与冻结快照一致",
                    after_snapshot={
                        "device": observed.st_dev,
                        "inode": observed.st_ino,
                        "size": observed.st_size,
                        "file_type": "regular",
                    },
                )
            return self._manual(journal, "复用目标已与冻结快照不一致")

        try:
            evidence = self._filesystem.inspect_repair_target(
                target_root_relative_path=plan.output_directory,
                target_relative_path=action.torrent_path,
                expected_length=action.length,
                source_relative_path=action.source_path,
                expected_source_snapshot=action.source_snapshot,
            )
        except DomainViolation:
            # 父目录缺失或路径链不再安全：目标不可能由上次操作创建，
            # 但若同路径确实存在内容则必须交给人工核对。
            try:
                target.lstat()
            except OSError:
                return self._absent(journal)
            return self._manual(journal, "目标路径无法通过安全核对")
        if not evidence.target_exists:
            return self._absent(journal)
        if plan.storage_mode is TaskStorageMode.HARDLINK:
            source = action.source_snapshot
            if (
                source is not None
                and evidence.target_device == source.device
                and evidence.target_inode == source.inode
                and evidence.target_size == action.length
            ):
                return _Resolved(
                    journal.id,
                    journal.operation_type,
                    ReconcileResolution.APPLIED,
                    "硬链接目标与冻结源 inode 一致",
                    after_snapshot={
                        "device": evidence.target_device,
                        "inode": evidence.target_inode,
                        "size": evidence.target_size,
                        "file_type": "regular",
                    },
                )
            return self._manual(journal, "同路径已存在文件，但与冻结源身份不一致")
        return self._manual(journal, "复制结果无法证明归属，需要人工核对")

    def _absent(self, journal: _JournalSnapshot) -> _Resolved:
        return _Resolved(
            journal.id,
            journal.operation_type,
            ReconcileResolution.NO_EFFECT,
            "目标文件不存在，确认上次写入未生效",
        )

    def _manual(self, journal: _JournalSnapshot, note: str) -> _Resolved:
        return _Resolved(
            journal.id,
            journal.operation_type,
            ReconcileResolution.MANUAL,
            note,
        )

    @staticmethod
    def _action_for(plan: UnpackExecutionPlan, torrent_path: str) -> UnpackExecutionAction | None:
        for action in plan.actions:
            if action.torrent_path == torrent_path:
                return action
        return None

    # ---------------------------------------------------------------- 落库

    def _apply(
        self,
        snapshot: _ItemSnapshot,
        expected_item_version: int,
        resolved: list[_Resolved],
    ) -> UnpackItemReconcileReport:
        manual = [item for item in resolved if item.resolution is ReconcileResolution.MANUAL]
        no_effect = [item for item in resolved if item.resolution is ReconcileResolution.NO_EFFECT]
        filesystem_reset = any(
            item.operation_type == _DIRECTORY
            or item.operation_type.startswith(_MATERIALIZE_PREFIX)
            or item.operation_type == _REUSE
            for item in no_effect
        )
        now = utc_now()
        with self._session_factory() as session:
            begin_immediate_write(session)
            item = session.get(UnpackExecutionItem, snapshot.item_id)
            if item is None:
                raise self._not_found()
            if item.version != expected_item_version:
                raise ApplicationError(
                    code="UNPACK_ITEM_VERSION_CONFLICT",
                    status=409,
                    title="影片项已发生变化",
                    detail="对账期间影片项被其他操作修改，请刷新后重试",
                )
            assert_unmarked_execution_for_write(session, item.execution_id)
            for outcome in resolved:
                row = session.get(UnpackExternalOperationJournal, outcome.journal_id)
                if row is None:
                    continue
                if outcome.resolution is ReconcileResolution.APPLIED:
                    if row.status in _RESOLVED_STATUSES:
                        continue
                    row.status = OperationStatus.APPLIED.value
                    if outcome.after_snapshot is not None:
                        row.after_snapshot = outcome.after_snapshot
                    row.last_error_code = None
                    row.updated_at = now
                    continue
                if outcome.resolution is ReconcileResolution.NO_EFFECT:
                    self._archive_and_delete(
                        session,
                        row,
                        snapshot,
                        resolution=outcome.resolution,
                        note=outcome.note,
                        now=now,
                    )
                    continue
                if row.status != OperationStatus.RECONCILE_REQUIRED.value:
                    row.status = OperationStatus.RECONCILE_REQUIRED.value
                row.last_error_code = "UNPACK_RECONCILE_MANUAL_REVIEW"
                row.updated_at = now

            if manual:
                item.status = UnpackItemStatus.EXECUTION_ERROR.value
                item.last_error_code = "UNPACK_RECONCILE_MANUAL_REVIEW"
                item.last_error_message = (
                    f"仍有 {len(manual)} 项外部操作无法自动对账，请核对执行明细后处理"
                )
                item.updated_at = now
                item.version += 1
            else:
                stage = self._resume_stage(snapshot, filesystem_reset)
                item.status = UnpackItemStatus.EXECUTING.value
                item.execution_state = {
                    "stage": stage,
                    "plan_digest": snapshot.plan.plan_digest if snapshot.plan else None,
                }
                item.last_error_code = None
                item.last_error_message = None
                item.updated_at = now
                item.version += 1
            session.flush()
            execution = session.get(UnpackExecution, item.execution_id)
            if execution is not None:
                refresh_unpack_execution_state(session, execution)
            session.commit()
            resumed_stage = None if manual else self._resume_stage(snapshot, filesystem_reset)
            return UnpackItemReconcileReport(
                item_id=snapshot.item_id,
                item_status=item.status,
                resumed_stage=resumed_stage,
                resolved_count=len(resolved) - len(manual),
                manual_count=len(manual),
                outcomes=tuple(
                    OperationReconcileOutcome(
                        journal_id=outcome.journal_id,
                        operation_type=outcome.operation_type,
                        resolution=outcome.resolution,
                        note=outcome.note,
                    )
                    for outcome in resolved
                ),
            )

    def _archive_and_delete(
        self,
        session: Session,
        row: UnpackExternalOperationJournal,
        snapshot: _ItemSnapshot,
        *,
        resolution: ReconcileResolution,
        note: str,
        now: Any,
    ) -> None:
        """把 journal 的完整证据留存到独立审计表后移除原行。"""

        session.add(
            UnpackReconciledOperationAudit(
                id=new_uuid(),
                definition_id=snapshot.definition_id,
                execution_id=snapshot.execution_id,
                item_id=snapshot.item_id,
                journal_id=row.id,
                operation_type=row.operation_type,
                status_before=row.status,
                resolution=resolution.value,
                note=note,
                payload={
                    "idempotency_key": row.idempotency_key,
                    "target": dict(row.target or {}),
                    "intent": dict(row.intent or {}),
                    "before_snapshot": row.before_snapshot,
                    "after_snapshot": row.after_snapshot,
                    "last_error_code": row.last_error_code,
                    "created_at": row.created_at.isoformat(),
                    "updated_at": row.updated_at.isoformat(),
                },
                created_at=now,
            )
        )
        session.delete(row)
        session.flush()

    def _stale_plan_reason(self, snapshot: _ItemSnapshot) -> str | None:
        """冻结计划绑定的下载器身份是否已与当前配置不一致。"""

        plan = snapshot.plan
        if plan is None:
            return None
        try:
            binding = self._downloaders.write_binding(plan.target_downloader_id)
        except ApplicationError:
            # 下载器不可用时按常规对账处理，不在这里重建计划。
            return None
        changed: list[str] = []
        if binding.downloader_version != plan.target_downloader_version:
            changed.append("下载器配置版本")
        if binding.binding_digest != plan.target_downloader_binding_digest:
            changed.append("路径映射/绑定摘要")
        try:
            remote = binding.remote_save_path(Path(plan.output_directory))
        except (DomainViolation, ValueError):
            return "输出目录已无法映射到下载器"
        if remote != plan.target_remote_save_path:
            changed.append(f"保存路径（{plan.target_remote_save_path} → {remote}）")
        if not changed:
            return None
        return "、".join(changed) + " 已变化"

    def _reset_stale_plan(
        self,
        snapshot: _ItemSnapshot,
        expected_item_version: int,
        reason: str,
    ) -> UnpackItemReconcileReport:
        """归档旧计划相关的 journal，并让条目按当前下载器配置重新生成计划。"""

        now = utc_now()
        with self._session_factory() as session:
            begin_immediate_write(session)
            item = session.get(UnpackExecutionItem, snapshot.item_id)
            if item is None:
                raise self._not_found()
            if item.version != expected_item_version:
                raise ApplicationError(
                    code="UNPACK_ITEM_VERSION_CONFLICT",
                    status=409,
                    title="影片项已发生变化",
                    detail="对账期间影片项被其他操作修改，请刷新后重试",
                )
            assert_unmarked_execution_for_write(session, item.execution_id)
            rows = session.scalars(
                select(UnpackExternalOperationJournal).where(
                    UnpackExternalOperationJournal.item_id == item.id
                )
            ).all()
            for row in rows:
                self._archive_and_delete(
                    session,
                    row,
                    snapshot,
                    resolution=ReconcileResolution.PLAN_RESET,
                    note=f"冻结计划失效：{reason}",
                    now=now,
                )
            item.status = UnpackItemStatus.CONTENT_VERIFIED.value
            item.execution_plan = None
            item.execution_plan_digest = None
            item.execution_plan_created_at = None
            item.execution_state = None
            item.last_error_code = None
            item.last_error_message = None
            item.updated_at = now
            item.version += 1
            session.flush()
            execution = session.get(UnpackExecution, item.execution_id)
            if execution is not None:
                refresh_unpack_execution_state(session, execution)
            session.commit()
            return UnpackItemReconcileReport(
                item_id=snapshot.item_id,
                item_status=item.status,
                resumed_stage="CONTENT_VERIFIED",
                resolved_count=len(rows),
                manual_count=0,
                outcomes=tuple(
                    OperationReconcileOutcome(
                        journal_id=row_id,
                        operation_type=operation_type,
                        resolution=ReconcileResolution.PLAN_RESET,
                        note=f"冻结计划失效：{reason}",
                    )
                    for row_id, operation_type in ((row.id, row.operation_type) for row in rows)
                ),
            )

    @staticmethod
    def _resume_stage(snapshot: _ItemSnapshot, filesystem_reset: bool) -> str:
        if filesystem_reset:
            return "MATERIALIZING"
        if snapshot.stage == "ADD_RESUME_CHECKING":
            # torrent 已添加成功，只需继续等待下载器回填进度。
            return "ADD_RESUME_CHECKING"
        if snapshot.stage in _MATERIALIZED_STAGES:
            return "FILES_MATERIALIZED"
        return "MATERIALIZING"

    # ---------------------------------------------------------------- 错误

    @staticmethod
    def _not_found() -> ApplicationError:
        return ApplicationError(
            code="UNPACK_ITEM_NOT_FOUND",
            status=404,
            title="影片项不存在",
            detail="请刷新任务详情后重试",
        )

    @staticmethod
    def _execution_not_found() -> ApplicationError:
        return ApplicationError(
            code="UNPACK_EXECUTION_NOT_FOUND",
            status=404,
            title="数据拆包执行不存在",
            detail="未找到指定数据拆包执行",
        )

    @staticmethod
    def _invalid(detail: str) -> ApplicationError:
        return ApplicationError(
            code="UNPACK_RECONCILE_INVALID",
            status=422,
            title="对账参数无效",
            detail=detail,
        )
