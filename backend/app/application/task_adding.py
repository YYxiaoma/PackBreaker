from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.analysis import AnalysisSiteProvider
from backend.app.application.downloader_operations import (
    QBITTORRENT_ADD_OPERATION,
    QbittorrentAddOperationRequest,
    QbittorrentAddOperationResult,
    QbittorrentAddOperationService,
)
from backend.app.application.downloaders import QbittorrentWriteBinding, TransmissionWriteBinding
from backend.app.application.errors import ApplicationError
from backend.app.application.transmission_operations import (
    TRANSMISSION_ADD_OPERATION,
    TransmissionAddOperationRequest,
    TransmissionAddOperationResult,
    TransmissionAddOperationService,
)
from backend.app.domain.errors import DomainViolation
from backend.app.domain.execution_plan import EXECUTION_PLAN_SCHEMA_VERSION
from backend.app.domain.idempotency import candidate_execution_key, downloader_operation_key
from backend.app.domain.task_state import TaskStatus
from backend.app.domain.verification import DownloaderKind, VerificationLevel
from backend.app.infrastructure.adapters.downloaders import (
    DownloaderAdapterError,
    QbittorrentTorrentState,
    TransmissionTorrentState,
)
from backend.app.infrastructure.adapters.site_errors import SiteAdapterError
from backend.app.infrastructure.persistence.models import TaskExecutionPlanRecord
from backend.app.infrastructure.persistence.preflight_repositories import (
    PreflightSnapshotRepository,
)
from backend.app.infrastructure.persistence.repositories import (
    OperationJournalRepository,
    TaskRepository,
)
from backend.app.infrastructure.persistence.task_analysis_repositories import (
    TaskCandidateRepository,
    TaskExecutionGateRepository,
    TaskExecutionPlanRepository,
    TaskReviewRepository,
    TaskUnitRepository,
)
from backend.app.infrastructure.safe_filesystem import SafeFilesystemGateway
from backend.app.infrastructure.source_inventory import (
    scan_source_inventory,
    source_inventory_digest,
)
from backend.app.infrastructure.torrent_parser import parse_torrent

POST_ADD_CHECKPOINT_SCHEMA_VERSION = "packbreaker-post-add-checkpoint-v1"
CLIENT_VERIFICATION_CHECKPOINT_SCHEMA_VERSION = "packbreaker-client-verification-checkpoint-v1"
SEEDING_CHECKPOINT_SCHEMA_VERSION = "packbreaker-seeding-checkpoint-v1"
EXISTING_TORRENT_SKIP_CHECKPOINT_SCHEMA_VERSION = "packbreaker-existing-torrent-skip-v1"


class DownloaderBindingProvider(Protocol):
    def write_binding(
        self, downloader_id: str
    ) -> QbittorrentWriteBinding | TransmissionWriteBinding: ...


@dataclass(frozen=True, slots=True)
class TaskAddingResult:
    task_id: str
    task_version: int
    status: TaskStatus
    execution_plan_id: str
    qbit_journal_id: str | None
    torrent_hash: str
    remote_save_path: str
    ownership_tag: str | None
    skip_checking: bool
    replayed: bool
    recovered_after_unknown_result: bool
    skipped_existing: bool = False


@dataclass(frozen=True, slots=True)
class _AuthorizedAdd:
    task_id: str
    task_version: int
    task_idempotency_key: str
    unit_id: str
    plan_id: str
    plan_digest: str
    plan_task_version: int
    gate_id: str
    gate_digest: str
    preflight_snapshot_id: str
    review_revision_id: str
    candidate_id: str
    candidate_site_id: str
    candidate_torrent_id: str
    source_root: str
    source_inventory_digest: str
    metainfo_digest: str
    target_root: str
    target_device: int
    target_downloader_id: str
    target_downloader_version: int
    target_downloader_binding_digest: str
    target_remote_save_path: str
    verification_level: VerificationLevel
    client_check_required: bool


class TaskAddingCoordinator:
    """把 ADDING 检查点安全兑现为受控下载器任务，并推进客户端校验/做种状态。"""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        site_service: AnalysisSiteProvider,
        downloader_service: DownloaderBindingProvider,
        qbit_operations: QbittorrentAddOperationService,
        transmission_operations: TransmissionAddOperationService | None = None,
        *,
        data_root: Path,
    ) -> None:
        self._session_factory = session_factory
        self._site_service = site_service
        self._downloader_service = downloader_service
        self._qbit_operations = qbit_operations
        self._transmission_operations = transmission_operations
        self._data_root = data_root
        self._filesystem = SafeFilesystemGateway(data_root)

    async def execute(
        self,
        unit_id: str,
        *,
        execution_plan_id: str,
        fault_hook: Callable[[str], None] | None = None,
    ) -> TaskAddingResult:
        existing = self._load_completed(unit_id, execution_plan_id)
        if existing is not None:
            return existing

        authorized = self._load_authorized(unit_id, execution_plan_id)
        self._assert_source_inventory_current(authorized)
        self._assert_target_root_current(authorized)
        binding = self._load_target_binding(authorized)
        torrent_content = await self._fetch_torrent(authorized)
        candidate_key = candidate_execution_key(
            task_key=authorized.task_idempotency_key,
            site_id=authorized.candidate_site_id,
            remote_torrent_id=authorized.candidate_torrent_id,
            target_downloader_id=authorized.target_downloader_id,
        )
        downloader_kind = (
            DownloaderKind.QBITTORRENT
            if isinstance(binding, QbittorrentWriteBinding)
            else DownloaderKind.TRANSMISSION
        )
        add_operation_key = downloader_operation_key(
            candidate_key=candidate_key,
            operation_type=(
                QBITTORRENT_ADD_OPERATION
                if downloader_kind is DownloaderKind.QBITTORRENT
                else TRANSMISSION_ADD_OPERATION
            ),
            downloader_id=authorized.target_downloader_id,
        )
        if not self._add_operation_known(add_operation_key):
            existing_torrent = await self._find_existing_torrent(binding, torrent_content)
            if existing_torrent is not None:
                return self._complete_existing_torrent(
                    authorized,
                    existing_torrent,
                    downloader_kind=downloader_kind,
                )

        if isinstance(binding, QbittorrentWriteBinding):
            skip_checking = (
                authorized.verification_level is VerificationLevel.FULL_VERIFIED
                and not authorized.client_check_required
                and binding.capabilities.get("supports_skip_checking") is True
            )
            result: (
                QbittorrentAddOperationResult | TransmissionAddOperationResult
            ) = await self._qbit_operations.execute(
                QbittorrentAddOperationRequest(
                    task_id=authorized.task_id,
                    candidate_key=candidate_key,
                    downloader_id=authorized.target_downloader_id,
                    downloader_version=authorized.target_downloader_version,
                    execution_plan_id=authorized.plan_id,
                    execution_plan_digest=authorized.plan_digest,
                    expected_metainfo_digest=authorized.metainfo_digest,
                    torrent_content=torrent_content,
                    remote_save_path=authorized.target_remote_save_path,
                    verification_level=authorized.verification_level,
                    skip_checking=skip_checking,
                ),
                binding,
            )
            downloader_kind = DownloaderKind.QBITTORRENT
            fault_stage = "after_qb_applied"
        else:
            if self._transmission_operations is None:
                raise ApplicationError(
                    code="ADDING_DOWNLOADER_UNSUPPORTED",
                    status=409,
                    title="Transmission 添加服务未注册",
                    detail="当前运行时尚未注册 journal-backed Transmission 添加服务",
                )
            result = await self._transmission_operations.execute(
                TransmissionAddOperationRequest(
                    task_id=authorized.task_id,
                    candidate_key=candidate_key,
                    downloader_id=authorized.target_downloader_id,
                    downloader_version=authorized.target_downloader_version,
                    execution_plan_id=authorized.plan_id,
                    execution_plan_digest=authorized.plan_digest,
                    expected_metainfo_digest=authorized.metainfo_digest,
                    torrent_content=torrent_content,
                    remote_save_path=authorized.target_remote_save_path,
                ),
                binding,
            )
            downloader_kind = DownloaderKind.TRANSMISSION
            fault_stage = "after_transmission_applied"
        if fault_hook is not None:
            fault_hook(fault_stage)
        return self._complete_task(authorized, result, downloader_kind=downloader_kind)

    def _load_authorized(self, unit_id: str, plan_id: str) -> _AuthorizedAdd:
        with self._session_factory() as session:
            plan_repository = TaskExecutionPlanRepository(session)
            plan = plan_repository.get(plan_id)
            latest_plan = plan_repository.latest(unit_id)
            if (
                plan is None
                or plan.task_unit_id != unit_id
                or latest_plan is None
                or latest_plan.id != plan.id
                or not plan.ready
                or plan.blocked_reasons
            ):
                raise _adding_plan_not_current()
            payload = deepcopy(plan.payload)
            if payload.get("schema_version") != EXECUTION_PLAN_SCHEMA_VERSION:
                raise _adding_plan_not_current()

            task = TaskRepository(session).get(plan.task_id)
            if (
                task is None
                or task.status != TaskStatus.ADDING.value
                or task.version != plan.task_version + 2
            ):
                raise _adding_task_state_invalid()
            checkpoint = deepcopy(task.checkpoint)
            self._assert_adding_checkpoint(plan, checkpoint)

            gate = TaskExecutionGateRepository(session).latest(unit_id)
            candidate = TaskCandidateRepository(session).get(plan.candidate_id)
            unit = TaskUnitRepository(session).get(unit_id)
            preflight = PreflightSnapshotRepository(session).latest_for_task(plan.task_id)
            review = TaskReviewRepository(session).latest(
                task_unit_id=unit_id,
                preflight_snapshot_id=_required_text(payload, "preflight_snapshot_id"),
            )
            if (
                gate is None
                or gate.id != plan.execution_gate_id
                or gate.gate_digest != _required_text(payload, "execution_gate_digest")
                or not gate.eligible
                or gate.task_version != plan.task_version
                or candidate is None
                or candidate.preflight_snapshot_id
                != _required_text(payload, "preflight_snapshot_id")
                or candidate.metainfo_digest != _required_text(payload, "metainfo_digest")
                or unit is None
                or unit.task_id != plan.task_id
                or unit.source_root != _required_text(payload, "source_root")
                or unit.source_inventory_digest
                != _required_text(payload, "source_inventory_digest")
                or preflight is None
                or preflight.id != _required_text(payload, "preflight_snapshot_id")
                or review is None
                or review.id != _required_text(payload, "review_revision_id")
                or review.approved_candidate_id != plan.candidate_id
            ):
                raise _adding_plan_not_current()
            try:
                verification_level = VerificationLevel(plan.verification_level)
            except ValueError as exc:
                raise _adding_plan_invalid("execution plan 包含未知验证等级") from exc

            return _AuthorizedAdd(
                task_id=task.id,
                task_version=task.version,
                task_idempotency_key=task.idempotency_key,
                unit_id=unit_id,
                plan_id=plan.id,
                plan_digest=plan.plan_digest,
                plan_task_version=plan.task_version,
                gate_id=plan.execution_gate_id,
                gate_digest=_required_text(payload, "execution_gate_digest"),
                preflight_snapshot_id=_required_text(payload, "preflight_snapshot_id"),
                review_revision_id=_required_text(payload, "review_revision_id"),
                candidate_id=plan.candidate_id,
                candidate_site_id=candidate.site_id,
                candidate_torrent_id=candidate.torrent_id,
                source_root=_required_text(payload, "source_root"),
                source_inventory_digest=_required_text(payload, "source_inventory_digest"),
                metainfo_digest=_required_text(payload, "metainfo_digest"),
                target_root=plan.target_root,
                target_device=plan.target_device,
                target_downloader_id=_required_text(payload, "target_downloader_id"),
                target_downloader_version=_required_positive_int(
                    payload, "target_downloader_version"
                ),
                target_downloader_binding_digest=_required_text(
                    payload, "target_downloader_binding_digest"
                ),
                target_remote_save_path=_required_text(payload, "target_remote_save_path"),
                verification_level=verification_level,
                client_check_required=plan.client_check_required,
            )

    def _assert_adding_checkpoint(
        self,
        plan: TaskExecutionPlanRecord,
        checkpoint: dict[str, object],
    ) -> None:
        payload = plan.payload
        if (
            checkpoint.get("schema_version") != "packbreaker-adding-checkpoint-v1"
            or checkpoint.get("stage") != TaskStatus.ADDING.value
            or checkpoint.get("execution_plan_id") != plan.id
            or checkpoint.get("execution_plan_digest") != plan.plan_digest
            or checkpoint.get("execution_gate_id") != plan.execution_gate_id
            or checkpoint.get("execution_gate_digest")
            != _required_text(payload, "execution_gate_digest")
            or checkpoint.get("task_version_before_linking") != plan.task_version
            or checkpoint.get("target_downloader_id")
            != _required_text(payload, "target_downloader_id")
            or checkpoint.get("target_downloader_version")
            != _required_positive_int(payload, "target_downloader_version")
            or checkpoint.get("target_downloader_binding_digest")
            != _required_text(payload, "target_downloader_binding_digest")
            or checkpoint.get("target_remote_save_path")
            != _required_text(payload, "target_remote_save_path")
        ):
            raise ApplicationError(
                code="ADDING_CHECKPOINT_MISMATCH",
                status=409,
                title="ADDING 检查点不匹配",
                detail="任务没有与 execution plan 和目标下载器匹配的 ADDING 授权检查点",
            )

    def _assert_source_inventory_current(self, authorized: _AuthorizedAdd) -> None:
        normalized = self._filesystem.normalize_relative_path(
            authorized.source_root, allow_root=True
        )
        root = (
            self._data_root
            if normalized == "."
            else self._data_root.joinpath(*normalized.split("/"))
        )
        try:
            observed = source_inventory_digest(scan_source_inventory(root))
        except DomainViolation as exc:
            raise ApplicationError(
                code="ADDING_SOURCE_UNAVAILABLE",
                status=409,
                title="ADDING 源目录不可用",
                detail="qBittorrent 写入前无法重新确认 source inventory",
            ) from exc
        if observed != authorized.source_inventory_digest:
            raise ApplicationError(
                code="ADDING_SOURCE_CHANGED",
                status=409,
                title="ADDING 源目录已变化",
                detail="qBittorrent 写入前 source inventory 已变化，禁止继续",
            )

    def _assert_target_root_current(self, authorized: _AuthorizedAdd) -> None:
        try:
            self._filesystem.assert_directory(
                relative_path=authorized.target_root,
                expected_device=authorized.target_device,
            )
        except DomainViolation as exc:
            raise ApplicationError(
                code="ADDING_TARGET_CHANGED",
                status=409,
                title="ADDING 目标目录已变化",
                detail="下载器写入前 target root 身份或设备已变化",
            ) from exc

    def _load_target_binding(
        self, authorized: _AuthorizedAdd
    ) -> QbittorrentWriteBinding | TransmissionWriteBinding:
        try:
            binding = self._downloader_service.write_binding(authorized.target_downloader_id)
        except ApplicationError as exc:
            raise ApplicationError(
                code="ADDING_DOWNLOADER_CHANGED",
                status=409,
                title="目标下载器配置已变化",
                detail="execution plan 授权后的目标下载器已不可用于安全写入",
            ) from exc
        normalized_target = self._filesystem.normalize_relative_path(
            authorized.target_root,
            allow_root=True,
        )
        target_path = (
            self._data_root
            if normalized_target == "."
            else self._data_root.joinpath(*normalized_target.split("/"))
        )
        try:
            remote_save_path = binding.remote_save_path(target_path)
        except DomainViolation as exc:
            raise ApplicationError(
                code="ADDING_TARGET_MAPPING_CHANGED",
                status=409,
                title="目标下载器路径映射已变化",
                detail="target root 已无法按 execution plan 唯一映射到目标下载器",
            ) from exc
        if (
            binding.downloader_version != authorized.target_downloader_version
            or binding.binding_digest != authorized.target_downloader_binding_digest
            or remote_save_path != authorized.target_remote_save_path
        ):
            raise ApplicationError(
                code="ADDING_DOWNLOADER_CHANGED",
                status=409,
                title="目标下载器配置已变化",
                detail="execution plan 授权后的下载器版本、能力或路径映射已变化",
            )
        return binding

    async def _fetch_torrent(self, authorized: _AuthorizedAdd) -> bytes:
        bindings = tuple(
            item
            for item in self._site_service.enabled_adapters()
            if item.site_id == authorized.candidate_site_id
        )
        if len(bindings) != 1:
            raise ApplicationError(
                code="ADDING_SITE_UNAVAILABLE",
                status=409,
                title="候选站点不可唯一确定",
                detail="ADDING 要求批准候选对应且仅对应一个当前启用站点配置",
            )
        try:
            payload = await bindings[0].adapter.fetch_torrent(authorized.candidate_torrent_id)
        except SiteAdapterError as exc:
            raise ApplicationError(
                code="ADDING_TORRENT_FETCH_FAILED",
                status=502,
                title="ADDING 获取 torrent 失败",
                detail=f"站点适配器返回安全错误码：{exc.code}",
            ) from exc
        if (
            payload.site_id != authorized.candidate_site_id
            or payload.torrent_id != authorized.candidate_torrent_id
        ):
            raise ApplicationError(
                code="ADDING_TORRENT_IDENTITY_MISMATCH",
                status=409,
                title="ADDING torrent 身份不一致",
                detail="重新获取的 torrent 不属于 execution plan 批准候选",
            )
        try:
            meta = parse_torrent(payload.content)
        except DomainViolation as exc:
            raise ApplicationError(
                code="ADDING_TORRENT_INVALID",
                status=409,
                title="ADDING torrent 无法安全解析",
                detail=f"torrent 安全解析失败：{exc.code.value}",
            ) from exc
        if meta.metainfo_digest != authorized.metainfo_digest:
            raise ApplicationError(
                code="ADDING_TORRENT_CHANGED",
                status=409,
                title="候选 torrent 已变化",
                detail="ADDING 获取的 metainfo digest 与 execution plan 不一致",
            )
        return payload.content

    def _add_operation_known(self, operation_key: str) -> bool:
        with self._session_factory() as session:
            repository = OperationJournalRepository(session)
            return (
                repository.get_by_idempotency_key(operation_key) is not None
                or repository.get_tombstone_by_idempotency_key(operation_key) is not None
            )

    async def _find_existing_torrent(
        self,
        binding: QbittorrentWriteBinding | TransmissionWriteBinding,
        torrent_content: bytes,
    ) -> QbittorrentTorrentState | TransmissionTorrentState | None:
        meta = parse_torrent(torrent_content)
        expected_hashes = tuple(
            item.lower() for item in (meta.v1_info_hash, meta.v2_info_hash) if item is not None
        )
        if not expected_hashes:
            raise _adding_plan_invalid("候选 torrent 缺少可用于下载器去重的 info hash")
        try:
            observed = await binding.adapter.get_torrents(expected_hashes)
        except DownloaderAdapterError as exc:
            raise ApplicationError(
                code=exc.code,
                status=502,
                title="辅种前查询目标下载器失败",
                detail="无法确认目标下载器是否已存在相同 torrent，已停止本次辅种以避免重复添加",
            ) from exc
        if not observed:
            return None
        matching = tuple(item for item in observed if item.torrent_hash.lower() in expected_hashes)
        if len(matching) != 1:
            raise ApplicationError(
                code="ADDING_EXISTING_TORRENT_AMBIGUOUS",
                status=409,
                title="目标下载器中的 torrent 身份无法唯一确认",
                detail="辅种前发现多个匹配结果，已停止本次操作以避免误判或重复添加",
            )
        return matching[0]

    def _complete_existing_torrent(
        self,
        authorized: _AuthorizedAdd,
        state: QbittorrentTorrentState | TransmissionTorrentState,
        *,
        downloader_kind: DownloaderKind,
    ) -> TaskAddingResult:
        remote_save_path = (
            state.save_path if isinstance(state, QbittorrentTorrentState) else state.download_dir
        )
        checkpoint = {
            "schema_version": EXISTING_TORRENT_SKIP_CHECKPOINT_SCHEMA_VERSION,
            "stage": TaskStatus.DONE.value,
            "execution_plan_id": authorized.plan_id,
            "execution_plan_digest": authorized.plan_digest,
            "execution_gate_id": authorized.gate_id,
            "execution_gate_digest": authorized.gate_digest,
            "target_downloader_id": authorized.target_downloader_id,
            "target_downloader_version": authorized.target_downloader_version,
            "target_downloader_binding_digest": authorized.target_downloader_binding_digest,
            "downloader_kind": downloader_kind.value,
            "torrent_hash": state.torrent_hash,
            "remote_save_path": remote_save_path,
            "existing_torrent_skipped": True,
        }
        with self._session_factory() as session:
            repository = TaskRepository(session)
            task = repository.get(authorized.task_id)
            if (
                task is None
                or task.status != TaskStatus.ADDING.value
                or task.version != authorized.task_version
            ):
                raise _adding_task_state_invalid()
            try:
                task = repository.transition(
                    task_id=task.id,
                    expected_version=task.version,
                    to_status=TaskStatus.DONE,
                    event_type="SEEDING_SKIPPED_EXISTING_TORRENT",
                    reason=(
                        f"目标 {downloader_kind.value} 已存在相同 torrent；"
                        "已跳过重复辅种，未修改或认领下载器中的现有任务"
                    ),
                    checkpoint=checkpoint,
                )
            except DomainViolation as exc:
                raise _adding_task_state_invalid() from exc
            session.commit()
            return TaskAddingResult(
                task_id=task.id,
                task_version=task.version,
                status=TaskStatus.DONE,
                execution_plan_id=authorized.plan_id,
                qbit_journal_id=None,
                torrent_hash=state.torrent_hash,
                remote_save_path=remote_save_path,
                ownership_tag=None,
                skip_checking=False,
                replayed=False,
                recovered_after_unknown_result=False,
                skipped_existing=True,
            )

    def _complete_task(
        self,
        authorized: _AuthorizedAdd,
        result: QbittorrentAddOperationResult | TransmissionAddOperationResult,
        *,
        downloader_kind: DownloaderKind,
    ) -> TaskAddingResult:
        checkpoint = {
            "schema_version": POST_ADD_CHECKPOINT_SCHEMA_VERSION,
            "stage": "POST_ADD",
            "execution_plan_id": authorized.plan_id,
            "execution_plan_digest": authorized.plan_digest,
            "execution_gate_id": authorized.gate_id,
            "execution_gate_digest": authorized.gate_digest,
            "target_downloader_id": authorized.target_downloader_id,
            "target_downloader_version": authorized.target_downloader_version,
            "target_downloader_binding_digest": authorized.target_downloader_binding_digest,
            "target_remote_save_path": authorized.target_remote_save_path,
            "qbit_journal_id": result.journal_id,
            "add_journal_id": result.journal_id,
            "downloader_kind": downloader_kind.value,
            "torrent_hash": result.torrent_hash,
            "remote_save_path": result.save_path,
            "ownership_tag": result.ownership_tag,
            "verification_level": authorized.verification_level.value,
            "skip_checking": result.skip_checking,
        }
        with self._session_factory() as session:
            repository = TaskRepository(session)
            task = repository.get(authorized.task_id)
            if (
                task is None
                or task.status != TaskStatus.ADDING.value
                or task.version != authorized.task_version
            ):
                raise ApplicationError(
                    code="ADDING_TASK_CHANGED",
                    status=409,
                    title="ADDING 任务状态已经变化",
                    detail="下载器添加已登记完成，但任务状态无法安全推进，需要对账",
                )
            try:
                task = repository.transition_after_add(
                    task_id=task.id,
                    expected_version=task.version,
                    downloader=downloader_kind,
                    verification_level=authorized.verification_level,
                    skip_checking_enabled=result.skip_checking,
                    preflight_current=True,
                    event_type=(
                        "QBITTORRENT_ADD_CONFIRMED"
                        if downloader_kind is DownloaderKind.QBITTORRENT
                        else "TRANSMISSION_ADD_CONFIRMED"
                    ),
                    reason=(
                        "qBittorrent 暂停添加已由 operation journal 和实际状态共同确认"
                        if downloader_kind is DownloaderKind.QBITTORRENT
                        else "Transmission 暂停添加已由 operation journal 和实际状态共同确认"
                    ),
                    checkpoint=checkpoint,
                )
            except DomainViolation as exc:
                raise ApplicationError(
                    code="ADDING_TASK_CHANGED",
                    status=409,
                    title="ADDING 任务状态已经变化",
                    detail="下载器添加已登记完成，但任务状态无法安全推进，需要对账",
                ) from exc
            session.commit()
            status = TaskStatus(task.status)
            return TaskAddingResult(
                task_id=task.id,
                task_version=task.version,
                status=status,
                execution_plan_id=authorized.plan_id,
                qbit_journal_id=result.journal_id,
                torrent_hash=result.torrent_hash,
                remote_save_path=result.save_path,
                ownership_tag=result.ownership_tag,
                skip_checking=result.skip_checking,
                replayed=result.replayed,
                recovered_after_unknown_result=result.recovered_after_unknown_result,
                skipped_existing=False,
            )

    def _load_completed(self, unit_id: str, plan_id: str) -> TaskAddingResult | None:
        with self._session_factory() as session:
            plan = TaskExecutionPlanRepository(session).get(plan_id)
            if plan is None or plan.task_unit_id != unit_id:
                raise _adding_plan_not_found()
            task = TaskRepository(session).get(plan.task_id)
            if task is None:
                raise _adding_plan_not_found()
            try:
                status = TaskStatus(task.status)
            except ValueError as exc:
                raise _adding_plan_invalid("任务包含未知状态") from exc
            if status is TaskStatus.ADDING:
                return None
            if status not in {
                TaskStatus.SEEDING,
                TaskStatus.CLIENT_VERIFYING,
                TaskStatus.RETRY,
                TaskStatus.DONE,
            }:
                raise _adding_task_state_invalid()
            checkpoint = deepcopy(task.checkpoint)
            task_id = task.id
            task_version = task.version
            plan_record_id = plan.id
            plan_record_digest = plan.plan_digest
        schema = checkpoint.get("schema_version")
        if schema == EXISTING_TORRENT_SKIP_CHECKPOINT_SCHEMA_VERSION:
            if (
                status is not TaskStatus.DONE
                or checkpoint.get("stage") != TaskStatus.DONE.value
                or checkpoint.get("execution_plan_id") != plan_record_id
                or checkpoint.get("execution_plan_digest") != plan_record_digest
                or checkpoint.get("existing_torrent_skipped") is not True
            ):
                raise ApplicationError(
                    code="POST_ADD_CHECKPOINT_MISMATCH",
                    status=409,
                    title="重复辅种跳过检查点不匹配",
                    detail="当前任务状态不能证明已因目标下载器存在相同 torrent 而安全跳过",
                )
            return TaskAddingResult(
                task_id=task_id,
                task_version=task_version,
                status=status,
                execution_plan_id=plan_record_id,
                qbit_journal_id=None,
                torrent_hash=_required_text(checkpoint, "torrent_hash"),
                remote_save_path=_required_text(checkpoint, "remote_save_path"),
                ownership_tag=None,
                skip_checking=False,
                replayed=True,
                recovered_after_unknown_result=False,
                skipped_existing=True,
            )
        stage_matches = (
            (schema == POST_ADD_CHECKPOINT_SCHEMA_VERSION and checkpoint.get("stage") == "POST_ADD")
            or (
                schema == CLIENT_VERIFICATION_CHECKPOINT_SCHEMA_VERSION
                and checkpoint.get("stage") == status.value
            )
            or (
                schema == SEEDING_CHECKPOINT_SCHEMA_VERSION
                and checkpoint.get("stage") == status.value
            )
        )
        if (
            not stage_matches
            or checkpoint.get("execution_plan_id") != plan_record_id
            or checkpoint.get("execution_plan_digest") != plan_record_digest
        ):
            raise ApplicationError(
                code="POST_ADD_CHECKPOINT_MISMATCH",
                status=409,
                title="下载器添加完成检查点不匹配",
                detail="当前任务状态不能证明来自指定 execution plan 的已确认添加结果",
            )
        return TaskAddingResult(
            task_id=task_id,
            task_version=task_version,
            status=status,
            execution_plan_id=plan_record_id,
            qbit_journal_id=_required_add_journal_id(checkpoint),
            torrent_hash=_required_text(checkpoint, "torrent_hash"),
            remote_save_path=_required_text(checkpoint, "remote_save_path"),
            ownership_tag=_required_text(checkpoint, "ownership_tag"),
            skip_checking=_required_bool(checkpoint, "skip_checking"),
            replayed=True,
            recovered_after_unknown_result=False,
            skipped_existing=False,
        )


def _required_add_journal_id(payload: dict[str, object]) -> str:
    value = payload.get("add_journal_id")
    if value is None:
        return _required_text(payload, "qbit_journal_id")
    if not isinstance(value, str) or not value:
        raise _adding_plan_invalid("证据缺少有效 add_journal_id")
    return value


def _required_text(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise _adding_plan_invalid(f"证据缺少有效 {key}")
    return value


def _required_positive_int(payload: dict[str, object], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise _adding_plan_invalid(f"证据缺少有效 {key}")
    return value


def _required_bool(payload: dict[str, object], key: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise _adding_plan_invalid(f"证据缺少有效 {key}")
    return value


def _adding_plan_not_found() -> ApplicationError:
    return ApplicationError(
        code="ADDING_PLAN_NOT_FOUND",
        status=404,
        title="ADDING execution plan 不存在",
        detail="指定 execution plan 不存在或不属于当前 task unit",
    )


def _adding_plan_not_current() -> ApplicationError:
    return ApplicationError(
        code="ADDING_PLAN_NOT_CURRENT",
        status=409,
        title="ADDING execution plan 已失效",
        detail="只有 LINKING 已授权并完成的 latest execution plan 才能进入 qBittorrent 添加",
    )


def _adding_task_state_invalid() -> ApplicationError:
    return ApplicationError(
        code="ADDING_TASK_STATE_INVALID",
        status=409,
        title="任务状态不允许 qBittorrent 添加",
        detail=(
            "qBittorrent 添加只能从 ADDING 执行，或读取已进入 "
            "CLIENT_VERIFYING/SEEDING/RETRY/DONE 的既有添加结果"
        ),
    )


def _adding_plan_invalid(detail: str) -> ApplicationError:
    return ApplicationError(
        code="ADDING_PLAN_INVALID",
        status=409,
        title="ADDING execution plan 证据无效",
        detail=detail,
    )
