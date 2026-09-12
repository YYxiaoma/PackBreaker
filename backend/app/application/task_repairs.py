from __future__ import annotations

import asyncio
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.analysis import AnalysisSiteProvider, verify_torrent_evidence
from backend.app.application.downloader_operations import (
    QBITTORRENT_ADD_OPERATION,
    QBITTORRENT_RECHECK_OPERATION,
)
from backend.app.application.downloaders import QbittorrentWriteBinding, TransmissionWriteBinding
from backend.app.application.errors import ApplicationError
from backend.app.application.filesystem_operations import (
    CREATE_HARDLINK_OPERATION,
    FILESYSTEM_OPERATION_SCHEMA_VERSION,
    ISOLATE_REPAIR_TARGET_OPERATION,
    REPAIR_ISOLATION_SCHEMA_VERSION,
    FilesystemOperationService,
    RepairIsolationExecutionRequest,
)
from backend.app.application.task_adding import CLIENT_VERIFICATION_CHECKPOINT_SCHEMA_VERSION
from backend.app.application.transmission_operations import (
    TRANSMISSION_ADD_OPERATION,
    TRANSMISSION_VERIFY_OPERATION,
)
from backend.app.domain.errors import DomainViolation
from backend.app.domain.execution_plan import (
    EXECUTION_PLAN_SCHEMA_VERSION,
    ExecutionPlanAction,
    ExecutionPlanActionKind,
    execution_plan_actions_from_payload,
)
from backend.app.domain.file_mapping import AutoMappingDecision, MappingMethod
from backend.app.domain.operation import OperationStatus
from backend.app.domain.repair import (
    RepairMode,
    RepairPlan,
    RepairTargetEvidence,
    build_repair_plan,
)
from backend.app.domain.task_state import TaskStatus
from backend.app.domain.torrent import TorrentMeta
from backend.app.domain.verification import (
    DownloaderKind,
    FileMappingEvidence,
    FileMappingState,
    FileSnapshot,
    HybridVerificationResult,
    TorrentVerificationResult,
    V1VerificationResult,
    V2VerificationResult,
)
from backend.app.infrastructure.adapters.downloaders import (
    DownloaderAdapterError,
    QbittorrentTorrentState,
    TransmissionTorrentState,
)
from backend.app.infrastructure.adapters.site_errors import SiteAdapterError
from backend.app.infrastructure.persistence.models import OperationJournal
from backend.app.infrastructure.persistence.repositories import (
    OperationJournalRepository,
    TaskRepository,
)
from backend.app.infrastructure.persistence.task_analysis_repositories import (
    TaskCandidateRepository,
    TaskExecutionPlanRepository,
)
from backend.app.infrastructure.safe_filesystem import FilesystemSnapshot, SafeFilesystemGateway
from backend.app.infrastructure.source_inventory import (
    scan_source_inventory,
    source_inventory_digest,
)
from backend.app.infrastructure.torrent_parser import parse_torrent


class DownloaderBindingProvider(Protocol):
    def write_binding(
        self, downloader_id: str
    ) -> QbittorrentWriteBinding | TransmissionWriteBinding: ...


@dataclass(frozen=True, slots=True)
class RepairPlanView:
    task_id: str
    task_unit_id: str
    execution_plan_id: str
    downloader_kind: DownloaderKind
    evidence_source: str
    plan: RepairPlan


@dataclass(frozen=True, slots=True)
class RepairIsolationTarget:
    torrent_path: str
    hardlink_journal_id: str


@dataclass(frozen=True, slots=True)
class RepairIsolationPreparation:
    task_id: str
    task_unit_id: str
    execution_plan_id: str
    targets: tuple[RepairIsolationTarget, ...]


@dataclass(frozen=True, slots=True)
class TaskRepairIsolationResult:
    task_id: str
    task_unit_id: str
    execution_plan_id: str
    isolated_paths: tuple[str, ...]
    isolation_journal_ids: tuple[str, ...]
    replayed: bool


@dataclass(frozen=True, slots=True)
class _AuthorizedRepair:
    task_id: str
    task_version: int
    task_unit_id: str
    plan_id: str
    plan_digest: str
    candidate_site_id: str
    candidate_torrent_id: str
    source_root: str
    source_inventory_digest: str
    target_root: str
    target_device: int
    target_downloader_id: str
    target_downloader_version: int
    target_downloader_binding_digest: str
    target_remote_save_path: str
    downloader_kind: DownloaderKind
    torrent_hash: str
    ownership_tag: str
    add_journal_id: str
    verification_journal_id: str
    metainfo_digest: str
    actions: tuple[ExecutionPlanAction, ...]
    checkpoint: dict[str, object]


class TaskRepairPlanService:
    """从持久化任务/operation/downloader/文件系统事实生成只读 99% repair plan。"""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        site_service: AnalysisSiteProvider,
        downloader_service: DownloaderBindingProvider,
        *,
        data_root: Path,
    ) -> None:
        self._session_factory = session_factory
        self._site_service = site_service
        self._downloader_service = downloader_service
        self._data_root = data_root
        self._filesystem = SafeFilesystemGateway(data_root)

    async def generate(self, unit_id: str, *, mode: RepairMode) -> RepairPlanView:
        authorized = self._load_authorized(unit_id)
        binding = self._load_binding(authorized)
        current = await self._owned_downloader_state(authorized, binding)
        if not current.stopped:
            raise ApplicationError(
                code="REPAIR_PLAN_DOWNLOADER_NOT_PAUSED",
                status=409,
                title="目标下载器尚未暂停",
                detail="生成字节级 repair plan 前必须由真实下载器状态证明目标 torrent 已停止写入",
            )

        meta = await self._fetch_meta(authorized)
        self._assert_source_inventory(authorized)
        target_evidence, target_mappings = self._inspect_targets(authorized, meta)
        target_verification = await asyncio.to_thread(
            verify_torrent_evidence,
            meta,
            target_mappings,
        )
        repair_verification = _verification_with_plan_mappings(
            target_verification,
            _plan_mapping_evidence(meta, authorized.actions, authorized.source_root),
        )
        plan = build_repair_plan(
            meta,
            repair_verification,
            target_evidence,
            mode=mode,
            downloader_paused=True,
        )

        # 站点、文件 hash 与文件系统检查期间不能依赖旧的数据库/下载器事实。
        final_authorized = self._load_authorized(unit_id)
        if final_authorized != authorized:
            raise _repair_input_changed()
        final_binding = self._load_binding(final_authorized)
        final_state = await self._owned_downloader_state(final_authorized, final_binding)
        if not final_state.stopped:
            raise _repair_input_changed()
        self._assert_source_inventory(final_authorized)
        self._recheck_hardlink_ownership(final_authorized)

        return RepairPlanView(
            task_id=authorized.task_id,
            task_unit_id=authorized.task_unit_id,
            execution_plan_id=authorized.plan_id,
            downloader_kind=authorized.downloader_kind,
            evidence_source="CLIENT_VERIFICATION_INCOMPLETE",
            plan=plan,
        )

    async def prepare_isolation(self, unit_id: str) -> RepairIsolationPreparation:
        """重新生成可信 AUTO_PIECE 计划，只返回当前仍需隔离的 journal-owned hardlink。"""

        authorized = self._load_authorized(unit_id)
        binding = self._load_binding(authorized)
        current = await self._owned_downloader_state(authorized, binding)
        if not current.stopped:
            raise ApplicationError(
                code="REPAIR_ISOLATION_DOWNLOADER_NOT_PAUSED",
                status=409,
                title="修复隔离前下载器必须保持暂停",
                detail="恢复或开始 inode 隔离前必须重新由真实下载器状态证明 torrent 已停止写入",
            )
        self._assert_source_inventory(authorized)

        pending = self._pending_isolation_target(authorized)
        if pending is not None:
            return RepairIsolationPreparation(
                task_id=authorized.task_id,
                task_unit_id=authorized.task_unit_id,
                execution_plan_id=authorized.plan_id,
                targets=(pending,),
            )

        view = await self.generate(unit_id, mode=RepairMode.AUTO_PIECE)
        if not view.plan.ready or view.plan.blocked_reasons:
            raise ApplicationError(
                code="REPAIR_ISOLATION_PLAN_NOT_READY",
                status=409,
                title="修复隔离计划尚未满足安全门",
                detail=(
                    "只有当前可信 repair plan 已通过暂停、空间和文件证据检查时才能执行 inode 隔离"
                ),
            )

        required_paths = {
            item.torrent_path for item in view.plan.affected_files if item.isolation_required
        }
        if not required_paths:
            return RepairIsolationPreparation(
                task_id=view.task_id,
                task_unit_id=view.task_unit_id,
                execution_plan_id=view.execution_plan_id,
                targets=(),
            )

        authorized = self._load_authorized(unit_id)
        if (
            authorized.task_id != view.task_id
            or authorized.task_unit_id != view.task_unit_id
            or authorized.plan_id != view.execution_plan_id
        ):
            raise _repair_input_changed()
        binding = self._load_binding(authorized)
        current = await self._owned_downloader_state(authorized, binding)
        if not current.stopped:
            raise _repair_input_changed()
        self._assert_source_inventory(authorized)

        hardlinks = self._hardlink_journals(authorized)
        targets: list[RepairIsolationTarget] = []
        action_by_path = {item.torrent_path: item for item in authorized.actions}
        for torrent_path in sorted(required_paths):
            action = action_by_path.get(torrent_path)
            journal = hardlinks.get(torrent_path)
            if (
                action is None
                or action.kind is not ExecutionPlanActionKind.HARDLINK
                or action.source_relative_path is None
                or journal is None
            ):
                raise _repair_ownership_unproven(
                    "当前 isolation target 缺少 execution plan HARDLINK 与 "
                    "APPLIED journal 的一一绑定"
                )
            source_relative = _join_relative_root(
                authorized.source_root,
                action.source_relative_path,
            )
            self._assert_hardlink_journal(
                authorized,
                action,
                source_relative,
                journal,
                allow_isolation_handoff=False,
            )
            targets.append(RepairIsolationTarget(torrent_path, journal.id))
        return RepairIsolationPreparation(
            task_id=view.task_id,
            task_unit_id=view.task_unit_id,
            execution_plan_id=view.execution_plan_id,
            targets=tuple(targets),
        )

    def _pending_isolation_target(
        self,
        authorized: _AuthorizedRepair,
    ) -> RepairIsolationTarget | None:
        with self._session_factory() as session:
            journals = tuple(
                journal
                for journal in OperationJournalRepository(session).list_for_task(
                    authorized.task_id,
                    operation_types=(ISOLATE_REPAIR_TARGET_OPERATION,),
                )
                if OperationStatus(journal.status) is OperationStatus.INTENT_RECORDED
            )
            if len(journals) > 1:
                raise _repair_ownership_unproven(
                    "同一任务存在多个未完成 repair isolation journal，不能自动选择恢复顺序"
                )
            if not journals:
                return None
            isolation = journals[0]
            hardlink_id = isolation.intent.get("hardlink_journal_id")
            if not isinstance(hardlink_id, str) or not hardlink_id:
                raise _repair_ownership_unproven(
                    "未完成 repair isolation 缺少 hardlink journal 绑定"
                )
            hardlink = OperationJournalRepository(session).get(hardlink_id)
            if (
                hardlink is None
                or hardlink.task_id != authorized.task_id
                or hardlink.operation_type != CREATE_HARDLINK_OPERATION
                or OperationStatus(hardlink.status) is not OperationStatus.APPLIED
                or hardlink.intent.get("schema_version") != FILESYSTEM_OPERATION_SCHEMA_VERSION
                or hardlink.intent.get("resource_kind") != "hardlink"
                or isolation.intent.get("schema_version") != REPAIR_ISOLATION_SCHEMA_VERSION
                or isolation.intent.get("resource_kind") != "repair_isolation"
                or isolation.target != hardlink.target
            ):
                raise _repair_ownership_unproven(
                    "未完成 repair isolation 与原 hardlink journal 绑定无效"
                )

            torrent_path = hardlink.target.get("relative_path")
            if (
                hardlink.target.get("target_root") != authorized.target_root
                or not isinstance(torrent_path, str)
                or not torrent_path
            ):
                raise _repair_ownership_unproven(
                    "未完成 repair isolation 目标不属于当前 execution plan"
                )
            action = next(
                (
                    item
                    for item in authorized.actions
                    if item.torrent_path == torrent_path
                    and item.kind is ExecutionPlanActionKind.HARDLINK
                ),
                None,
            )
            if (
                action is None
                or action.source_relative_path is None
                or action.source_snapshot is None
            ):
                raise _repair_ownership_unproven(
                    "未完成 repair isolation 找不到 execution plan HARDLINK 源证据"
                )
            source_relative = _join_relative_root(
                authorized.source_root,
                action.source_relative_path,
            )
            if (
                hardlink.intent.get("source_relative_path") != source_relative
                or hardlink.intent.get("source_snapshot")
                != _file_snapshot_payload(action.source_snapshot)
                or isolation.intent.get("source_relative_path") != source_relative
                or isolation.intent.get("source_snapshot")
                != _file_snapshot_payload(action.source_snapshot)
            ):
                raise _repair_ownership_unproven(
                    "未完成 repair isolation 的 source 证据与 execution plan 不一致"
                )
            return RepairIsolationTarget(torrent_path, hardlink.id)

    def _load_authorized(self, unit_id: str) -> _AuthorizedRepair:
        with self._session_factory() as session:
            plans = TaskExecutionPlanRepository(session)
            plan = plans.latest(unit_id)
            if (
                plan is None
                or plan.task_unit_id != unit_id
                or not plan.ready
                or plan.blocked_reasons
                or plan.payload.get("schema_version") != EXECUTION_PLAN_SCHEMA_VERSION
            ):
                raise ApplicationError(
                    code="REPAIR_PLAN_EXECUTION_PLAN_NOT_READY",
                    status=409,
                    title="修复所需 execution plan 未就绪",
                    detail="只允许基于 latest ready execution plan 生成 repair plan",
                )
            task = TaskRepository(session).get(plan.task_id)
            if task is None:
                raise ApplicationError(
                    code="REPAIR_PLAN_TASK_NOT_FOUND",
                    status=404,
                    title="修复任务不存在",
                    detail="execution plan 对应的任务不存在",
                )
            if TaskStatus(task.status) is not TaskStatus.RETRY:
                raise ApplicationError(
                    code="REPAIR_PLAN_TASK_STATE_INVALID",
                    status=409,
                    title="任务状态不允许生成自动修复计划",
                    detail=(
                        "只有客户端完整校验已明确发现缺失数据并进入 RETRY 的任务"
                        "可生成可信 repair plan"
                    ),
                )
            checkpoint = deepcopy(task.checkpoint)
            self._assert_incomplete_checkpoint(plan.id, plan.plan_digest, checkpoint)
            candidate = TaskCandidateRepository(session).get(plan.candidate_id)
            if candidate is None or candidate.task_id != task.id:
                raise _repair_evidence_invalid("execution plan 批准候选已不可用")

            downloader_kind = _checkpoint_downloader_kind(checkpoint)
            _assert_checkpoint_aliases(checkpoint)
            add_journal_id = _required_text(
                checkpoint,
                "add_journal_id",
                fallback="qbit_journal_id",
            )
            verification_journal_id = _required_text(
                checkpoint,
                "verification_journal_id",
                fallback="recheck_journal_id",
            )
            torrent_hash = _required_hash(checkpoint, "torrent_hash")
            ownership_tag = _required_text(checkpoint, "ownership_tag")
            remote_save_path = _required_text(checkpoint, "remote_save_path")
            target_downloader_id = _required_text(checkpoint, "target_downloader_id")
            target_downloader_version = _required_positive_int(
                checkpoint, "target_downloader_version"
            )
            target_binding_digest = _required_digest(checkpoint, "target_downloader_binding_digest")

            if (
                plan.payload.get("target_downloader_id") != target_downloader_id
                or plan.payload.get("target_downloader_version") != target_downloader_version
                or plan.payload.get("target_downloader_binding_digest") != target_binding_digest
                or plan.payload.get("target_remote_save_path") != remote_save_path
            ):
                raise _repair_evidence_invalid(
                    "RETRY checkpoint 与 execution plan 的目标下载器绑定不一致"
                )

            source_root = _required_plan_text(plan.payload, "source_root")
            source_digest = _required_plan_digest(plan.payload, "source_inventory_digest")
            metainfo_digest = _required_plan_digest(plan.payload, "metainfo_digest")
            if candidate.metainfo_digest != metainfo_digest:
                raise _repair_evidence_invalid("候选 metainfo digest 与 execution plan 不一致")

            journals = OperationJournalRepository(session)
            add_journal = journals.get(add_journal_id)
            verification_journal = journals.get(verification_journal_id)
            self._assert_downloader_journals(
                task_id=task.id,
                plan_id=plan.id,
                plan_digest=plan.plan_digest,
                metainfo_digest=metainfo_digest,
                downloader_kind=downloader_kind,
                downloader_id=target_downloader_id,
                downloader_version=target_downloader_version,
                torrent_hash=torrent_hash,
                remote_save_path=remote_save_path,
                ownership_tag=ownership_tag,
                add_journal=add_journal,
                verification_journal=verification_journal,
                add_journal_id=add_journal_id,
            )

            try:
                actions = execution_plan_actions_from_payload(plan.payload)
            except (ValueError, KeyError) as exc:
                raise _repair_evidence_invalid("execution plan 文件动作证据无法安全恢复") from exc

            return _AuthorizedRepair(
                task_id=task.id,
                task_version=task.version,
                task_unit_id=unit_id,
                plan_id=plan.id,
                plan_digest=plan.plan_digest,
                candidate_site_id=candidate.site_id,
                candidate_torrent_id=candidate.torrent_id,
                source_root=source_root,
                source_inventory_digest=source_digest,
                target_root=plan.target_root,
                target_device=plan.target_device,
                target_downloader_id=target_downloader_id,
                target_downloader_version=target_downloader_version,
                target_downloader_binding_digest=target_binding_digest,
                target_remote_save_path=remote_save_path,
                downloader_kind=downloader_kind,
                torrent_hash=torrent_hash,
                ownership_tag=ownership_tag,
                add_journal_id=add_journal_id,
                verification_journal_id=verification_journal_id,
                metainfo_digest=metainfo_digest,
                actions=actions,
                checkpoint=checkpoint,
            )

    def _assert_incomplete_checkpoint(
        self,
        plan_id: str,
        plan_digest: str,
        checkpoint: dict[str, object],
    ) -> None:
        progress = checkpoint.get("client_progress")
        if (
            checkpoint.get("schema_version") != CLIENT_VERIFICATION_CHECKPOINT_SCHEMA_VERSION
            or checkpoint.get("stage") != TaskStatus.RETRY.value
            or checkpoint.get("execution_plan_id") != plan_id
            or checkpoint.get("execution_plan_digest") != plan_digest
            or checkpoint.get("verification_outcome") != "INCOMPLETE"
            or checkpoint.get("checking_observed") is not True
            or isinstance(progress, bool)
            or not isinstance(progress, (int, float))
            or not 0.0 <= float(progress) < 1.0
        ):
            raise _repair_evidence_invalid(
                "RETRY checkpoint 不能证明客户端下载器已完成一次不完整校验"
            )

    def _assert_downloader_journals(
        self,
        *,
        task_id: str,
        plan_id: str,
        plan_digest: str,
        metainfo_digest: str,
        downloader_kind: DownloaderKind,
        downloader_id: str,
        downloader_version: int,
        torrent_hash: str,
        remote_save_path: str,
        ownership_tag: str,
        add_journal: OperationJournal | None,
        verification_journal: OperationJournal | None,
        add_journal_id: str,
    ) -> None:
        expected_add = (
            QBITTORRENT_ADD_OPERATION
            if downloader_kind is DownloaderKind.QBITTORRENT
            else TRANSMISSION_ADD_OPERATION
        )
        expected_verify = (
            QBITTORRENT_RECHECK_OPERATION
            if downloader_kind is DownloaderKind.QBITTORRENT
            else TRANSMISSION_VERIFY_OPERATION
        )
        if (
            add_journal is None
            or add_journal.task_id != task_id
            or add_journal.operation_type != expected_add
            or OperationStatus(add_journal.status) is not OperationStatus.APPLIED
            or add_journal.intent.get("execution_plan_id") != plan_id
            or add_journal.intent.get("execution_plan_digest") != plan_digest
            or add_journal.intent.get("expected_metainfo_digest") != metainfo_digest
            or add_journal.intent.get("downloader_version") != downloader_version
            or add_journal.target.get("downloader_id") != downloader_id
            or add_journal.after_snapshot is None
            or add_journal.after_snapshot.get("torrent_hash") != torrent_hash
            or add_journal.after_snapshot.get("save_path") != remote_save_path
            or add_journal.after_snapshot.get("ownership_tag") != ownership_tag
        ):
            raise _repair_evidence_invalid("ADD journal 无法证明当前 downloader torrent 归属")
        if (
            verification_journal is None
            or verification_journal.task_id != task_id
            or verification_journal.operation_type != expected_verify
            or OperationStatus(verification_journal.status) is not OperationStatus.APPLIED
            or verification_journal.intent.get("execution_plan_id") != plan_id
            or verification_journal.intent.get("downloader_version") != downloader_version
            or verification_journal.target.get("downloader_id") != downloader_id
            or verification_journal.intent.get("torrent_hash") != torrent_hash
            or verification_journal.intent.get("remote_save_path") != remote_save_path
            or verification_journal.intent.get("ownership_tag") != ownership_tag
            or verification_journal.after_snapshot is None
            or verification_journal.after_snapshot.get("torrent_hash") != torrent_hash
            or verification_journal.after_snapshot.get("save_path") != remote_save_path
            or verification_journal.after_snapshot.get("ownership_tag") != ownership_tag
        ):
            raise _repair_evidence_invalid("VERIFY journal 无法证明当前客户端校验来源")
        linked_add = verification_journal.intent.get(
            "qbit_add_journal_id"
            if downloader_kind is DownloaderKind.QBITTORRENT
            else "add_journal_id"
        )
        if linked_add != add_journal_id:
            raise _repair_evidence_invalid("VERIFY journal 未绑定同一 ADD journal")

    def _load_binding(
        self,
        authorized: _AuthorizedRepair,
    ) -> QbittorrentWriteBinding | TransmissionWriteBinding:
        try:
            self._filesystem.assert_directory(
                relative_path=authorized.target_root,
                expected_device=authorized.target_device,
            )
            binding = self._downloader_service.write_binding(authorized.target_downloader_id)
        except (ApplicationError, DomainViolation) as exc:
            raise ApplicationError(
                code="REPAIR_PLAN_BINDING_CHANGED",
                status=409,
                title="修复目标环境已变化",
                detail="target root 或下载器 binding 已不能证明与 execution plan 一致",
            ) from exc
        target_path = _data_path(self._data_root, authorized.target_root, allow_root=True)
        try:
            remote_save_path = binding.remote_save_path(target_path)
        except DomainViolation as exc:
            raise ApplicationError(
                code="REPAIR_PLAN_BINDING_CHANGED",
                status=409,
                title="修复目标路径映射已变化",
                detail="target root 已无法唯一反向映射到 execution plan 的下载器 save path",
            ) from exc
        if (
            binding.downloader_version != authorized.target_downloader_version
            or binding.binding_digest != authorized.target_downloader_binding_digest
            or remote_save_path != authorized.target_remote_save_path
            or (
                authorized.downloader_kind is DownloaderKind.QBITTORRENT
                and not isinstance(binding, QbittorrentWriteBinding)
            )
            or (
                authorized.downloader_kind is DownloaderKind.TRANSMISSION
                and not isinstance(binding, TransmissionWriteBinding)
            )
        ):
            raise ApplicationError(
                code="REPAIR_PLAN_BINDING_CHANGED",
                status=409,
                title="修复下载器绑定已变化",
                detail="下载器类型、version、binding digest 或 save path 与 execution plan 不一致",
            )
        return binding

    async def _owned_downloader_state(
        self,
        authorized: _AuthorizedRepair,
        binding: QbittorrentWriteBinding | TransmissionWriteBinding,
    ) -> QbittorrentTorrentState | TransmissionTorrentState:
        if isinstance(binding, QbittorrentWriteBinding):
            try:
                observed_qbit = await binding.adapter.get_torrents((authorized.torrent_hash,))
            except DownloaderAdapterError as exc:
                raise _repair_downloader_unavailable(exc) from exc
            qbit_matches = tuple(
                item
                for item in observed_qbit
                if item.torrent_hash == authorized.torrent_hash
                and item.save_path == authorized.target_remote_save_path
                and authorized.ownership_tag in item.tags
            )
            if len(observed_qbit) != 1 or len(qbit_matches) != 1:
                raise _repair_downloader_ownership_unproven()
            return qbit_matches[0]

        try:
            observed_transmission = await binding.adapter.get_torrents((authorized.torrent_hash,))
        except DownloaderAdapterError as exc:
            raise _repair_downloader_unavailable(exc) from exc
        transmission_matches = tuple(
            item
            for item in observed_transmission
            if item.torrent_hash == authorized.torrent_hash
            and item.download_dir == authorized.target_remote_save_path
            and authorized.ownership_tag in item.labels
        )
        if len(observed_transmission) != 1 or len(transmission_matches) != 1:
            raise _repair_downloader_ownership_unproven()
        return transmission_matches[0]

    async def _fetch_meta(self, authorized: _AuthorizedRepair) -> TorrentMeta:
        bindings = tuple(
            item
            for item in self._site_service.enabled_adapters()
            if item.site_id == authorized.candidate_site_id
        )
        if len(bindings) != 1:
            raise ApplicationError(
                code="REPAIR_PLAN_SITE_UNAVAILABLE",
                status=409,
                title="候选站点不可唯一确定",
                detail="repair plan 必须重新取得 execution plan 批准候选的当前 torrent metainfo",
            )
        try:
            payload = await bindings[0].adapter.fetch_torrent(authorized.candidate_torrent_id)
        except SiteAdapterError as exc:
            raise ApplicationError(
                code="REPAIR_PLAN_TORRENT_FETCH_FAILED",
                status=502,
                title="修复候选 torrent 获取失败",
                detail=f"站点适配器返回安全错误码：{exc.code}",
            ) from exc
        if (
            payload.site_id != authorized.candidate_site_id
            or payload.torrent_id != authorized.candidate_torrent_id
        ):
            raise _repair_evidence_invalid("重新获取的 torrent 身份与 execution plan 候选不一致")
        try:
            meta = parse_torrent(payload.content)
        except DomainViolation as exc:
            raise ApplicationError(
                code="REPAIR_PLAN_TORRENT_INVALID",
                status=409,
                title="修复候选 torrent 无法安全解析",
                detail=f"torrent 安全解析失败：{exc.code.value}",
            ) from exc
        if meta.metainfo_digest != authorized.metainfo_digest:
            raise ApplicationError(
                code="REPAIR_PLAN_TORRENT_CHANGED",
                status=409,
                title="修复候选 torrent 已变化",
                detail="当前 metainfo digest 与 execution plan 绑定值不一致",
            )
        return meta

    def _assert_source_inventory(self, authorized: _AuthorizedRepair) -> None:
        source_path = _data_path(self._data_root, authorized.source_root, allow_root=True)
        try:
            inventory = scan_source_inventory(source_path)
        except DomainViolation as exc:
            raise ApplicationError(
                code="REPAIR_PLAN_SOURCE_UNAVAILABLE",
                status=409,
                title="修复源文件不可用",
                detail="生成 repair plan 时无法重新确认 source inventory",
            ) from exc
        if source_inventory_digest(inventory) != authorized.source_inventory_digest:
            raise ApplicationError(
                code="REPAIR_PLAN_SOURCE_CHANGED",
                status=409,
                title="修复源文件已经变化",
                detail="source inventory 与 execution plan 绑定值不一致，必须重新分析",
            )

    def _inspect_targets(
        self,
        authorized: _AuthorizedRepair,
        meta: TorrentMeta,
    ) -> tuple[tuple[RepairTargetEvidence, ...], tuple[AutoMappingDecision, ...]]:
        action_by_path = {item.torrent_path: item for item in authorized.actions}
        if len(action_by_path) != len(authorized.actions):
            raise _repair_evidence_invalid("execution plan 包含重复 torrent path")
        hardlink_journals = self._hardlink_journals(authorized)
        evidence: list[RepairTargetEvidence] = []
        mappings: list[AutoMappingDecision] = []
        for torrent_file in meta.files:
            action = action_by_path.get(torrent_file.path)
            if action is None or action.length != torrent_file.length:
                raise _repair_evidence_invalid("torrent 文件与 execution plan action 不一致")
            if torrent_file.padding:
                if action.kind is not ExecutionPlanActionKind.PROTOCOL_PADDING:
                    raise _repair_evidence_invalid("padding 文件与 execution plan action 不一致")
                mappings.append(
                    AutoMappingDecision(
                        torrent_file.path,
                        FileMappingState.PADDING,
                        MappingMethod.PROTOCOL_PADDING,
                        None,
                        None,
                    )
                )
                continue
            if torrent_file.zero_length:
                if action.kind is not ExecutionPlanActionKind.ZERO_LENGTH:
                    raise _repair_evidence_invalid("零长度文件与 execution plan action 不一致")
                mappings.append(
                    AutoMappingDecision(
                        torrent_file.path,
                        FileMappingState.ZERO_LENGTH,
                        MappingMethod.ZERO_LENGTH,
                        None,
                        None,
                    )
                )
                continue

            source_relative: str | None = None
            source_snapshot: FileSnapshot | None = None
            if action.kind is ExecutionPlanActionKind.HARDLINK:
                if action.source_relative_path is None or action.source_snapshot is None:
                    raise _repair_evidence_invalid("HARDLINK action 缺少源快照")
                source_relative = _join_relative_root(
                    authorized.source_root,
                    action.source_relative_path,
                )
                source_snapshot = action.source_snapshot
                journal = hardlink_journals.get(torrent_file.path)
                if journal is None:
                    raise _repair_ownership_unproven(
                        "缺少该 HARDLINK action 的 APPLIED operation journal"
                    )
                self._assert_hardlink_journal(
                    authorized,
                    action,
                    source_relative,
                    journal,
                )
            elif action.kind is not ExecutionPlanActionKind.CLIENT_FETCH:
                raise _repair_evidence_invalid("非协议文件包含未知 execution plan action")

            try:
                target = self._filesystem.inspect_repair_target(
                    target_root_relative_path=authorized.target_root,
                    target_relative_path=torrent_file.path,
                    expected_length=torrent_file.length,
                    source_relative_path=source_relative,
                    expected_source_snapshot=source_snapshot,
                )
            except DomainViolation as exc:
                raise ApplicationError(
                    code="REPAIR_PLAN_TARGET_UNSAFE",
                    status=409,
                    title="修复目标文件无法安全检查",
                    detail=f"目标文件系统证据失败：{exc.code.value}",
                ) from exc
            evidence.append(target)
            target_path = _data_path(
                self._data_root,
                _join_relative_root(authorized.target_root, torrent_file.path),
            )
            if target.target_exists and target.target_size == torrent_file.length:
                mappings.append(
                    AutoMappingDecision(
                        torrent_file.path,
                        FileMappingState.MAPPED,
                        MappingMethod.EXACT_PATH,
                        str(target_path),
                        None,
                        (str(target_path),),
                    )
                )
            else:
                mappings.append(
                    AutoMappingDecision(
                        torrent_file.path,
                        FileMappingState.MISSING,
                        MappingMethod.NONE,
                        None,
                        None,
                    )
                )
        return tuple(evidence), tuple(mappings)

    def _hardlink_journals(self, authorized: _AuthorizedRepair) -> dict[str, OperationJournal]:
        with self._session_factory() as session:
            journals = OperationJournalRepository(session).list_for_task(
                authorized.task_id,
                operation_types=(CREATE_HARDLINK_OPERATION,),
            )
            result: dict[str, OperationJournal] = {}
            for journal in journals:
                if (
                    OperationStatus(journal.status) is not OperationStatus.APPLIED
                    or journal.target.get("target_root") != authorized.target_root
                ):
                    continue
                torrent_path = journal.target.get("relative_path")
                if not isinstance(torrent_path, str) or not torrent_path:
                    continue
                if torrent_path in result:
                    raise _repair_ownership_unproven(
                        "同一 target path 存在多个 APPLIED hardlink journal"
                    )
                # detach scalar/JSON values before session closes
                session.expunge(journal)
                result[torrent_path] = journal
            return result

    def _assert_hardlink_journal(
        self,
        authorized: _AuthorizedRepair,
        action: ExecutionPlanAction,
        source_relative: str,
        journal: OperationJournal,
        *,
        allow_isolation_handoff: bool = True,
    ) -> None:
        if action.source_snapshot is None:
            raise _repair_ownership_unproven("HARDLINK action 缺少 source snapshot")
        if (
            journal.task_id != authorized.task_id
            or journal.intent.get("schema_version") != FILESYSTEM_OPERATION_SCHEMA_VERSION
            or journal.intent.get("resource_kind") != "hardlink"
            or journal.intent.get("source_relative_path") != source_relative
            or journal.intent.get("source_snapshot")
            != _file_snapshot_payload(action.source_snapshot)
            or journal.after_snapshot is None
        ):
            raise _repair_ownership_unproven("hardlink journal 与 execution plan 源/目标证据不一致")

        isolation = self._repair_isolation_journal(authorized, journal)
        if isolation is not None:
            if not allow_isolation_handoff:
                raise _repair_ownership_unproven(
                    "目标 hardlink 已完成 inode 隔离，不应再次进入隔离队列"
                )
            if (
                isolation.target != journal.target
                or isolation.intent.get("schema_version") != REPAIR_ISOLATION_SCHEMA_VERSION
                or isolation.intent.get("resource_kind") != "repair_isolation"
                or isolation.intent.get("hardlink_journal_id") != journal.id
                or isolation.intent.get("source_relative_path") != source_relative
                or isolation.intent.get("source_snapshot")
                != _file_snapshot_payload(action.source_snapshot)
                or isolation.after_snapshot is None
            ):
                raise _repair_ownership_unproven(
                    "repair isolation journal 无法证明已从原 hardlink 完成 ownership handoff"
                )
            expected_isolated = _filesystem_snapshot(isolation.after_snapshot)
            try:
                self._filesystem.assert_repair_isolation_matches(
                    source_relative_path=source_relative,
                    target_root_relative_path=authorized.target_root,
                    target_relative_path=action.torrent_path,
                    expected_source_snapshot=action.source_snapshot,
                    expected_target_snapshot=expected_isolated,
                )
            except DomainViolation as exc:
                raise _repair_ownership_unproven(
                    "journal-owned repair isolation 当前快照已变化"
                ) from exc
            return

        expected = _filesystem_snapshot(journal.after_snapshot)
        try:
            self._filesystem.assert_hardlink_matches(
                target_root_relative_path=authorized.target_root,
                target_relative_path=action.torrent_path,
                expected_snapshot=expected,
            )
        except DomainViolation as exc:
            raise _repair_ownership_unproven("journal-owned hardlink 当前快照已变化") from exc

    def _repair_isolation_journal(
        self,
        authorized: _AuthorizedRepair,
        hardlink: OperationJournal,
    ) -> OperationJournal | None:
        with self._session_factory() as session:
            matches = tuple(
                journal
                for journal in OperationJournalRepository(session).list_for_task(
                    authorized.task_id,
                    operation_types=(ISOLATE_REPAIR_TARGET_OPERATION,),
                )
                if journal.intent.get("hardlink_journal_id") == hardlink.id
            )
            if len(matches) > 1:
                raise _repair_ownership_unproven(
                    "同一 hardlink journal 出现多个 repair isolation journal"
                )
            if not matches:
                return None
            isolation = matches[0]
            if OperationStatus(isolation.status) is not OperationStatus.APPLIED:
                raise _repair_ownership_unproven(
                    "repair isolation journal 尚未 APPLIED，必须先完成安全对账"
                )
            session.expunge(isolation)
            return isolation

    def _recheck_hardlink_ownership(self, authorized: _AuthorizedRepair) -> None:
        journals = self._hardlink_journals(authorized)
        for action in authorized.actions:
            if action.kind is not ExecutionPlanActionKind.HARDLINK:
                continue
            journal = journals.get(action.torrent_path)
            if journal is None or action.source_relative_path is None:
                raise _repair_ownership_unproven("最终复核缺少 APPLIED hardlink journal")
            self._assert_hardlink_journal(
                authorized,
                action,
                _join_relative_root(authorized.source_root, action.source_relative_path),
                journal,
            )


class TaskRepairIsolationCoordinator:
    """只执行可信 repair plan 中的 inode 隔离，不触发下载补齐或客户端下载器重校验。"""

    def __init__(
        self,
        plan_service: TaskRepairPlanService,
        filesystem_operations: FilesystemOperationService,
    ) -> None:
        self._plan_service = plan_service
        self._filesystem_operations = filesystem_operations

    async def execute(
        self,
        unit_id: str,
        *,
        fault_hook: Callable[[str], None] | None = None,
    ) -> TaskRepairIsolationResult:
        isolated_paths: list[str] = []
        journal_ids: list[str] = []
        first_preparation: RepairIsolationPreparation | None = None

        while True:
            preparation = await self._plan_service.prepare_isolation(unit_id)
            if first_preparation is None:
                first_preparation = preparation
            if not preparation.targets:
                assert first_preparation is not None
                return TaskRepairIsolationResult(
                    task_id=first_preparation.task_id,
                    task_unit_id=first_preparation.task_unit_id,
                    execution_plan_id=first_preparation.execution_plan_id,
                    isolated_paths=tuple(isolated_paths),
                    isolation_journal_ids=tuple(journal_ids),
                    replayed=not isolated_paths,
                )

            target = preparation.targets[0]
            result = await asyncio.to_thread(
                self._filesystem_operations.execute_repair_isolation,
                RepairIsolationExecutionRequest(
                    task_id=preparation.task_id,
                    hardlink_journal_id=target.hardlink_journal_id,
                ),
                fault_hook=fault_hook,
            )
            isolated_paths.append(target.torrent_path)
            journal_ids.append(result.isolation_journal_id)


def _plan_mapping_evidence(
    meta: TorrentMeta,
    actions: tuple[ExecutionPlanAction, ...],
    source_root: str,
) -> tuple[FileMappingEvidence, ...]:
    action_by_path = {item.torrent_path: item for item in actions}
    mappings: list[FileMappingEvidence] = []
    for torrent_file in meta.files:
        action = action_by_path.get(torrent_file.path)
        if action is None:
            raise _repair_evidence_invalid("execution plan 缺少 torrent 文件 action")
        if action.kind is ExecutionPlanActionKind.HARDLINK:
            if action.source_relative_path is None or action.source_snapshot is None:
                raise _repair_evidence_invalid("HARDLINK action 缺少源证据")
            mappings.append(
                FileMappingEvidence(
                    torrent_path=torrent_file.path,
                    state=FileMappingState.MAPPED,
                    source_path=_join_relative_root(source_root, action.source_relative_path),
                    snapshot=action.source_snapshot,
                )
            )
        elif action.kind is ExecutionPlanActionKind.CLIENT_FETCH:
            mappings.append(
                FileMappingEvidence(torrent_file.path, FileMappingState.MISSING, None, None)
            )
        elif action.kind is ExecutionPlanActionKind.PROTOCOL_PADDING:
            mappings.append(
                FileMappingEvidence(torrent_file.path, FileMappingState.PADDING, None, None)
            )
        elif action.kind is ExecutionPlanActionKind.ZERO_LENGTH:
            mappings.append(
                FileMappingEvidence(torrent_file.path, FileMappingState.ZERO_LENGTH, None, None)
            )
        else:
            raise _repair_evidence_invalid("execution plan 包含未知文件动作")
    return tuple(mappings)


def _verification_with_plan_mappings(
    verification: TorrentVerificationResult,
    mappings: tuple[FileMappingEvidence, ...],
) -> TorrentVerificationResult:
    if isinstance(verification, V1VerificationResult):
        return V1VerificationResult(verification.level, mappings, verification.pieces)
    if isinstance(verification, V2VerificationResult):
        return V2VerificationResult(verification.level, mappings, verification.files)
    return HybridVerificationResult(
        verification.level,
        V1VerificationResult(verification.v1.level, mappings, verification.v1.pieces),
        V2VerificationResult(verification.v2.level, mappings, verification.v2.files),
    )


def _checkpoint_downloader_kind(checkpoint: dict[str, object]) -> DownloaderKind:
    value = checkpoint.get("downloader_kind")
    if not isinstance(value, str):
        raise _repair_evidence_invalid("RETRY checkpoint 缺少 downloader_kind")
    try:
        kind = DownloaderKind(value)
    except ValueError as exc:
        raise _repair_evidence_invalid("RETRY checkpoint downloader_kind 不受支持") from exc
    if kind not in {DownloaderKind.QBITTORRENT, DownloaderKind.TRANSMISSION}:
        raise _repair_evidence_invalid("RETRY checkpoint downloader_kind 不受支持")
    return kind


def _assert_checkpoint_aliases(checkpoint: dict[str, object]) -> None:
    aliases = (
        ("add_journal_id", "qbit_journal_id"),
        ("verification_journal_id", "recheck_journal_id"),
        ("remote_save_path", "target_remote_save_path"),
    )
    for canonical, legacy in aliases:
        left = checkpoint.get(canonical)
        right = checkpoint.get(legacy)
        if left is not None and right is not None and left != right:
            raise _repair_evidence_invalid(f"RETRY checkpoint 的 {canonical}/{legacy} 证据互相矛盾")


def _required_text(payload: dict[str, object], key: str, *, fallback: str | None = None) -> str:
    value = payload.get(key)
    if value is None and fallback is not None:
        value = payload.get(fallback)
    if not isinstance(value, str) or not value:
        raise _repair_evidence_invalid(f"repair evidence 缺少有效 {key}")
    return value


def _required_hash(payload: dict[str, object], key: str) -> str:
    value = _required_text(payload, key).lower()
    if len(value) not in {40, 64} or any(char not in "0123456789abcdef" for char in value):
        raise _repair_evidence_invalid(f"repair evidence {key} 格式无效")
    return value


def _required_positive_int(payload: dict[str, object], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise _repair_evidence_invalid(f"repair evidence 缺少有效 {key}")
    return value


def _required_digest(payload: dict[str, object], key: str) -> str:
    value = _required_text(payload, key)
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise _repair_evidence_invalid(f"repair evidence {key} digest 无效")
    return value


def _required_plan_text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise _repair_evidence_invalid(f"execution plan 缺少 {key}")
    return value


def _required_plan_digest(payload: dict[str, Any], key: str) -> str:
    value = _required_plan_text(payload, key)
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise _repair_evidence_invalid(f"execution plan {key} digest 无效")
    return value


def _file_snapshot_payload(snapshot: FileSnapshot | None) -> dict[str, object] | None:
    if snapshot is None:
        return None
    return {
        "device": snapshot.device,
        "inode": snapshot.inode,
        "size": snapshot.size,
        "mtime_ns": snapshot.mtime_ns,
        "file_type": snapshot.file_type,
    }


def _filesystem_snapshot(payload: dict[str, Any]) -> FilesystemSnapshot:
    device = payload.get("device")
    inode = payload.get("inode")
    size = payload.get("size")
    mtime_ns = payload.get("mtime_ns")
    link_count = payload.get("link_count")
    file_type = payload.get("file_type")
    if (
        not _is_nonnegative_int(device)
        or not _is_nonnegative_int(inode)
        or not _is_nonnegative_int(size)
        or not _is_nonnegative_int(mtime_ns)
        or not _is_nonnegative_int(link_count)
        or not isinstance(file_type, str)
        or not file_type
    ):
        raise _repair_ownership_unproven("hardlink after snapshot 格式无效")
    assert isinstance(device, int)
    assert isinstance(inode, int)
    assert isinstance(size, int)
    assert isinstance(mtime_ns, int)
    assert isinstance(link_count, int)
    return FilesystemSnapshot(device, inode, size, mtime_ns, file_type, link_count)


def _is_nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _data_path(data_root: Path, relative: str, *, allow_root: bool = False) -> Path:
    gateway = SafeFilesystemGateway(data_root)
    normalized = gateway.normalize_relative_path(relative, allow_root=allow_root)
    return data_root if normalized == "." else data_root.joinpath(*normalized.split("/"))


def _join_relative_root(root: str, relative: str) -> str:
    return relative if root == "." else f"{root}/{relative}"


def _repair_evidence_invalid(detail: str) -> ApplicationError:
    return ApplicationError(
        code="REPAIR_PLAN_EVIDENCE_INVALID",
        status=409,
        title="修复安全证据无效",
        detail=detail,
    )


def _repair_ownership_unproven(detail: str) -> ApplicationError:
    return ApplicationError(
        code="REPAIR_PLAN_TARGET_OWNERSHIP_UNPROVEN",
        status=409,
        title="无法证明修复目标文件归属",
        detail=detail,
    )


def _repair_input_changed() -> ApplicationError:
    return ApplicationError(
        code="REPAIR_PLAN_INPUT_CHANGED",
        status=409,
        title="修复计划生成期间输入发生变化",
        detail="task/execution plan/downloader 或文件证据在只读验证期间发生变化，请重新生成",
    )


def _repair_downloader_unavailable(exc: DownloaderAdapterError) -> ApplicationError:
    return ApplicationError(
        code="REPAIR_PLAN_DOWNLOADER_UNAVAILABLE",
        status=502,
        title="无法读取修复目标下载器状态",
        detail=f"下载器返回稳定安全错误码：{exc.code}",
    )


def _repair_downloader_ownership_unproven() -> ApplicationError:
    return ApplicationError(
        code="REPAIR_PLAN_DOWNLOADER_OWNERSHIP_UNPROVEN",
        status=409,
        title="无法证明修复目标 torrent 归属",
        detail=("当前 hash、save path、ownership tag/label 或存在性与 operation journal 不一致"),
    )
