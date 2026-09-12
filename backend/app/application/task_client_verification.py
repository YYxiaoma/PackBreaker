from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.downloader_operations import (
    QBITTORRENT_ADD_OPERATION,
    QbittorrentRecheckOperationRequest,
    QbittorrentRecheckOperationResult,
    QbittorrentRecheckOperationService,
)
from backend.app.application.downloaders import QbittorrentWriteBinding, TransmissionWriteBinding
from backend.app.application.errors import ApplicationError
from backend.app.application.repair_downloader_operations import (
    RepairDownloadOperationResult,
    RepairDownloadOperationService,
    RepairDownloadStartRequest,
    RepairDownloadStopRequest,
)
from backend.app.application.task_adding import (
    CLIENT_VERIFICATION_CHECKPOINT_SCHEMA_VERSION,
    POST_ADD_CHECKPOINT_SCHEMA_VERSION,
    SEEDING_CHECKPOINT_SCHEMA_VERSION,
)
from backend.app.application.task_repairs import (
    REPAIR_DOWNLOAD_CHECKPOINT_SCHEMA_VERSION,
    REPAIR_STAGE_DOWNLOAD_PENDING,
    REPAIR_STAGE_DOWNLOADING,
    REPAIR_STAGE_INCOMPLETE,
    REPAIR_STAGE_RECHECK_PENDING,
    REPAIR_STAGE_RECHECKING,
    REPAIR_STAGE_VERIFIED,
)
from backend.app.application.transmission_operations import (
    TRANSMISSION_ADD_OPERATION,
    TransmissionVerifyOperationRequest,
    TransmissionVerifyOperationResult,
    TransmissionVerifyOperationService,
)
from backend.app.domain.errors import DomainViolation
from backend.app.domain.execution_plan import EXECUTION_PLAN_SCHEMA_VERSION
from backend.app.domain.idempotency import candidate_execution_key
from backend.app.domain.operation import OperationStatus
from backend.app.domain.task_state import TaskStatus
from backend.app.domain.verification import DownloaderKind
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


class DownloaderBindingProvider(Protocol):
    def write_binding(
        self, downloader_id: str
    ) -> QbittorrentWriteBinding | TransmissionWriteBinding: ...


@dataclass(frozen=True, slots=True)
class TaskClientVerificationResult:
    task_id: str
    task_version: int
    status: TaskStatus
    execution_plan_id: str
    recheck_journal_id: str
    torrent_hash: str
    client_state: str
    progress: float
    verification_outcome: str
    checking_observed: bool
    replayed: bool
    recovered_after_unknown_result: bool


@dataclass(frozen=True, slots=True)
class _AuthorizedVerification:
    task_id: str
    task_version: int
    task_idempotency_key: str
    unit_id: str
    plan_id: str
    plan_digest: str
    candidate_site_id: str
    candidate_torrent_id: str
    target_root: str
    target_device: int
    target_downloader_id: str
    target_downloader_version: int
    target_downloader_binding_digest: str
    target_remote_save_path: str
    add_journal_id: str
    downloader_kind: DownloaderKind
    torrent_hash: str
    ownership_tag: str
    checkpoint: dict[str, object]
    checking_observed: bool


class TaskClientVerificationCoordinator:
    """CLIENT_VERIFYING 单步 tick；不长轮询、不盲目重复校验，只凭真实下载器状态推进。"""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        downloader_service: DownloaderBindingProvider,
        recheck_operations: QbittorrentRecheckOperationService,
        transmission_verify_operations: TransmissionVerifyOperationService | None = None,
        repair_download_operations: RepairDownloadOperationService | None = None,
        *,
        data_root: Path,
    ) -> None:
        self._session_factory = session_factory
        self._downloader_service = downloader_service
        self._recheck_operations = recheck_operations
        self._transmission_verify_operations = transmission_verify_operations
        self._repair_download_operations = repair_download_operations
        self._data_root = data_root
        self._filesystem = SafeFilesystemGateway(data_root)

    async def execute(
        self,
        unit_id: str,
        *,
        execution_plan_id: str,
        fault_hook: Callable[[str], None] | None = None,
    ) -> TaskClientVerificationResult:
        completed = self._load_completed(unit_id, execution_plan_id)
        if completed is not None:
            return completed

        authorized = self._load_authorized(unit_id, execution_plan_id)
        binding = self._load_target_binding(authorized)
        repair_stage = _repair_stage(authorized.checkpoint)
        if repair_stage in {REPAIR_STAGE_DOWNLOAD_PENDING, REPAIR_STAGE_DOWNLOADING}:
            return await self._execute_repair_download(
                authorized,
                binding,
                fault_hook=fault_hook,
            )
        candidate_key = (
            _required_digest(authorized.checkpoint, "repair_candidate_key")
            if repair_stage in {REPAIR_STAGE_RECHECK_PENDING, REPAIR_STAGE_RECHECKING}
            else candidate_execution_key(
                task_key=authorized.task_idempotency_key,
                site_id=authorized.candidate_site_id,
                remote_torrent_id=authorized.candidate_torrent_id,
                target_downloader_id=authorized.target_downloader_id,
            )
        )
        if authorized.downloader_kind is DownloaderKind.QBITTORRENT:
            if not isinstance(binding, QbittorrentWriteBinding):
                raise _verification_binding_changed("目标下载器类型与 ADDING checkpoint 不一致")
            result: (
                QbittorrentRecheckOperationResult | TransmissionVerifyOperationResult
            ) = await self._recheck_operations.execute(
                QbittorrentRecheckOperationRequest(
                    task_id=authorized.task_id,
                    candidate_key=candidate_key,
                    downloader_id=authorized.target_downloader_id,
                    downloader_version=authorized.target_downloader_version,
                    execution_plan_id=authorized.plan_id,
                    qbit_add_journal_id=authorized.add_journal_id,
                    torrent_hash=authorized.torrent_hash,
                    remote_save_path=authorized.target_remote_save_path,
                    ownership_tag=authorized.ownership_tag,
                ),
                binding,
            )
            fault_stage = "after_recheck_applied"
            client_name = "qBittorrent"
        else:
            if not isinstance(binding, TransmissionWriteBinding):
                raise _verification_binding_changed("目标下载器类型与 ADDING checkpoint 不一致")
            if self._transmission_verify_operations is None:
                raise ApplicationError(
                    code="CLIENT_VERIFYING_DOWNLOADER_UNSUPPORTED",
                    status=409,
                    title="Transmission 校验服务未注册",
                    detail="当前运行时尚未注册 journal-backed Transmission verify 服务",
                )
            result = await self._transmission_verify_operations.execute(
                TransmissionVerifyOperationRequest(
                    task_id=authorized.task_id,
                    candidate_key=candidate_key,
                    downloader_id=authorized.target_downloader_id,
                    downloader_version=authorized.target_downloader_version,
                    execution_plan_id=authorized.plan_id,
                    add_journal_id=authorized.add_journal_id,
                    torrent_hash=authorized.torrent_hash,
                    remote_save_path=authorized.target_remote_save_path,
                    ownership_tag=authorized.ownership_tag,
                ),
                binding,
            )
            fault_stage = "after_transmission_verify_applied"
            client_name = "Transmission"
        if fault_hook is not None:
            fault_hook(fault_stage)

        checking_observed = authorized.checking_observed or result.checking_observed
        if result.checking:
            return self._record_progress(
                authorized,
                result,
                checking_observed=True,
                outcome="CHECKING",
            )
        if result.verification_complete and (checking_observed or result.completion_proven):
            return self._finish(
                authorized,
                result,
                to_status=TaskStatus.SEEDING,
                checking_observed=checking_observed,
                outcome="VERIFIED",
                reason=f"{client_name} 完整客户端校验已确认 100%，进入 SEEDING",
            )
        if result.verification_incomplete and checking_observed:
            return self._finish(
                authorized,
                result,
                to_status=TaskStatus.RETRY,
                checking_observed=True,
                outcome="INCOMPLETE",
                reason=f"{client_name} 客户端校验已结束但内容未达到 100%，转入 RETRY",
            )
        if result.verification_complete or result.verification_incomplete:
            return self._record_progress(
                authorized,
                result,
                checking_observed=checking_observed,
                outcome="AWAITING_CHECK_EVIDENCE",
            )
        raise ApplicationError(
            code="CLIENT_VERIFYING_STATE_UNSAFE",
            status=409,
            title="客户端下载器校验状态不可安全解释",
            detail="torrent 离开 checking 后既不是停止且完整，也不是停止且不完整；禁止自动推进",
        )

    async def _execute_repair_download(
        self,
        authorized: _AuthorizedVerification,
        binding: QbittorrentWriteBinding | TransmissionWriteBinding,
        *,
        fault_hook: Callable[[str], None] | None,
    ) -> TaskClientVerificationResult:
        operations = self._repair_download_operations
        if operations is None:
            raise ApplicationError(
                code="REPAIR_DOWNLOAD_SERVICE_UNAVAILABLE",
                status=409,
                title="修复下载服务未注册",
                detail="当前运行时尚未注册 journal-backed repair download start/stop 服务",
            )
        checkpoint = authorized.checkpoint
        candidate_key = _required_digest(checkpoint, "repair_candidate_key")
        evidence_digest = _required_digest(checkpoint, "repair_evidence_digest")
        source_verification_journal_id = _required_text(
            checkpoint,
            "repair_source_verification_journal_id",
        )
        start = await operations.start(
            RepairDownloadStartRequest(
                task_id=authorized.task_id,
                candidate_key=candidate_key,
                downloader_id=authorized.target_downloader_id,
                downloader_version=authorized.target_downloader_version,
                execution_plan_id=authorized.plan_id,
                add_journal_id=authorized.add_journal_id,
                source_verification_journal_id=source_verification_journal_id,
                torrent_hash=authorized.torrent_hash,
                remote_save_path=authorized.target_remote_save_path,
                ownership_tag=authorized.ownership_tag,
                repair_evidence_digest=evidence_digest,
            ),
            binding,
        )
        if fault_hook is not None:
            fault_hook("after_repair_download_started")

        if not start.complete:
            if start.stopped:
                raise ApplicationError(
                    code="REPAIR_DOWNLOAD_STOPPED_INCOMPLETE",
                    status=409,
                    title="修复下载在补齐完成前停止",
                    detail=(
                        "repair start journal 已 APPLIED，但 owned torrent 当前停止且仍不完整；"
                        "禁止把外部停止状态自动解释为可再次 start"
                    ),
                )
            return self._record_repair_progress(
                authorized,
                start,
                repair_stage=REPAIR_STAGE_DOWNLOADING,
                outcome="REPAIR_DOWNLOADING",
                repair_start_journal_id=start.journal_id,
                repair_stop_journal_id=None,
                event_type="REPAIR_DOWNLOAD_PROGRESS",
                reason="修复下载已启动，等待客户端下载补齐受影响数据",
            )

        stop = await operations.stop(
            RepairDownloadStopRequest(
                task_id=authorized.task_id,
                candidate_key=candidate_key,
                downloader_id=authorized.target_downloader_id,
                downloader_version=authorized.target_downloader_version,
                execution_plan_id=authorized.plan_id,
                add_journal_id=authorized.add_journal_id,
                source_verification_journal_id=source_verification_journal_id,
                repair_start_journal_id=start.journal_id,
                torrent_hash=authorized.torrent_hash,
                remote_save_path=authorized.target_remote_save_path,
                ownership_tag=authorized.ownership_tag,
                repair_evidence_digest=evidence_digest,
            ),
            binding,
        )
        stop_journal_id = stop.journal_id
        if fault_hook is not None:
            fault_hook("after_repair_download_stopped")
        if not stop.complete or not stop.stopped:
            raise ApplicationError(
                code="REPAIR_DOWNLOAD_STOP_STATE_UNSAFE",
                status=409,
                title="修复下载停止结果不可安全确认",
                detail="进入第二轮客户端校验前必须由真实状态证明 torrent 已停止且完整",
            )
        return self._record_repair_progress(
            authorized,
            stop,
            repair_stage=REPAIR_STAGE_RECHECK_PENDING,
            outcome="REPAIR_RECHECK_PENDING",
            repair_start_journal_id=start.journal_id,
            repair_stop_journal_id=stop_journal_id,
            event_type="REPAIR_DOWNLOAD_COMPLETED",
            reason="客户端下载已补齐到 100% 并停止；下一 tick 将启动独立第二轮完整校验",
        )

    def _record_repair_progress(
        self,
        authorized: _AuthorizedVerification,
        result: RepairDownloadOperationResult,
        *,
        repair_stage: str,
        outcome: str,
        repair_start_journal_id: str,
        repair_stop_journal_id: str | None,
        event_type: str,
        reason: str,
    ) -> TaskClientVerificationResult:
        checkpoint = deepcopy(authorized.checkpoint)
        checkpoint.update(
            {
                "schema_version": CLIENT_VERIFICATION_CHECKPOINT_SCHEMA_VERSION,
                "stage": TaskStatus.CLIENT_VERIFYING.value,
                "client_state": result.state,
                "client_progress": result.progress,
                "checking_observed": False,
                "verification_outcome": outcome,
                "repair_schema_version": REPAIR_DOWNLOAD_CHECKPOINT_SCHEMA_VERSION,
                "repair_stage": repair_stage,
                "repair_start_journal_id": repair_start_journal_id,
                "repair_stop_journal_id": repair_stop_journal_id,
            }
        )
        with self._session_factory() as session:
            repository = TaskRepository(session)
            task = repository.get(authorized.task_id)
            if (
                task is None
                or task.status != TaskStatus.CLIENT_VERIFYING.value
                or task.version != authorized.task_version
            ):
                raise _verification_task_changed()
            if task.checkpoint == checkpoint:
                return TaskClientVerificationResult(
                    task_id=task.id,
                    task_version=task.version,
                    status=TaskStatus.CLIENT_VERIFYING,
                    execution_plan_id=authorized.plan_id,
                    recheck_journal_id=_required_text(
                        checkpoint,
                        "repair_source_verification_journal_id",
                    ),
                    torrent_hash=result.torrent_hash,
                    client_state=result.state,
                    progress=result.progress,
                    verification_outcome=outcome,
                    checking_observed=False,
                    replayed=True,
                    recovered_after_unknown_result=result.recovered_after_unknown_result,
                )
            try:
                task = repository.record_checkpoint(
                    task_id=task.id,
                    expected_version=task.version,
                    expected_status=TaskStatus.CLIENT_VERIFYING,
                    checkpoint=checkpoint,
                    event_type=event_type,
                    reason=reason,
                )
            except DomainViolation as exc:
                raise _verification_task_changed() from exc
            session.commit()
            return TaskClientVerificationResult(
                task_id=task.id,
                task_version=task.version,
                status=TaskStatus.CLIENT_VERIFYING,
                execution_plan_id=authorized.plan_id,
                recheck_journal_id=_required_text(
                    checkpoint,
                    "repair_source_verification_journal_id",
                ),
                torrent_hash=result.torrent_hash,
                client_state=result.state,
                progress=result.progress,
                verification_outcome=outcome,
                checking_observed=False,
                replayed=result.replayed,
                recovered_after_unknown_result=result.recovered_after_unknown_result,
            )

    def _load_authorized(self, unit_id: str, plan_id: str) -> _AuthorizedVerification:
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
                raise _verification_plan_not_current()
            task = TaskRepository(session).get(plan.task_id)
            if task is None or task.status != TaskStatus.CLIENT_VERIFYING.value:
                raise _verification_task_state_invalid()
            checkpoint = deepcopy(task.checkpoint)
            self._assert_checkpoint_matches_plan(plan, checkpoint)
            if _required_bool(checkpoint, "skip_checking"):
                raise _verification_evidence_invalid(
                    "CLIENT_VERIFYING checkpoint 不能声明 skip_checking"
                )

            candidate = TaskCandidateRepository(session).get(plan.candidate_id)
            if candidate is None or candidate.task_id != plan.task_id:
                raise _verification_plan_not_current()
            downloader_kind = _checkpoint_downloader_kind(checkpoint)
            add_journal_id = _required_add_journal_id(checkpoint)
            add_journal = OperationJournalRepository(session).get(add_journal_id)
            self._assert_add_journal(plan, checkpoint, add_journal, downloader_kind)

            return _AuthorizedVerification(
                task_id=task.id,
                task_version=task.version,
                task_idempotency_key=task.idempotency_key,
                unit_id=unit_id,
                plan_id=plan.id,
                plan_digest=plan.plan_digest,
                candidate_site_id=candidate.site_id,
                candidate_torrent_id=candidate.torrent_id,
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
                downloader_kind=downloader_kind,
                torrent_hash=_required_text(checkpoint, "torrent_hash").lower(),
                ownership_tag=_required_text(checkpoint, "ownership_tag"),
                checkpoint=checkpoint,
                checking_observed=_optional_bool(checkpoint, "checking_observed"),
            )

    def _assert_checkpoint_matches_plan(
        self,
        plan: TaskExecutionPlanRecord,
        checkpoint: dict[str, object],
    ) -> None:
        schema = checkpoint.get("schema_version")
        if schema not in {
            POST_ADD_CHECKPOINT_SCHEMA_VERSION,
            CLIENT_VERIFICATION_CHECKPOINT_SCHEMA_VERSION,
        }:
            raise _verification_evidence_invalid("CLIENT_VERIFYING checkpoint schema 无效")
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
        ):
            raise _verification_evidence_invalid(
                "CLIENT_VERIFYING checkpoint 与 execution plan 不匹配"
            )
        if (
            schema == CLIENT_VERIFICATION_CHECKPOINT_SCHEMA_VERSION
            and checkpoint.get("stage") != TaskStatus.CLIENT_VERIFYING.value
        ):
            raise _verification_evidence_invalid("CLIENT_VERIFYING checkpoint stage 无效")
        if schema == POST_ADD_CHECKPOINT_SCHEMA_VERSION and checkpoint.get("stage") != "POST_ADD":
            raise _verification_evidence_invalid("POST_ADD checkpoint stage 无效")

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
            raise _verification_evidence_invalid("下载器 add journal 无法证明当前 torrent 归属")

    def _load_target_binding(
        self,
        authorized: _AuthorizedVerification,
    ) -> QbittorrentWriteBinding | TransmissionWriteBinding:
        try:
            self._filesystem.assert_directory(
                relative_path=authorized.target_root,
                expected_device=authorized.target_device,
            )
            binding = self._downloader_service.write_binding(authorized.target_downloader_id)
        except (ApplicationError, DomainViolation) as exc:
            raise ApplicationError(
                code="CLIENT_VERIFYING_BINDING_CHANGED",
                status=409,
                title="CLIENT_VERIFYING 目标环境已变化",
                detail="target root 或目标下载器已不能证明与 execution plan 一致",
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
                code="CLIENT_VERIFYING_BINDING_CHANGED",
                status=409,
                title="CLIENT_VERIFYING 路径映射已变化",
                detail="target root 已无法唯一反向映射到计划中的下载器 save path",
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
            or binding.capabilities.get("supports_force_recheck") is not True
            or binding.capabilities.get("supports_verify_progress") is not True
        ):
            raise ApplicationError(
                code="CLIENT_VERIFYING_BINDING_CHANGED",
                status=409,
                title="CLIENT_VERIFYING 下载器能力已变化",
                detail="下载器 version、binding digest、save path 或客户端校验能力与计划不一致",
            )
        return binding

    def _record_progress(
        self,
        authorized: _AuthorizedVerification,
        result: QbittorrentRecheckOperationResult | TransmissionVerifyOperationResult,
        *,
        checking_observed: bool,
        outcome: str,
    ) -> TaskClientVerificationResult:
        checkpoint = _verification_checkpoint(
            authorized.checkpoint,
            result,
            status=TaskStatus.CLIENT_VERIFYING,
            checking_observed=checking_observed,
            outcome=outcome,
        )
        with self._session_factory() as session:
            repository = TaskRepository(session)
            task = repository.get(authorized.task_id)
            if (
                task is None
                or task.status != TaskStatus.CLIENT_VERIFYING.value
                or task.version != authorized.task_version
            ):
                raise _verification_task_changed()
            if task.checkpoint == checkpoint:
                return _result_from_values(
                    task.id,
                    task.version,
                    TaskStatus.CLIENT_VERIFYING,
                    authorized.plan_id,
                    result,
                    outcome=outcome,
                    checking_observed=checking_observed,
                )
            try:
                task = repository.record_checkpoint(
                    task_id=task.id,
                    expected_version=task.version,
                    expected_status=TaskStatus.CLIENT_VERIFYING,
                    checkpoint=checkpoint,
                    event_type="CLIENT_VERIFYING_PROGRESS",
                    reason="已保存客户端下载器校验的可恢复进度证据",
                )
            except DomainViolation as exc:
                raise _verification_task_changed() from exc
            session.commit()
            return _result_from_values(
                task.id,
                task.version,
                TaskStatus.CLIENT_VERIFYING,
                authorized.plan_id,
                result,
                outcome=outcome,
                checking_observed=checking_observed,
            )

    def _finish(
        self,
        authorized: _AuthorizedVerification,
        result: QbittorrentRecheckOperationResult | TransmissionVerifyOperationResult,
        *,
        to_status: TaskStatus,
        checking_observed: bool,
        outcome: str,
        reason: str,
    ) -> TaskClientVerificationResult:
        checkpoint = _verification_checkpoint(
            authorized.checkpoint,
            result,
            status=to_status,
            checking_observed=checking_observed,
            outcome=outcome,
        )
        with self._session_factory() as session:
            repository = TaskRepository(session)
            task = repository.get(authorized.task_id)
            if (
                task is None
                or task.status != TaskStatus.CLIENT_VERIFYING.value
                or task.version != authorized.task_version
            ):
                raise _verification_task_changed()
            try:
                task = repository.transition(
                    task_id=task.id,
                    expected_version=task.version,
                    to_status=to_status,
                    event_type=(
                        "CLIENT_VERIFICATION_CONFIRMED"
                        if to_status is TaskStatus.SEEDING
                        else "CLIENT_VERIFICATION_INCOMPLETE"
                    ),
                    reason=reason,
                    checkpoint=checkpoint,
                )
            except DomainViolation as exc:
                raise _verification_task_changed() from exc
            session.commit()
            return _result_from_values(
                task.id,
                task.version,
                to_status,
                authorized.plan_id,
                result,
                outcome=outcome,
                checking_observed=checking_observed,
            )

    def _load_completed(
        self,
        unit_id: str,
        plan_id: str,
    ) -> TaskClientVerificationResult | None:
        with self._session_factory() as session:
            plan = TaskExecutionPlanRepository(session).get(plan_id)
            if plan is None or plan.task_unit_id != unit_id:
                raise _verification_plan_not_found()
            task = TaskRepository(session).get(plan.task_id)
            if task is None:
                raise _verification_plan_not_found()
            status = TaskStatus(task.status)
            if status is TaskStatus.CLIENT_VERIFYING:
                return None
            if status not in {TaskStatus.SEEDING, TaskStatus.RETRY, TaskStatus.DONE}:
                raise _verification_task_state_invalid()
            checkpoint = deepcopy(task.checkpoint)
            task_id = task.id
            task_version = task.version
            plan_record_id = plan.id
            plan_record_digest = plan.plan_digest
        if (
            checkpoint.get("schema_version")
            not in {
                CLIENT_VERIFICATION_CHECKPOINT_SCHEMA_VERSION,
                SEEDING_CHECKPOINT_SCHEMA_VERSION,
            }
            or checkpoint.get("stage") != status.value
            or checkpoint.get("execution_plan_id") != plan_record_id
            or checkpoint.get("execution_plan_digest") != plan_record_digest
        ):
            raise _verification_task_state_invalid()
        return TaskClientVerificationResult(
            task_id=task_id,
            task_version=task_version,
            status=status,
            execution_plan_id=plan_record_id,
            recheck_journal_id=_required_verification_journal_id(checkpoint),
            torrent_hash=_required_text(checkpoint, "torrent_hash"),
            client_state=_required_text(checkpoint, "client_state"),
            progress=_required_progress(checkpoint, "client_progress"),
            verification_outcome=_required_text(checkpoint, "verification_outcome"),
            checking_observed=_required_bool(checkpoint, "checking_observed"),
            replayed=True,
            recovered_after_unknown_result=False,
        )


def _verification_checkpoint(
    previous: dict[str, object],
    result: QbittorrentRecheckOperationResult | TransmissionVerifyOperationResult,
    *,
    status: TaskStatus,
    checking_observed: bool,
    outcome: str,
) -> dict[str, object]:
    checkpoint = deepcopy(previous)
    checkpoint.update(
        {
            "schema_version": CLIENT_VERIFICATION_CHECKPOINT_SCHEMA_VERSION,
            "stage": status.value,
            "recheck_journal_id": result.journal_id,
            "verification_journal_id": result.journal_id,
            "client_state": result.state,
            "client_progress": result.progress,
            "checking_observed": checking_observed,
            "verification_outcome": outcome,
        }
    )
    if checkpoint.get("repair_schema_version") == REPAIR_DOWNLOAD_CHECKPOINT_SCHEMA_VERSION:
        if status is TaskStatus.CLIENT_VERIFYING:
            checkpoint["repair_stage"] = REPAIR_STAGE_RECHECKING
        elif status is TaskStatus.SEEDING:
            checkpoint["repair_stage"] = REPAIR_STAGE_VERIFIED
        elif status is TaskStatus.RETRY:
            checkpoint["repair_stage"] = REPAIR_STAGE_INCOMPLETE
    return checkpoint


def _result_from_values(
    task_id: str,
    task_version: int,
    status: TaskStatus,
    plan_id: str,
    result: QbittorrentRecheckOperationResult | TransmissionVerifyOperationResult,
    *,
    outcome: str,
    checking_observed: bool,
) -> TaskClientVerificationResult:
    return TaskClientVerificationResult(
        task_id=task_id,
        task_version=task_version,
        status=status,
        execution_plan_id=plan_id,
        recheck_journal_id=result.journal_id,
        torrent_hash=result.torrent_hash,
        client_state=result.state,
        progress=result.progress,
        verification_outcome=outcome,
        checking_observed=checking_observed,
        replayed=result.replayed,
        recovered_after_unknown_result=result.recovered_after_unknown_result,
    )


def _required_add_journal_id(payload: dict[str, object]) -> str:
    value = payload.get("add_journal_id")
    if value is None:
        return _required_text(payload, "qbit_journal_id")
    if not isinstance(value, str) or not value:
        raise _verification_evidence_invalid("CLIENT_VERIFYING 证据缺少有效 add_journal_id")
    return value


def _required_verification_journal_id(payload: dict[str, object]) -> str:
    value = payload.get("verification_journal_id")
    if value is None:
        return _required_text(payload, "recheck_journal_id")
    if not isinstance(value, str) or not value:
        raise _verification_evidence_invalid(
            "CLIENT_VERIFYING 证据缺少有效 verification_journal_id"
        )
    return value


def _required_text(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise _verification_evidence_invalid(f"CLIENT_VERIFYING 证据缺少有效 {key}")
    return value


def _required_digest(payload: dict[str, object], key: str) -> str:
    value = _required_text(payload, key)
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise _verification_evidence_invalid(f"CLIENT_VERIFYING 证据包含无效 {key}")
    return value


def _repair_stage(checkpoint: dict[str, object]) -> str | None:
    schema = checkpoint.get("repair_schema_version")
    if schema is None:
        return None
    if schema != REPAIR_DOWNLOAD_CHECKPOINT_SCHEMA_VERSION:
        raise _verification_evidence_invalid("repair download checkpoint schema 无效")
    stage = checkpoint.get("repair_stage")
    if stage not in {
        REPAIR_STAGE_DOWNLOAD_PENDING,
        REPAIR_STAGE_DOWNLOADING,
        REPAIR_STAGE_RECHECK_PENDING,
        REPAIR_STAGE_RECHECKING,
        REPAIR_STAGE_VERIFIED,
        REPAIR_STAGE_INCOMPLETE,
    }:
        raise _verification_evidence_invalid("repair download checkpoint stage 无效")
    assert isinstance(stage, str)
    return stage


def _required_positive_int(payload: dict[str, object], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise _verification_evidence_invalid(f"CLIENT_VERIFYING 证据缺少有效 {key}")
    return value


def _required_bool(payload: dict[str, object], key: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise _verification_evidence_invalid(f"CLIENT_VERIFYING 证据缺少有效 {key}")
    return value


def _optional_bool(payload: dict[str, object], key: str) -> bool:
    value = payload.get(key)
    if value is None:
        return False
    return _required_bool(payload, key)


def _required_progress(payload: dict[str, object], key: str) -> float:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _verification_evidence_invalid(f"CLIENT_VERIFYING 证据缺少有效 {key}")
    normalized = float(value)
    if not 0.0 <= normalized <= 1.0:
        raise _verification_evidence_invalid(f"CLIENT_VERIFYING {key} 超出 0..1")
    return normalized


def _checkpoint_downloader_kind(checkpoint: dict[str, object]) -> DownloaderKind:
    value = checkpoint.get("downloader_kind")
    if value is None:
        return DownloaderKind.QBITTORRENT
    if not isinstance(value, str):
        raise _verification_evidence_invalid("CLIENT_VERIFYING downloader_kind 无效")
    try:
        kind = DownloaderKind(value)
    except ValueError as exc:
        raise _verification_evidence_invalid("CLIENT_VERIFYING downloader_kind 无效") from exc
    if kind not in {DownloaderKind.QBITTORRENT, DownloaderKind.TRANSMISSION}:
        raise _verification_evidence_invalid("CLIENT_VERIFYING downloader_kind 不受支持")
    return kind


def _verification_binding_changed(detail: str) -> ApplicationError:
    return ApplicationError(
        code="CLIENT_VERIFYING_BINDING_CHANGED",
        status=409,
        title="CLIENT_VERIFYING 目标环境已变化",
        detail=detail,
    )


def _verification_plan_not_found() -> ApplicationError:
    return ApplicationError(
        code="CLIENT_VERIFYING_PLAN_NOT_FOUND",
        status=404,
        title="CLIENT_VERIFYING execution plan 不存在",
        detail="指定 execution plan 不存在或不属于当前 task unit",
    )


def _verification_plan_not_current() -> ApplicationError:
    return ApplicationError(
        code="CLIENT_VERIFYING_PLAN_NOT_CURRENT",
        status=409,
        title="CLIENT_VERIFYING execution plan 已失效",
        detail="客户端校验只能继续 LINKING/ADDING 已兑现的 latest ready execution plan",
    )


def _verification_task_state_invalid() -> ApplicationError:
    return ApplicationError(
        code="CLIENT_VERIFYING_TASK_STATE_INVALID",
        status=409,
        title="任务状态不允许客户端校验",
        detail="客户端校验只能从 CLIENT_VERIFYING 执行，或读取其 SEEDING/RETRY/DONE 既有结果",
    )


def _verification_evidence_invalid(detail: str) -> ApplicationError:
    return ApplicationError(
        code="CLIENT_VERIFYING_EVIDENCE_INVALID",
        status=409,
        title="CLIENT_VERIFYING 恢复证据无效",
        detail=detail,
    )


def _verification_task_changed() -> ApplicationError:
    return ApplicationError(
        code="CLIENT_VERIFYING_TASK_CHANGED",
        status=409,
        title="CLIENT_VERIFYING 任务状态已经变化",
        detail="recheck 后无法用原 task version 安全保存进度或状态，需要重新读取对账",
    )
