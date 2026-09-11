from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.downloader_operations import (
    QBITTORRENT_ADD_OPERATION,
    QBITTORRENT_RECHECK_OPERATION,
    QbittorrentStartOperationRequest,
    QbittorrentStartOperationResult,
    QbittorrentStartOperationService,
)
from backend.app.application.downloaders import QbittorrentWriteBinding, TransmissionWriteBinding
from backend.app.application.errors import ApplicationError
from backend.app.application.task_adding import (
    CLIENT_VERIFICATION_CHECKPOINT_SCHEMA_VERSION,
    POST_ADD_CHECKPOINT_SCHEMA_VERSION,
    SEEDING_CHECKPOINT_SCHEMA_VERSION,
)
from backend.app.application.transmission_operations import (
    TRANSMISSION_ADD_OPERATION,
    TRANSMISSION_VERIFY_OPERATION,
    TransmissionStartOperationRequest,
    TransmissionStartOperationResult,
    TransmissionStartOperationService,
)
from backend.app.domain.errors import DomainViolation
from backend.app.domain.execution_plan import EXECUTION_PLAN_SCHEMA_VERSION
from backend.app.domain.idempotency import candidate_execution_key
from backend.app.domain.operation import OperationStatus
from backend.app.domain.task_state import TaskStatus
from backend.app.domain.verification import DownloaderKind, VerificationLevel
from backend.app.infrastructure.persistence.models import OperationJournal, TaskExecutionPlanRecord
from backend.app.infrastructure.persistence.repositories import (
    OperationJournalRepository,
    TaskRepository,
)
from backend.app.infrastructure.persistence.task_analysis_repositories import (
    TaskCandidateRepository,
    TaskExecutionPlanRepository,
)
from backend.app.infrastructure.safe_filesystem import SafeFilesystemGateway
from backend.app.infrastructure.source_inventory import (
    scan_source_inventory,
    source_inventory_digest,
)


class DownloaderBindingProvider(Protocol):
    def write_binding(
        self, downloader_id: str
    ) -> QbittorrentWriteBinding | TransmissionWriteBinding: ...


@dataclass(frozen=True, slots=True)
class TaskSeedingResult:
    task_id: str
    task_version: int
    status: TaskStatus
    execution_plan_id: str
    start_journal_id: str
    torrent_hash: str
    client_state: str
    progress: float
    replayed: bool
    recovered_after_unknown_result: bool


@dataclass(frozen=True, slots=True)
class _AuthorizedSeeding:
    task_id: str
    task_version: int
    task_idempotency_key: str
    unit_id: str
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
    add_journal_id: str
    verification_journal_id: str | None
    downloader_kind: DownloaderKind
    torrent_hash: str
    ownership_tag: str
    checkpoint: dict[str, object]


class TaskSeedingCoordinator:
    """将已获准做种的 torrent 安全启动，并在真实做种状态确认后完成任务。"""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        downloader_service: DownloaderBindingProvider,
        start_operations: QbittorrentStartOperationService,
        transmission_start_operations: TransmissionStartOperationService | None = None,
        *,
        data_root: Path,
    ) -> None:
        self._session_factory = session_factory
        self._downloader_service = downloader_service
        self._start_operations = start_operations
        self._transmission_start_operations = transmission_start_operations
        self._data_root = data_root
        self._filesystem = SafeFilesystemGateway(data_root)

    async def execute(
        self,
        unit_id: str,
        *,
        execution_plan_id: str,
        fault_hook: Callable[[str], None] | None = None,
    ) -> TaskSeedingResult:
        completed = self._load_completed(unit_id, execution_plan_id)
        if completed is not None:
            return completed

        authorized = self._load_authorized(unit_id, execution_plan_id)
        self._assert_source_inventory_current(authorized)
        binding = self._load_target_binding(authorized)
        candidate_key = candidate_execution_key(
            task_key=authorized.task_idempotency_key,
            site_id=authorized.candidate_site_id,
            remote_torrent_id=authorized.candidate_torrent_id,
            target_downloader_id=authorized.target_downloader_id,
        )
        if authorized.downloader_kind is DownloaderKind.QBITTORRENT:
            if not isinstance(binding, QbittorrentWriteBinding):
                raise _seeding_binding_changed("目标下载器类型与 SEEDING checkpoint 不一致")
            result: (
                QbittorrentStartOperationResult | TransmissionStartOperationResult
            ) = await self._start_operations.execute(
                QbittorrentStartOperationRequest(
                    task_id=authorized.task_id,
                    candidate_key=candidate_key,
                    downloader_id=authorized.target_downloader_id,
                    downloader_version=authorized.target_downloader_version,
                    execution_plan_id=authorized.plan_id,
                    qbit_add_journal_id=authorized.add_journal_id,
                    verification_journal_id=authorized.verification_journal_id,
                    torrent_hash=authorized.torrent_hash,
                    remote_save_path=authorized.target_remote_save_path,
                    ownership_tag=authorized.ownership_tag,
                ),
                binding,
            )
            fault_stage = "after_start_applied"
        else:
            if not isinstance(binding, TransmissionWriteBinding):
                raise _seeding_binding_changed("目标下载器类型与 SEEDING checkpoint 不一致")
            if self._transmission_start_operations is None:
                raise ApplicationError(
                    code="SEEDING_DOWNLOADER_UNSUPPORTED",
                    status=409,
                    title="Transmission 做种启动服务未注册",
                    detail="当前运行时尚未注册 journal-backed Transmission start 服务",
                )
            if authorized.verification_journal_id is None:
                raise _seeding_evidence_invalid("Transmission SEEDING 必须绑定 verify journal")
            result = await self._transmission_start_operations.execute(
                TransmissionStartOperationRequest(
                    task_id=authorized.task_id,
                    candidate_key=candidate_key,
                    downloader_id=authorized.target_downloader_id,
                    downloader_version=authorized.target_downloader_version,
                    execution_plan_id=authorized.plan_id,
                    add_journal_id=authorized.add_journal_id,
                    verification_journal_id=authorized.verification_journal_id,
                    torrent_hash=authorized.torrent_hash,
                    remote_save_path=authorized.target_remote_save_path,
                    ownership_tag=authorized.ownership_tag,
                ),
                binding,
            )
            fault_stage = "after_transmission_start_applied"
        if fault_hook is not None:
            fault_hook(fault_stage)
        return self._complete_task(authorized, result)

    def _load_authorized(self, unit_id: str, plan_id: str) -> _AuthorizedSeeding:
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
                or plan.payload.get("schema_version") != EXECUTION_PLAN_SCHEMA_VERSION
            ):
                raise _seeding_plan_not_current()
            task = TaskRepository(session).get(plan.task_id)
            if task is None or task.status != TaskStatus.SEEDING.value:
                raise _seeding_task_state_invalid()
            checkpoint = deepcopy(task.checkpoint)
            self._assert_checkpoint_matches_plan(plan, checkpoint)

            candidate = TaskCandidateRepository(session).get(plan.candidate_id)
            if candidate is None or candidate.task_id != plan.task_id:
                raise _seeding_plan_not_current()
            downloader_kind = _checkpoint_downloader_kind(checkpoint)
            add_journal_id = _required_add_journal_id(checkpoint)
            add_journal = OperationJournalRepository(session).get(add_journal_id)
            self._assert_add_journal(plan, checkpoint, add_journal, downloader_kind)
            verification_journal_id = self._verification_journal_id(
                plan,
                checkpoint,
                session,
                downloader_kind,
                add_journal_id,
            )

            return _AuthorizedSeeding(
                task_id=task.id,
                task_version=task.version,
                task_idempotency_key=task.idempotency_key,
                unit_id=unit_id,
                plan_id=plan.id,
                plan_digest=plan.plan_digest,
                candidate_site_id=candidate.site_id,
                candidate_torrent_id=candidate.torrent_id,
                source_root=_required_text(plan.payload, "source_root"),
                source_inventory_digest=_required_text(plan.payload, "source_inventory_digest"),
                target_root=plan.target_root,
                target_device=plan.target_device,
                target_downloader_id=_required_text(checkpoint, "target_downloader_id"),
                target_downloader_version=_required_positive_int(
                    checkpoint, "target_downloader_version"
                ),
                target_downloader_binding_digest=_required_text(
                    checkpoint, "target_downloader_binding_digest"
                ),
                target_remote_save_path=_required_text(checkpoint, "target_remote_save_path"),
                add_journal_id=add_journal_id,
                verification_journal_id=verification_journal_id,
                downloader_kind=downloader_kind,
                torrent_hash=_required_text(checkpoint, "torrent_hash").lower(),
                ownership_tag=_required_text(checkpoint, "ownership_tag"),
                checkpoint=checkpoint,
            )

    def _assert_checkpoint_matches_plan(
        self,
        plan: TaskExecutionPlanRecord,
        checkpoint: dict[str, object],
    ) -> None:
        schema = checkpoint.get("schema_version")
        downloader_kind = _checkpoint_downloader_kind(checkpoint)
        if schema not in {
            POST_ADD_CHECKPOINT_SCHEMA_VERSION,
            CLIENT_VERIFICATION_CHECKPOINT_SCHEMA_VERSION,
        }:
            raise _seeding_evidence_invalid("SEEDING checkpoint schema 无效")
        if (
            checkpoint.get("execution_plan_id") != plan.id
            or checkpoint.get("execution_plan_digest") != plan.plan_digest
            or checkpoint.get("target_downloader_id") != plan.payload.get("target_downloader_id")
            or checkpoint.get("target_downloader_version")
            != plan.payload.get("target_downloader_version")
            or checkpoint.get("target_downloader_binding_digest")
            != plan.payload.get("target_downloader_binding_digest")
            or checkpoint.get("target_remote_save_path")
            != plan.payload.get("target_remote_save_path")
            or checkpoint.get("remote_save_path") != plan.payload.get("target_remote_save_path")
        ):
            raise _seeding_evidence_invalid("SEEDING checkpoint 与 execution plan 不匹配")

        if schema == POST_ADD_CHECKPOINT_SCHEMA_VERSION:
            if (
                downloader_kind is not DownloaderKind.QBITTORRENT
                or checkpoint.get("stage") != "POST_ADD"
                or _required_bool(checkpoint, "skip_checking") is not True
                or checkpoint.get("verification_level") != VerificationLevel.FULL_VERIFIED.value
            ):
                raise _seeding_evidence_invalid("跳过客户端校验的 SEEDING 证据无效")
            return

        if (
            checkpoint.get("stage") != TaskStatus.SEEDING.value
            or _required_bool(checkpoint, "skip_checking") is not False
            or checkpoint.get("verification_outcome") != "VERIFIED"
            or _required_progress(checkpoint, "client_progress") != 1.0
        ):
            raise _seeding_evidence_invalid("客户端完整校验后的 SEEDING 证据无效")

    def _assert_add_journal(
        self,
        plan: TaskExecutionPlanRecord,
        checkpoint: dict[str, object],
        journal: OperationJournal | None,
        downloader_kind: DownloaderKind,
    ) -> None:
        expected_operation = (
            QBITTORRENT_ADD_OPERATION
            if downloader_kind is DownloaderKind.QBITTORRENT
            else TRANSMISSION_ADD_OPERATION
        )
        if (
            journal is None
            or journal.task_id != plan.task_id
            or journal.operation_type != expected_operation
            or OperationStatus(journal.status) is not OperationStatus.APPLIED
            or journal.intent.get("execution_plan_id") != plan.id
            or journal.target.get("downloader_id") != checkpoint.get("target_downloader_id")
            or journal.after_snapshot is None
            or journal.after_snapshot.get("torrent_hash") != checkpoint.get("torrent_hash")
            or journal.after_snapshot.get("save_path") != checkpoint.get("remote_save_path")
            or journal.after_snapshot.get("ownership_tag") != checkpoint.get("ownership_tag")
        ):
            raise _seeding_evidence_invalid("下载器 add journal 无法证明当前 torrent 归属")

    def _verification_journal_id(
        self,
        plan: TaskExecutionPlanRecord,
        checkpoint: dict[str, object],
        session: Session,
        downloader_kind: DownloaderKind,
        add_journal_id: str,
    ) -> str | None:
        if checkpoint.get("schema_version") == POST_ADD_CHECKPOINT_SCHEMA_VERSION:
            return None
        journal_id = _required_verification_journal_id(checkpoint)
        journal = OperationJournalRepository(session).get(journal_id)
        expected_operation = (
            QBITTORRENT_RECHECK_OPERATION
            if downloader_kind is DownloaderKind.QBITTORRENT
            else TRANSMISSION_VERIFY_OPERATION
        )
        add_reference_key = (
            "qbit_add_journal_id"
            if downloader_kind is DownloaderKind.QBITTORRENT
            else "add_journal_id"
        )
        if (
            journal is None
            or journal.task_id != plan.task_id
            or journal.operation_type != expected_operation
            or OperationStatus(journal.status) is not OperationStatus.APPLIED
            or journal.intent.get("execution_plan_id") != plan.id
            or journal.intent.get(add_reference_key) != add_journal_id
            or journal.intent.get("torrent_hash") != checkpoint.get("torrent_hash")
            or journal.intent.get("remote_save_path") != checkpoint.get("remote_save_path")
            or journal.intent.get("ownership_tag") != checkpoint.get("ownership_tag")
        ):
            raise _seeding_evidence_invalid("客户端校验 journal 无法证明完整校验链")
        return journal_id

    def _load_target_binding(
        self, authorized: _AuthorizedSeeding
    ) -> QbittorrentWriteBinding | TransmissionWriteBinding:
        try:
            self._filesystem.assert_directory(
                relative_path=authorized.target_root,
                expected_device=authorized.target_device,
            )
            binding = self._downloader_service.write_binding(authorized.target_downloader_id)
        except (ApplicationError, DomainViolation) as exc:
            raise _seeding_binding_changed(
                "target root 或目标下载器已不能证明与 execution plan 一致"
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
            raise _seeding_binding_changed(
                "target root 已无法唯一反向映射到计划中的下载器 save path"
            ) from exc
        if (
            (
                authorized.downloader_kind is DownloaderKind.QBITTORRENT
                and not isinstance(binding, QbittorrentWriteBinding)
            )
            or (
                authorized.downloader_kind is DownloaderKind.TRANSMISSION
                and not isinstance(binding, TransmissionWriteBinding)
            )
            or binding.downloader_version != authorized.target_downloader_version
            or binding.binding_digest != authorized.target_downloader_binding_digest
            or remote_save_path != authorized.target_remote_save_path
        ):
            raise _seeding_binding_changed(
                "下载器类型、version、binding digest 或 save path 与 execution plan 不一致"
            )
        return binding

    def _assert_source_inventory_current(self, authorized: _AuthorizedSeeding) -> None:
        normalized = self._filesystem.normalize_relative_path(
            authorized.source_root,
            allow_root=True,
        )
        source_path = (
            self._data_root
            if normalized == "."
            else self._data_root.joinpath(*normalized.split("/"))
        )
        try:
            observed = source_inventory_digest(scan_source_inventory(source_path))
        except DomainViolation as exc:
            raise ApplicationError(
                code="SEEDING_SOURCE_UNAVAILABLE",
                status=409,
                title="SEEDING 源目录不可用",
                detail="开始做种前无法重新确认 source inventory",
            ) from exc
        if observed != authorized.source_inventory_digest:
            raise ApplicationError(
                code="SEEDING_SOURCE_CHANGED",
                status=409,
                title="SEEDING 源目录已变化",
                detail="开始做种前 source inventory 已变化，禁止启动下载器 torrent",
            )

    def _complete_task(
        self,
        authorized: _AuthorizedSeeding,
        result: QbittorrentStartOperationResult | TransmissionStartOperationResult,
    ) -> TaskSeedingResult:
        if not result.seeding or result.progress != 1.0:
            raise _seeding_evidence_invalid("下载器 start 未证明 torrent 已进入完整做种状态")
        checkpoint = deepcopy(authorized.checkpoint)
        checkpoint.update(
            {
                "schema_version": SEEDING_CHECKPOINT_SCHEMA_VERSION,
                "stage": TaskStatus.DONE.value,
                "start_journal_id": result.journal_id,
                "client_state": result.state,
                "client_progress": result.progress,
                "seeding_confirmed": True,
            }
        )
        with self._session_factory() as session:
            repository = TaskRepository(session)
            task = repository.get(authorized.task_id)
            if (
                task is None
                or task.status != TaskStatus.SEEDING.value
                or task.version != authorized.task_version
            ):
                raise _seeding_task_changed()
            try:
                task = repository.transition(
                    task_id=task.id,
                    expected_version=task.version,
                    to_status=TaskStatus.DONE,
                    event_type=(
                        "QBITTORRENT_SEEDING_CONFIRMED"
                        if authorized.downloader_kind is DownloaderKind.QBITTORRENT
                        else "TRANSMISSION_SEEDING_CONFIRMED"
                    ),
                    reason=(
                        "qBittorrent 已确认 progress=1 且进入上行做种状态，任务完成"
                        if authorized.downloader_kind is DownloaderKind.QBITTORRENT
                        else "Transmission 已确认 percent_done=1 且进入做种状态，任务完成"
                    ),
                    checkpoint=checkpoint,
                )
            except DomainViolation as exc:
                raise _seeding_task_changed() from exc
            session.commit()
            return TaskSeedingResult(
                task_id=task.id,
                task_version=task.version,
                status=TaskStatus.DONE,
                execution_plan_id=authorized.plan_id,
                start_journal_id=result.journal_id,
                torrent_hash=result.torrent_hash,
                client_state=result.state,
                progress=result.progress,
                replayed=result.replayed,
                recovered_after_unknown_result=result.recovered_after_unknown_result,
            )

    def _load_completed(self, unit_id: str, plan_id: str) -> TaskSeedingResult | None:
        with self._session_factory() as session:
            plan = TaskExecutionPlanRepository(session).get(plan_id)
            if plan is None or plan.task_unit_id != unit_id:
                raise _seeding_plan_not_found()
            task = TaskRepository(session).get(plan.task_id)
            if task is None:
                raise _seeding_plan_not_found()
            status = TaskStatus(task.status)
            if status is TaskStatus.SEEDING:
                return None
            if status is not TaskStatus.DONE:
                raise _seeding_task_state_invalid()
            checkpoint = deepcopy(task.checkpoint)
            task_id = task.id
            task_version = task.version
            plan_record_id = plan.id
            plan_record_digest = plan.plan_digest
        if (
            checkpoint.get("schema_version") != SEEDING_CHECKPOINT_SCHEMA_VERSION
            or checkpoint.get("stage") != TaskStatus.DONE.value
            or checkpoint.get("execution_plan_id") != plan_record_id
            or checkpoint.get("execution_plan_digest") != plan_record_digest
            or _required_bool(checkpoint, "seeding_confirmed") is not True
        ):
            raise _seeding_task_state_invalid()
        return TaskSeedingResult(
            task_id=task_id,
            task_version=task_version,
            status=TaskStatus.DONE,
            execution_plan_id=plan_record_id,
            start_journal_id=_required_text(checkpoint, "start_journal_id"),
            torrent_hash=_required_text(checkpoint, "torrent_hash"),
            client_state=_required_text(checkpoint, "client_state"),
            progress=_required_progress(checkpoint, "client_progress"),
            replayed=True,
            recovered_after_unknown_result=False,
        )


def _required_add_journal_id(payload: dict[str, object]) -> str:
    value = payload.get("add_journal_id")
    if value is None:
        return _required_text(payload, "qbit_journal_id")
    if not isinstance(value, str) or not value:
        raise _seeding_evidence_invalid("SEEDING 证据缺少有效 add_journal_id")
    return value


def _required_verification_journal_id(payload: dict[str, object]) -> str:
    value = payload.get("verification_journal_id")
    if value is None:
        return _required_text(payload, "recheck_journal_id")
    if not isinstance(value, str) or not value:
        raise _seeding_evidence_invalid("SEEDING 证据缺少有效 verification_journal_id")
    return value


def _checkpoint_downloader_kind(checkpoint: dict[str, object]) -> DownloaderKind:
    value = checkpoint.get("downloader_kind")
    if value is None:
        return DownloaderKind.QBITTORRENT
    if not isinstance(value, str):
        raise _seeding_evidence_invalid("SEEDING downloader_kind 无效")
    try:
        kind = DownloaderKind(value)
    except ValueError as exc:
        raise _seeding_evidence_invalid("SEEDING downloader_kind 无效") from exc
    if kind not in {DownloaderKind.QBITTORRENT, DownloaderKind.TRANSMISSION}:
        raise _seeding_evidence_invalid("SEEDING downloader_kind 不受支持")
    return kind


def _required_text(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise _seeding_evidence_invalid(f"SEEDING 证据缺少有效 {key}")
    return value


def _required_positive_int(payload: dict[str, object], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise _seeding_evidence_invalid(f"SEEDING 证据缺少有效 {key}")
    return value


def _required_bool(payload: dict[str, object], key: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise _seeding_evidence_invalid(f"SEEDING 证据缺少有效 {key}")
    return value


def _required_progress(payload: dict[str, object], key: str) -> float:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _seeding_evidence_invalid(f"SEEDING 证据缺少有效 {key}")
    normalized = float(value)
    if not 0.0 <= normalized <= 1.0:
        raise _seeding_evidence_invalid(f"SEEDING {key} 超出 0..1")
    return normalized


def _seeding_plan_not_found() -> ApplicationError:
    return ApplicationError(
        code="SEEDING_PLAN_NOT_FOUND",
        status=404,
        title="SEEDING execution plan 不存在",
        detail="指定 execution plan 不存在或不属于当前 task unit",
    )


def _seeding_plan_not_current() -> ApplicationError:
    return ApplicationError(
        code="SEEDING_PLAN_NOT_CURRENT",
        status=409,
        title="SEEDING execution plan 已失效",
        detail="开始做种只能继续 LINKING/ADDING 已兑现的 latest ready execution plan",
    )


def _seeding_task_state_invalid() -> ApplicationError:
    return ApplicationError(
        code="SEEDING_TASK_STATE_INVALID",
        status=409,
        title="任务状态不允许启动做种",
        detail="启动做种只能从 SEEDING 执行，或读取其 DONE 终态结果",
    )


def _seeding_evidence_invalid(detail: str) -> ApplicationError:
    return ApplicationError(
        code="SEEDING_EVIDENCE_INVALID",
        status=409,
        title="SEEDING 恢复证据无效",
        detail=detail,
    )


def _seeding_binding_changed(detail: str) -> ApplicationError:
    return ApplicationError(
        code="SEEDING_BINDING_CHANGED",
        status=409,
        title="SEEDING 目标环境已变化",
        detail=detail,
    )


def _seeding_task_changed() -> ApplicationError:
    return ApplicationError(
        code="SEEDING_TASK_CHANGED",
        status=409,
        title="SEEDING 任务状态已经变化",
        detail="下载器 start 已确认，但原 task version 无法安全推进，需要重新读取对账",
    )
