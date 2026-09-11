from __future__ import annotations

import stat
import unicodedata
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.analysis import (
    AnalysisLifecycle,
    AnalysisService,
    AnalysisSiteProvider,
    verify_torrent_mappings,
)
from backend.app.application.errors import ApplicationError
from backend.app.domain.downloader import (
    PathMappingRule,
    ProbeStatus,
    downloader_execution_binding_digest,
    reverse_map_container_path_unique,
)
from backend.app.domain.errors import DomainViolation
from backend.app.domain.execution_gate import (
    ExecutionGateBlockReason,
    ExecutionGateSnapshot,
    ExecutionVerificationSource,
)
from backend.app.domain.execution_plan import (
    ExecutionPlanAction,
    ExecutionPlanActionKind,
    ExecutionPlanBlockReason,
    ExecutionPlanSnapshot,
    execution_plan_actions_from_payload,
)
from backend.app.domain.file_mapping import (
    AutoMappingDecision,
    MappingMethod,
    SourceFileCandidate,
    auto_map_files,
)
from backend.app.domain.preflight import PreflightSnapshot
from backend.app.domain.review import (
    ManualReviewMapping,
    ReviewState,
    ReviewVerificationSnapshot,
)
from backend.app.domain.task_state import TaskStatus
from backend.app.domain.task_units import SourceTaskFile, identify_task_units
from backend.app.domain.torrent import TorrentFile
from backend.app.domain.verification import (
    DownloaderKind,
    FileMappingState,
    FileSnapshot,
    VerificationLevel,
)
from backend.app.infrastructure.adapters.site_errors import SiteAdapterError
from backend.app.infrastructure.persistence.downloader_repositories import DownloaderRepository
from backend.app.infrastructure.persistence.models import (
    PreflightSnapshotRecord,
    TaskEvent,
)
from backend.app.infrastructure.persistence.preflight_repositories import (
    PreflightSnapshotRepository,
)
from backend.app.infrastructure.persistence.repositories import TaskCreate, TaskRepository
from backend.app.infrastructure.persistence.task_analysis_repositories import (
    TaskCandidateRepository,
    TaskExecutionGateRepository,
    TaskExecutionPlanRepository,
    TaskReviewRepository,
    TaskReviewVerificationRepository,
    TaskUnitRepository,
)
from backend.app.infrastructure.source_inventory import (
    scan_source_inventory,
    source_inventory_digest,
)
from backend.app.infrastructure.torrent_parser import parse_torrent


@dataclass(frozen=True, slots=True)
class TaskUnitView:
    id: str
    normalized_unit_key: str
    kind: str
    source_root: str
    source_relative_path: str
    length: int
    source_inventory_digest: str
    descriptor: dict[str, Any]
    discovered_at: datetime


@dataclass(frozen=True, slots=True)
class TaskCandidateView:
    id: str
    snapshot_id: str
    normalized_unit_key: str
    site_id: str
    torrent_id: str
    display_name: str
    score: float
    rejected: bool
    selected_for_verification: bool
    verification_level: str | None
    metainfo_digest: str | None
    error_code: str | None
    evidence: dict[str, Any]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class PreflightView:
    id: str
    snapshot_digest: str
    payload: dict[str, Any]
    current: bool
    stale_reasons: tuple[str, ...]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class TaskView:
    id: str
    type: str
    source_downloader_id: str
    source_hash: str
    normalized_unit_key: str
    status: str
    error_code: str | None
    version: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class TaskCreateView:
    task: TaskView
    created: bool


@dataclass(frozen=True, slots=True)
class ManualReviewMappingView:
    torrent_path: str
    source_relative_path: str


@dataclass(frozen=True, slots=True)
class TaskReviewView:
    id: str
    task_id: str
    task_unit_id: str
    preflight_snapshot_id: str
    approved_candidate_id: str | None
    rejected_candidate_ids: tuple[str, ...]
    manual_mappings: tuple[ManualReviewMappingView, ...]
    note: str | None
    requires_reverification: bool
    execution_allowed: bool
    actor_kind: str
    version: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ReviewVerificationView:
    id: str
    review_revision_id: str
    review_version: int
    candidate_id: str
    verification_digest: str
    verification_level: str
    metainfo_digest: str
    execution_allowed: bool
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ExecutionGateView:
    id: str
    gate_digest: str
    eligible: bool
    current: bool
    client_check_required: bool
    verification_level: str | None
    verification_source: str | None
    blocked_reasons: tuple[str, ...]
    preflight_stale_reasons: tuple[str, ...]
    candidate_id: str | None
    metainfo_digest: str | None
    review_revision_id: str
    review_version: int
    side_effects_started: bool
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ExecutionPlanView:
    id: str
    plan_digest: str
    ready: bool
    current: bool
    current_reasons: tuple[str, ...]
    target_root: str
    target_device: int
    target_downloader_id: str | None
    target_downloader_version: int | None
    target_remote_save_path: str | None
    verification_level: str
    client_check_required: bool
    hardlink_count: int
    client_fetch_count: int
    create_directory_count: int
    estimated_download_bytes_upper_bound: int
    blocked_reasons: tuple[str, ...]
    actions: tuple[dict[str, Any], ...]
    execution_allowed: bool
    side_effects_started: bool
    created_at: datetime


@dataclass(frozen=True, slots=True)
class _TargetLayout:
    device: int
    create_directories: tuple[str, ...]
    blocked_reasons: tuple[ExecutionPlanBlockReason, ...]


@dataclass(frozen=True, slots=True)
class _TargetDownloaderPlanBinding:
    downloader_id: str
    downloader_version: int
    binding_digest: str
    remote_save_path: str


class _TaskAnalysisLifecycle(AnalysisLifecycle):
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        task_id: str,
        version: int,
    ) -> None:
        self._session_factory = session_factory
        self._task_id = task_id
        self._version = version

    @property
    def version(self) -> int:
        return self._version

    def start(self) -> int:
        return self._advance(TaskStatus.ANALYZING, "ANALYSIS_STARTED", "开始只读任务分析")

    def enter_searching(self) -> int:
        return self._advance(TaskStatus.SEARCHING, "ANALYSIS_SEARCHING", "开始搜索启用站点")

    def enter_matching(self) -> int:
        return self._advance(TaskStatus.MATCHING, "ANALYSIS_MATCHING", "开始候选排序与匹配")

    def enter_verifying(self) -> int:
        return self._advance(
            TaskStatus.VERIFYING, "ANALYSIS_VERIFYING", "开始 torrent 解析与内容验证"
        )

    def enter_preflight(self) -> int:
        return self._advance(TaskStatus.PREFLIGHT, "ANALYSIS_PREFLIGHT_READY", "只读预演证据已生成")

    def recover_to_retry(self, *, reason: str) -> None:
        with self._session_factory() as session:
            repository = TaskRepository(session)
            task = repository.get(self._task_id)
            if task is None or task.version != self._version:
                return
            status = TaskStatus(task.status)
            if status not in {
                TaskStatus.ANALYZING,
                TaskStatus.SEARCHING,
                TaskStatus.MATCHING,
                TaskStatus.VERIFYING,
                TaskStatus.PREFLIGHT,
            }:
                return
            try:
                task = repository.transition(
                    task_id=self._task_id,
                    expected_version=self._version,
                    to_status=TaskStatus.RETRY,
                    event_type="ANALYSIS_RETRY_REQUIRED",
                    reason=reason,
                )
            except DomainViolation:
                return
            session.commit()
            self._version = task.version

    def _advance(self, to_status: TaskStatus, event_type: str, reason: str) -> int:
        with self._session_factory() as session:
            repository = TaskRepository(session)
            task = repository.get(self._task_id)
            if task is None:
                raise _task_not_found()
            if task.version != self._version:
                raise ApplicationError(
                    code="ANALYSIS_TASK_CHANGED",
                    status=409,
                    title="任务发生变化",
                    detail="分析状态推进时任务版本已被其他操作修改",
                )
            try:
                task = repository.transition(
                    task_id=self._task_id,
                    expected_version=self._version,
                    to_status=to_status,
                    event_type=event_type,
                    reason=reason,
                )
            except DomainViolation as exc:
                raise ApplicationError(
                    code="ANALYSIS_STATE_INVALID",
                    status=409,
                    title="任务状态不允许分析",
                    detail=str(exc),
                ) from exc
            session.commit()
            self._version = task.version
            return self._version


class TaskAnalysisService:
    """任务级 M2 入口：安全定位 /data、持久化 unit/candidate，并判断 preflight 当前性。"""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        site_service: AnalysisSiteProvider,
        *,
        data_root: Path,
    ) -> None:
        self._session_factory = session_factory
        self._site_service = site_service
        self._data_root = data_root
        self._analysis = AnalysisService(session_factory, site_service)

    def list_tasks(self, *, status: TaskStatus | None = None, limit: int = 100) -> list[TaskView]:
        with self._session_factory() as session:
            records = TaskRepository(session).list_recent(status=status, limit=limit)
            return [self._task_view(item) for item in records]

    def get_task(self, task_id: str) -> TaskView:
        with self._session_factory() as session:
            task = TaskRepository(session).get(task_id)
            if task is None:
                raise _task_not_found()
            return self._task_view(task)

    def create_task(
        self,
        *,
        task_type: str,
        source_downloader_id: str,
        source_hash: str,
        normalized_unit_key: str,
    ) -> TaskCreateView:
        normalized_values = tuple(
            value.strip()
            for value in (task_type, source_downloader_id, source_hash, normalized_unit_key)
        )
        if any(not value for value in normalized_values):
            raise ApplicationError(
                code="TASK_INPUT_INVALID",
                status=422,
                title="任务输入无效",
                detail="任务类型、来源下载器、source hash 与处理单元 key 均不能为空",
            )
        normalized_type, normalized_downloader, normalized_hash, normalized_unit = normalized_values
        request = TaskCreate(
            task_type=normalized_type,
            source_downloader_id=normalized_downloader,
            source_hash=normalized_hash,
            normalized_unit_key=normalized_unit,
            trace_id=str(uuid4()),
        )
        with self._session_factory() as session:
            task, created = TaskRepository(session).create_or_get(request)
            session.commit()
            return TaskCreateView(task=self._task_view(task), created=created)

    async def analyze(self, task_id: str, *, source_root: str) -> PreflightSnapshot:
        normalized_root, resolved_root = self._resolve_source_root(source_root)
        with self._session_factory() as session:
            task = TaskRepository(session).get(task_id)
            if task is None:
                raise _task_not_found()
            if TaskStatus(task.status) not in {
                TaskStatus.PENDING,
                TaskStatus.RETRY,
                TaskStatus.PAUSED,
            }:
                raise ApplicationError(
                    code="ANALYSIS_STATE_INVALID",
                    status=409,
                    title="任务状态不允许分析",
                    detail="手动 Analyze 只允许从 PENDING、RETRY 或 PAUSED 开始",
                )
            task_key = task.normalized_unit_key
            lifecycle = _TaskAnalysisLifecycle(
                self._session_factory,
                task_id=task_id,
                version=task.version,
            )
        lifecycle.start()
        try:
            inventory = scan_source_inventory(resolved_root)
        except DomainViolation as exc:
            lifecycle.recover_to_retry(reason=f"源目录扫描失败：{exc.code.value}")
            raise ApplicationError(
                code=exc.code.value,
                status=422,
                title="源目录扫描失败",
                detail=str(exc),
            ) from exc
        inventory_digest = source_inventory_digest(inventory)
        units = identify_task_units(
            tuple(SourceTaskFile(item.relative_path, item.length) for item in inventory)
        )
        selected = next((item for item in units if item.normalized_unit_key == task_key), None)
        if selected is None:
            lifecycle.recover_to_retry(reason="当前源目录未找到任务处理单元")
            raise ApplicationError(
                code="ANALYSIS_UNIT_NOT_FOUND",
                status=409,
                title="处理单元未找到",
                detail="当前源目录中没有与任务 normalized_unit_key 匹配的媒体单元",
            )
        with self._session_factory() as session:
            TaskUnitRepository(session).record_batch(
                task_id=task_id,
                source_root=normalized_root,
                source_inventory_digest=inventory_digest,
                units=units,
            )
            session.commit()
        try:
            return await self._analysis.analyze(
                task_id=task_id,
                unit=selected,
                source_root=resolved_root,
                source_inventory=inventory,
                lifecycle=lifecycle,
            )
        except ApplicationError as exc:
            lifecycle.recover_to_retry(reason=f"分析失败：{exc.code}")
            raise
        except DomainViolation as exc:
            lifecycle.recover_to_retry(reason=f"分析安全检查失败：{exc.code.value}")
            raise ApplicationError(
                code=exc.code.value,
                status=409,
                title="分析安全检查失败",
                detail=str(exc),
            ) from exc

    def list_units(self, task_id: str) -> list[TaskUnitView]:
        self._require_task(task_id)
        with self._session_factory() as session:
            records = TaskUnitRepository(session).list_latest(task_id)
            return [self._unit_view(item) for item in records]

    def list_candidates(self, task_id: str) -> list[TaskCandidateView]:
        self._require_task(task_id)
        with self._session_factory() as session:
            records = TaskCandidateRepository(session).list_for_latest_snapshot(task_id)
            return [self._candidate_view(item) for item in records]

    def latest_preflight(self, task_id: str) -> PreflightView:
        with self._session_factory() as session:
            task_repository = TaskRepository(session)
            task = task_repository.get(task_id)
            if task is None:
                raise _task_not_found()
            latest_event = task_repository.latest_event(task_id)
            record = PreflightSnapshotRepository(session).latest_for_task(task_id)
            if record is None:
                raise ApplicationError(
                    code="PREFLIGHT_NOT_FOUND",
                    status=404,
                    title="预演不存在",
                    detail="该任务尚未生成 preflight snapshot",
                )
            unit = TaskUnitRepository(session).get_for_snapshot(
                task_id=task_id,
                normalized_unit_key=record.normalized_unit_key,
                source_inventory_digest=record.source_inventory_digest,
            )
            payload = deepcopy(record.payload)
            record_id = record.id
            digest = record.snapshot_digest
            created_at = record.created_at
            record_task_version = record.task_version
            task_version = task.version
            task_status = task.status

        reasons: list[str] = []
        if task_version != record_task_version and not _review_bridge_is_current(
            task_version=task_version,
            task_status=task_status,
            snapshot_task_version=record_task_version,
            latest_event=latest_event,
        ):
            reasons.append("TASK_VERSION_CHANGED")
        if unit is None:
            reasons.append("UNIT_RECORD_MISSING")
        else:
            try:
                _, source_path = self._resolve_source_root(unit.source_root)
                current_inventory = scan_source_inventory(source_path)
                if source_inventory_digest(current_inventory) != record.source_inventory_digest:
                    reasons.append("SOURCE_CHANGED")
            except (ApplicationError, DomainViolation):
                reasons.append("SOURCE_UNAVAILABLE")

        expected_site_versions = _site_versions_from_payload(payload)
        if expected_site_versions is None:
            reasons.append("PREFLIGHT_EVIDENCE_INVALID")
        elif tuple(sorted(self._site_service.enabled_site_versions())) != expected_site_versions:
            reasons.append("SITE_CONFIG_CHANGED")

        return PreflightView(
            id=record_id,
            snapshot_digest=digest,
            payload=payload,
            current=not reasons,
            stale_reasons=tuple(reasons),
            created_at=created_at,
        )

    def get_review(self, unit_id: str) -> TaskReviewView:
        with self._session_factory() as session:
            unit = TaskUnitRepository(session).get(unit_id)
            if unit is None:
                raise _review_unit_not_found()
            snapshot = PreflightSnapshotRepository(session).latest_for_task(unit.task_id)
            if snapshot is None:
                raise _review_preflight_not_found()
            _ensure_review_unit_matches_snapshot(unit, snapshot)
            record = TaskReviewRepository(session).latest(
                task_unit_id=unit.id,
                preflight_snapshot_id=snapshot.id,
            )
            if record is None:
                raise ApplicationError(
                    code="REVIEW_NOT_FOUND",
                    status=404,
                    title="审核决策不存在",
                    detail="该处理单元尚未提交人工审核 revision",
                )
            return self._review_view(record)

    def get_review_verification(self, unit_id: str) -> ReviewVerificationView:
        with self._session_factory() as session:
            unit = TaskUnitRepository(session).get(unit_id)
            if unit is None:
                raise _review_unit_not_found()
            snapshot = PreflightSnapshotRepository(session).latest_for_task(unit.task_id)
            if snapshot is None:
                raise _review_preflight_not_found()
            _ensure_review_unit_matches_snapshot(unit, snapshot)
            review = TaskReviewRepository(session).latest(
                task_unit_id=unit.id,
                preflight_snapshot_id=snapshot.id,
            )
            if review is None:
                raise ApplicationError(
                    code="REVIEW_NOT_FOUND",
                    status=404,
                    title="审核决策不存在",
                    detail="该处理单元尚未提交人工审核 revision",
                )
            record = TaskReviewVerificationRepository(session).get_for_revision(review.id)
            if record is None:
                raise ApplicationError(
                    code="REVIEW_VERIFICATION_NOT_FOUND",
                    status=404,
                    title="重验证证据不存在",
                    detail="当前审核 revision 尚未生成重验证证据",
                )
            return self._review_verification_view(record)

    def get_execution_gate(self, unit_id: str) -> ExecutionGateView:
        with self._session_factory() as session:
            record = TaskExecutionGateRepository(session).latest(unit_id)
            if record is None:
                raise ApplicationError(
                    code="EXECUTION_GATE_NOT_FOUND",
                    status=404,
                    title="执行门证据不存在",
                    detail="该处理单元尚未生成 pre-execution gate",
                )
            stored = self._execution_gate_view(record, current=False)
        try:
            evaluated = self._evaluate_execution_gate(unit_id)
            current = evaluated.gate_digest == stored.gate_digest
        except ApplicationError:
            current = False
        return replace(stored, current=current)

    def refresh_execution_gate(self, unit_id: str) -> ExecutionGateView:
        snapshot = self._evaluate_execution_gate(unit_id)
        with self._session_factory() as session:
            self._assert_execution_gate_inputs(session, snapshot)
            record, _ = TaskExecutionGateRepository(session).create_or_get(snapshot)
            session.commit()
            return self._execution_gate_view(record, current=True)

    def get_execution_plan(self, unit_id: str) -> ExecutionPlanView:
        with self._session_factory() as session:
            record = TaskExecutionPlanRepository(session).latest(unit_id)
            if record is None:
                raise ApplicationError(
                    code="EXECUTION_PLAN_NOT_FOUND",
                    status=404,
                    title="执行计划不存在",
                    detail="该处理单元尚未生成无副作用执行计划",
                )
            stored_payload = deepcopy(record.payload)
            stored = self._execution_plan_view(record, current=False, current_reasons=())

        reasons: list[str] = []
        try:
            gate = self.get_execution_gate(unit_id)
            expected_gate_id = stored_payload.get("execution_gate_id")
            expected_gate_digest = stored_payload.get("execution_gate_digest")
            if (
                not gate.current
                or not gate.eligible
                or gate.id != expected_gate_id
                or gate.gate_digest != expected_gate_digest
            ):
                reasons.append("EXECUTION_GATE_CHANGED")
        except ApplicationError:
            reasons.append("EXECUTION_GATE_CHANGED")

        with self._session_factory() as session:
            task = TaskRepository(session).get(record.task_id)
            if task is None or task.version != record.task_version:
                reasons.append("TASK_VERSION_CHANGED")

        actions = _execution_plan_actions_from_payload(stored_payload)
        expected_directories = _string_tuple(stored_payload.get("create_directories"))
        expected_blockers = _execution_plan_blockers(stored_payload.get("blocked_reasons"))
        target_downloader_id = _optional_text(stored_payload.get("target_downloader_id"))
        target_downloader_version = _optional_int(stored_payload.get("target_downloader_version"))
        target_downloader_binding_digest = _optional_text(
            stored_payload.get("target_downloader_binding_digest")
        )
        target_remote_save_path = _optional_text(stored_payload.get("target_remote_save_path"))
        if (
            actions is None
            or expected_directories is None
            or expected_blockers is None
            or target_downloader_id is None
            or target_downloader_version is None
            or target_downloader_binding_digest is None
            or target_remote_save_path is None
        ):
            reasons.append("PLAN_EVIDENCE_INVALID")
        else:
            try:
                normalized_target, target_path = self._resolve_execution_target_root(
                    record.target_root
                )
                if normalized_target != record.target_root:
                    reasons.append("TARGET_ROOT_CHANGED")
                else:
                    layout = self._inspect_target_layout(target_path, actions)
                    target_blockers = tuple(
                        item
                        for item in expected_blockers
                        if item
                        in {
                            ExecutionPlanBlockReason.TARGET_EXISTS,
                            ExecutionPlanBlockReason.TARGET_PARENT_UNSAFE,
                            ExecutionPlanBlockReason.CROSS_DEVICE,
                        }
                    )
                    if layout.device != record.target_device:
                        reasons.append("TARGET_ROOT_CHANGED")
                    elif (
                        layout.create_directories != expected_directories
                        or layout.blocked_reasons != target_blockers
                    ):
                        reasons.append("TARGET_STATE_CHANGED")
            except ApplicationError:
                reasons.append("TARGET_ROOT_CHANGED")
            else:
                try:
                    with self._session_factory() as session:
                        binding = self._target_downloader_plan_binding(
                            session,
                            target_downloader_id,
                            target_path,
                            require_client_verification=record.client_check_required,
                        )
                except ApplicationError:
                    reasons.append("TARGET_DOWNLOADER_CHANGED")
                else:
                    if (
                        binding.downloader_version != target_downloader_version
                        or binding.binding_digest != target_downloader_binding_digest
                        or binding.remote_save_path != target_remote_save_path
                    ):
                        reasons.append("TARGET_DOWNLOADER_CHANGED")

        normalized_reasons = tuple(dict.fromkeys(reasons))
        return replace(
            stored,
            current=not normalized_reasons,
            current_reasons=normalized_reasons,
        )

    async def create_execution_plan(
        self,
        unit_id: str,
        *,
        target_root: str,
        target_downloader_id: str,
    ) -> ExecutionPlanView:
        gate = self.get_execution_gate(unit_id)
        if not gate.current or not gate.eligible:
            raise ApplicationError(
                code="EXECUTION_PLAN_GATE_NOT_READY",
                status=409,
                title="执行安全门未就绪",
                detail="只有 current 且 eligible 的 execution gate 才能生成执行计划",
            )
        if (
            gate.candidate_id is None
            or gate.metainfo_digest is None
            or gate.verification_level is None
        ):
            raise ApplicationError(
                code="EXECUTION_PLAN_GATE_INVALID",
                status=409,
                title="执行安全门证据不完整",
                detail="execution gate 缺少候选、metainfo 或验证等级",
            )
        try:
            verification_level = VerificationLevel(gate.verification_level)
        except ValueError as exc:
            raise ApplicationError(
                code="EXECUTION_PLAN_GATE_INVALID",
                status=409,
                title="执行安全门验证等级无效",
                detail="execution gate 包含未知验证等级",
            ) from exc

        normalized_target, resolved_target = self._resolve_execution_target_root(target_root)
        with self._session_factory() as session:
            target_downloader = self._target_downloader_plan_binding(
                session,
                target_downloader_id,
                resolved_target,
                require_client_verification=gate.client_check_required,
            )
            unit = TaskUnitRepository(session).get(unit_id)
            if unit is None:
                raise _review_unit_not_found()
            gate_record = TaskExecutionGateRepository(session).latest(unit_id)
            candidate = TaskCandidateRepository(session).get(gate.candidate_id)
            if (
                gate_record is None
                or gate_record.id != gate.id
                or gate_record.gate_digest != gate.gate_digest
                or candidate is None
                or candidate.id != gate.candidate_id
                or candidate.preflight_snapshot_id != gate_record.preflight_snapshot_id
            ):
                raise _execution_plan_input_changed()
            source_root = unit.source_root
            expected_inventory_digest = gate_record.payload.get("source_inventory_digest")
            if not isinstance(expected_inventory_digest, str):
                raise ApplicationError(
                    code="EXECUTION_PLAN_GATE_INVALID",
                    status=409,
                    title="执行安全门证据不完整",
                    detail="execution gate 缺少 source inventory digest",
                )
            mapping_payload = self._execution_plan_mapping_payload(session, gate_record, candidate)
            candidate_site_id = candidate.site_id
            candidate_torrent_id = candidate.torrent_id
            task_id = candidate.task_id
            task_version = gate_record.task_version
            preflight_snapshot_id = gate_record.preflight_snapshot_id
            review_revision_id = gate_record.review_revision_id

        _, resolved_source = self._resolve_source_root(source_root)
        try:
            inventory = scan_source_inventory(resolved_source)
        except DomainViolation as exc:
            raise ApplicationError(
                code="EXECUTION_PLAN_SOURCE_UNAVAILABLE",
                status=409,
                title="源文件不可用",
                detail="生成执行计划时无法重新确认源文件清单",
            ) from exc
        if source_inventory_digest(inventory) != expected_inventory_digest:
            raise _execution_plan_input_changed()

        binding = self._execution_plan_site_binding(candidate_site_id)
        try:
            torrent_payload = await binding.adapter.fetch_torrent(candidate_torrent_id)
        except SiteAdapterError as exc:
            raise ApplicationError(
                code="EXECUTION_PLAN_TORRENT_FETCH_FAILED",
                status=502,
                title="候选 torrent 获取失败",
                detail=f"站点适配器返回安全错误码：{exc.code}",
            ) from exc
        if (
            torrent_payload.site_id != candidate_site_id
            or torrent_payload.torrent_id != candidate_torrent_id
        ):
            raise ApplicationError(
                code="EXECUTION_PLAN_TORRENT_IDENTITY_MISMATCH",
                status=409,
                title="torrent 身份不一致",
                detail="执行计划取回的 torrent 身份与批准候选不一致",
            )
        try:
            meta = parse_torrent(torrent_payload.content)
        except DomainViolation as exc:
            raise ApplicationError(
                code="EXECUTION_PLAN_TORRENT_INVALID",
                status=409,
                title="候选 torrent 无法安全解析",
                detail=f"torrent 安全解析失败：{exc.code.value}",
            ) from exc
        if meta.metainfo_digest != gate.metainfo_digest:
            raise ApplicationError(
                code="EXECUTION_PLAN_TORRENT_CHANGED",
                status=409,
                title="候选 torrent 已变化",
                detail="当前获取的 metainfo digest 与 execution gate 不一致",
            )

        actions, mapping_blockers = self._build_execution_plan_actions(
            meta.files,
            mapping_payload=mapping_payload,
            inventory=inventory,
        )
        layout = self._inspect_target_layout(resolved_target, actions)
        blockers = tuple((*mapping_blockers, *layout.blocked_reasons))
        estimated_download = (
            sum(item.length for item in meta.files if not item.padding)
            if gate.client_check_required
            else sum(
                action.length
                for action in actions
                if action.kind is ExecutionPlanActionKind.CLIENT_FETCH
            )
        )
        snapshot = ExecutionPlanSnapshot(
            task_id=task_id,
            task_version=task_version,
            task_unit_id=unit_id,
            execution_gate_id=gate.id,
            execution_gate_digest=gate.gate_digest,
            preflight_snapshot_id=preflight_snapshot_id,
            review_revision_id=review_revision_id,
            candidate_id=gate.candidate_id,
            source_inventory_digest=expected_inventory_digest,
            metainfo_digest=meta.metainfo_digest,
            verification_level=verification_level,
            client_check_required=gate.client_check_required,
            source_root=source_root,
            target_root=normalized_target,
            target_device=layout.device,
            target_downloader_id=target_downloader.downloader_id,
            target_downloader_version=target_downloader.downloader_version,
            target_downloader_binding_digest=target_downloader.binding_digest,
            target_remote_save_path=target_downloader.remote_save_path,
            actions=actions,
            create_directories=layout.create_directories,
            estimated_download_bytes_upper_bound=estimated_download,
            blocked_reasons=blockers,
            created_at=datetime.now(UTC),
        )

        final_gate = self.get_execution_gate(unit_id)
        if (
            not final_gate.current
            or not final_gate.eligible
            or final_gate.id != gate.id
            or final_gate.gate_digest != gate.gate_digest
        ):
            raise _execution_plan_input_changed()
        try:
            final_inventory = scan_source_inventory(resolved_source)
        except DomainViolation as exc:
            raise _execution_plan_input_changed() from exc
        if source_inventory_digest(final_inventory) != expected_inventory_digest:
            raise _execution_plan_input_changed()
        final_layout = self._inspect_target_layout(resolved_target, actions)
        if final_layout != layout:
            raise _execution_plan_input_changed()

        with self._session_factory() as session:
            self._assert_execution_plan_inputs(session, snapshot)
            record, _ = TaskExecutionPlanRepository(session).create_or_get(snapshot)
            session.commit()
            return self._execution_plan_view(record, current=True, current_reasons=())

    async def reverify_review(self, unit_id: str) -> ReviewVerificationView:
        with self._session_factory() as session:
            unit = TaskUnitRepository(session).get(unit_id)
            if unit is None:
                raise _review_unit_not_found()
            task_id = unit.task_id
            snapshot = PreflightSnapshotRepository(session).latest_for_task(task_id)
            if snapshot is None:
                raise _review_preflight_not_found()
            _ensure_review_unit_matches_snapshot(unit, snapshot)
            review = TaskReviewRepository(session).latest(
                task_unit_id=unit.id,
                preflight_snapshot_id=snapshot.id,
            )
            if review is None:
                raise ApplicationError(
                    code="REVIEW_NOT_FOUND",
                    status=404,
                    title="审核决策不存在",
                    detail="必须先提交人工审核 revision 才能执行重验证",
                )
            if not review.requires_reverification:
                raise ApplicationError(
                    code="REVIEW_REVERIFICATION_NOT_REQUIRED",
                    status=409,
                    title="当前审核无需重验证",
                    detail="当前审核 revision 没有需要重新验证的候选或人工映射",
                )
            if review.approved_candidate_id is None:
                raise ApplicationError(
                    code="REVIEW_APPROVED_CANDIDATE_REQUIRED",
                    status=409,
                    title="缺少批准候选",
                    detail="重验证必须绑定一个当前审核批准的候选",
                )
            candidate = TaskCandidateRepository(session).get(review.approved_candidate_id)
            if candidate is None or candidate.preflight_snapshot_id != snapshot.id:
                raise ApplicationError(
                    code="REVIEW_CANDIDATE_INVALID",
                    status=409,
                    title="审核候选不可用",
                    detail="批准候选已不属于当前 preflight",
                )
            review_id = review.id
            review_version = review.version
            manual_mappings = tuple(deepcopy(review.manual_mappings))
            snapshot_id = snapshot.id
            expected_inventory_digest = snapshot.source_inventory_digest
            source_root = unit.source_root
            candidate_id = candidate.id
            candidate_site_id = candidate.site_id
            candidate_torrent_id = candidate.torrent_id
            expected_metainfo_digest = candidate.metainfo_digest

        preflight = self.latest_preflight(task_id)
        if preflight.id != snapshot_id or not preflight.current:
            raise ApplicationError(
                code="REVIEW_PREFLIGHT_STALE",
                status=409,
                title="预演证据已失效",
                detail="重验证只能基于当前 preflight 执行",
            )

        _, resolved_root = self._resolve_source_root(source_root)
        try:
            inventory = scan_source_inventory(resolved_root)
        except DomainViolation as exc:
            raise ApplicationError(
                code="REVIEW_SOURCE_UNAVAILABLE",
                status=409,
                title="源文件不可用",
                detail="重验证时无法重新确认源文件清单",
            ) from exc
        if source_inventory_digest(inventory) != expected_inventory_digest:
            raise ApplicationError(
                code="REVIEW_PREFLIGHT_STALE",
                status=409,
                title="源文件已经变化",
                detail="重验证前 source inventory 已变化，请重新 Analyze",
            )

        bindings = tuple(
            item
            for item in self._site_service.enabled_adapters()
            if item.site_id == candidate_site_id
        )
        if len(bindings) != 1:
            raise ApplicationError(
                code="REVIEW_SITE_UNAVAILABLE",
                status=409,
                title="候选站点不可唯一确定",
                detail="重验证要求批准候选对应且仅对应一个当前启用站点配置",
            )
        binding = bindings[0]
        try:
            payload = await binding.adapter.fetch_torrent(candidate_torrent_id)
        except SiteAdapterError as exc:
            raise ApplicationError(
                code="REVIEW_TORRENT_FETCH_FAILED",
                status=502,
                title="候选 torrent 获取失败",
                detail=f"站点适配器返回安全错误码：{exc.code}",
            ) from exc
        if payload.site_id != candidate_site_id or payload.torrent_id != candidate_torrent_id:
            raise ApplicationError(
                code="REVIEW_TORRENT_IDENTITY_MISMATCH",
                status=409,
                title="torrent 身份不一致",
                detail="重验证取回的 torrent 身份与批准候选不一致",
            )
        try:
            meta = parse_torrent(payload.content)
        except DomainViolation as exc:
            raise ApplicationError(
                code="REVIEW_TORRENT_INVALID",
                status=409,
                title="候选 torrent 无法安全解析",
                detail=f"torrent 安全解析失败：{exc.code.value}",
            ) from exc
        if (
            expected_metainfo_digest is not None
            and meta.metainfo_digest != expected_metainfo_digest
        ):
            raise ApplicationError(
                code="REVIEW_TORRENT_CHANGED",
                status=409,
                title="候选 torrent 已变化",
                detail="当前获取的 metainfo digest 与 preflight 候选证据不一致",
            )

        mappings = self._apply_review_mappings(
            auto_map_files(meta, inventory),
            manual_mappings=manual_mappings,
            inventory=inventory,
        )
        try:
            level = verify_torrent_mappings(meta, mappings)
        except DomainViolation as exc:
            raise ApplicationError(
                code="REVIEW_VERIFICATION_FAILED",
                status=409,
                title="人工映射重验证失败",
                detail=f"内容验证失败：{exc.code.value}",
            ) from exc

        final_preflight = self.latest_preflight(task_id)
        if final_preflight.id != snapshot_id or not final_preflight.current:
            raise ApplicationError(
                code="REVIEW_PREFLIGHT_STALE",
                status=409,
                title="预演证据已失效",
                detail="重验证完成后 preflight 已变化，本次结果不会落库",
            )
        verification = ReviewVerificationSnapshot(
            task_id=task_id,
            task_unit_id=unit_id,
            review_revision_id=review_id,
            review_version=review_version,
            preflight_snapshot_id=snapshot_id,
            candidate_id=candidate_id,
            source_inventory_digest=expected_inventory_digest,
            metainfo_digest=meta.metainfo_digest,
            verification_level=level,
            mappings=mappings,
            created_at=datetime.now(UTC),
        )
        with self._session_factory() as session:
            latest_review = TaskReviewRepository(session).latest(
                task_unit_id=unit_id,
                preflight_snapshot_id=snapshot_id,
            )
            if latest_review is None or latest_review.id != review_id:
                raise ApplicationError(
                    code="REVIEW_VERSION_CONFLICT",
                    status=409,
                    title="审核版本冲突",
                    detail="重验证期间审核 revision 已变化，本次结果不会落库",
                )
            try:
                record, _ = TaskReviewVerificationRepository(session).create_or_get(verification)
            except ValueError as exc:
                raise ApplicationError(
                    code="REVIEW_VERIFICATION_CONFLICT",
                    status=409,
                    title="重验证证据冲突",
                    detail="同一审核 revision 已存在不同的重验证证据",
                ) from exc
            session.commit()
            return self._review_verification_view(record)

    def _evaluate_execution_gate(self, unit_id: str) -> ExecutionGateSnapshot:
        with self._session_factory() as session:
            unit = TaskUnitRepository(session).get(unit_id)
            if unit is None:
                raise _review_unit_not_found()
            task = TaskRepository(session).get(unit.task_id)
            if task is None:
                raise _task_not_found()
            snapshot = PreflightSnapshotRepository(session).latest_for_task(unit.task_id)
            if snapshot is None:
                raise _review_preflight_not_found()
            _ensure_review_unit_matches_snapshot(unit, snapshot)
            review = TaskReviewRepository(session).latest(
                task_unit_id=unit.id,
                preflight_snapshot_id=snapshot.id,
            )
            if review is None:
                raise ApplicationError(
                    code="REVIEW_NOT_FOUND",
                    status=404,
                    title="审核决策不存在",
                    detail="生成执行门前必须先提交人工审核 revision",
                )
            candidate = (
                TaskCandidateRepository(session).get(review.approved_candidate_id)
                if review.approved_candidate_id is not None
                else None
            )
            reverification = (
                TaskReviewVerificationRepository(session).get_for_revision(review.id)
                if review.requires_reverification
                else None
            )

            task_id = task.id
            task_version = task.version
            task_status = task.status
            snapshot_id = snapshot.id
            snapshot_digest = snapshot.snapshot_digest
            inventory_digest = snapshot.source_inventory_digest
            review_id = review.id
            review_version = review.version
            approved_candidate_id = review.approved_candidate_id
            rejected_candidate_ids = frozenset(review.rejected_candidate_ids)
            requires_reverification = review.requires_reverification
            candidate_exists = candidate is not None
            candidate_snapshot_id = (
                candidate.preflight_snapshot_id if candidate is not None else None
            )
            candidate_rejected = candidate.rejected if candidate is not None else False
            candidate_selected = (
                candidate.selected_for_verification if candidate is not None else False
            )
            candidate_level = candidate.verification_level if candidate is not None else None
            candidate_metainfo = candidate.metainfo_digest if candidate is not None else None
            candidate_error = candidate.error_code if candidate is not None else None
            if reverification is not None:
                reverification_id = reverification.id
                reverification_review_id = reverification.review_revision_id
                reverification_review_version = reverification.review_version
                reverification_snapshot_id = reverification.preflight_snapshot_id
                reverification_candidate_id = reverification.candidate_id
                reverification_inventory = reverification.source_inventory_digest
                reverification_metainfo = reverification.metainfo_digest
                reverification_level = reverification.verification_level
                reverification_digest = reverification.verification_digest
            else:
                reverification_id = None
                reverification_review_id = None
                reverification_review_version = None
                reverification_snapshot_id = None
                reverification_candidate_id = None
                reverification_inventory = None
                reverification_metainfo = None
                reverification_level = None
                reverification_digest = None

        preflight = self.latest_preflight(task_id)
        reasons: list[ExecutionGateBlockReason] = []
        if task_status != TaskStatus.AWAITING_CONFIRMATION.value:
            reasons.append(ExecutionGateBlockReason.TASK_STATE_INVALID)
        if preflight.id != snapshot_id or not preflight.current:
            reasons.append(ExecutionGateBlockReason.PREFLIGHT_STALE)

        verification_source: ExecutionVerificationSource | None = None
        verification_level: VerificationLevel | None = None
        metainfo_digest = candidate_metainfo
        if approved_candidate_id is None:
            reasons.append(ExecutionGateBlockReason.APPROVED_CANDIDATE_MISSING)
        elif (
            not candidate_exists
            or candidate_snapshot_id != snapshot_id
            or approved_candidate_id in rejected_candidate_ids
        ):
            reasons.append(ExecutionGateBlockReason.CANDIDATE_INVALID)
        else:
            if candidate_rejected:
                reasons.append(ExecutionGateBlockReason.CANDIDATE_HARD_REJECTED)
            if requires_reverification:
                if reverification_id is None:
                    reasons.append(ExecutionGateBlockReason.REVERIFICATION_REQUIRED)
                elif (
                    reverification_review_id != review_id
                    or reverification_review_version != review_version
                    or reverification_snapshot_id != snapshot_id
                    or reverification_candidate_id != approved_candidate_id
                    or reverification_inventory != inventory_digest
                    or reverification_level is None
                    or (
                        candidate_metainfo is not None
                        and reverification_metainfo != candidate_metainfo
                    )
                ):
                    reasons.append(ExecutionGateBlockReason.REVERIFICATION_MISMATCH)
                else:
                    verification_source = ExecutionVerificationSource.REVIEW_REVERIFICATION
                    metainfo_digest = reverification_metainfo
                    try:
                        verification_level = VerificationLevel(reverification_level)
                    except ValueError:
                        reasons.append(ExecutionGateBlockReason.VERIFICATION_LEVEL_UNSUPPORTED)
            else:
                verification_source = ExecutionVerificationSource.PREFLIGHT
                if candidate_error is not None:
                    reasons.append(ExecutionGateBlockReason.CANDIDATE_ERROR)
                if not candidate_selected or candidate_metainfo is None or candidate_level is None:
                    reasons.append(ExecutionGateBlockReason.CANDIDATE_NOT_VERIFIED)
                elif candidate_error is None:
                    try:
                        verification_level = VerificationLevel(candidate_level)
                    except ValueError:
                        reasons.append(ExecutionGateBlockReason.VERIFICATION_LEVEL_UNSUPPORTED)

        if verification_level is VerificationLevel.BLOCKED:
            reasons.append(ExecutionGateBlockReason.VERIFICATION_BLOCKED)
        elif verification_level is not None and verification_level not in {
            VerificationLevel.FULL_VERIFIED,
            VerificationLevel.CLIENT_CHECK_REQUIRED,
        }:
            reasons.append(ExecutionGateBlockReason.VERIFICATION_LEVEL_UNSUPPORTED)

        blocked_reasons = tuple(reasons)
        eligible = not blocked_reasons and verification_level in {
            VerificationLevel.FULL_VERIFIED,
            VerificationLevel.CLIENT_CHECK_REQUIRED,
        }
        return ExecutionGateSnapshot(
            task_id=task_id,
            task_version=task_version,
            task_unit_id=unit_id,
            preflight_snapshot_id=snapshot_id,
            preflight_snapshot_digest=snapshot_digest,
            preflight_stale_reasons=preflight.stale_reasons,
            review_revision_id=review_id,
            review_version=review_version,
            candidate_id=approved_candidate_id,
            source_inventory_digest=inventory_digest,
            metainfo_digest=metainfo_digest,
            verification_source=verification_source,
            review_verification_id=reverification_id,
            review_verification_digest=reverification_digest,
            verification_level=verification_level,
            eligible=eligible,
            client_check_required=(
                eligible and verification_level is VerificationLevel.CLIENT_CHECK_REQUIRED
            ),
            blocked_reasons=blocked_reasons,
            created_at=datetime.now(UTC),
        )

    def _assert_execution_gate_inputs(
        self,
        session: Session,
        snapshot: ExecutionGateSnapshot,
    ) -> None:
        task = TaskRepository(session).get(snapshot.task_id)
        preflight = PreflightSnapshotRepository(session).latest_for_task(snapshot.task_id)
        review = TaskReviewRepository(session).latest(
            task_unit_id=snapshot.task_unit_id,
            preflight_snapshot_id=snapshot.preflight_snapshot_id,
        )
        reverification = (
            TaskReviewVerificationRepository(session).get_for_revision(snapshot.review_revision_id)
            if snapshot.review_verification_id is not None
            else None
        )
        if (
            task is None
            or task.version != snapshot.task_version
            or preflight is None
            or preflight.id != snapshot.preflight_snapshot_id
            or review is None
            or review.id != snapshot.review_revision_id
            or review.version != snapshot.review_version
            or review.approved_candidate_id != snapshot.candidate_id
            or (
                snapshot.review_verification_id is not None
                and (
                    reverification is None
                    or reverification.id != snapshot.review_verification_id
                    or reverification.verification_digest != snapshot.review_verification_digest
                )
            )
        ):
            raise ApplicationError(
                code="EXECUTION_GATE_INPUT_CHANGED",
                status=409,
                title="执行门输入已经变化",
                detail="生成 execution gate 期间任务、预演或审核证据发生变化，请重新检查",
            )

    def _execution_plan_mapping_payload(
        self,
        session: Session,
        gate_record: Any,
        candidate: Any,
    ) -> tuple[dict[str, Any], ...]:
        if gate_record.review_verification_id is not None:
            verification = TaskReviewVerificationRepository(session).get_for_revision(
                gate_record.review_revision_id
            )
            if verification is None or verification.id != gate_record.review_verification_id:
                raise _execution_plan_input_changed()
            raw = verification.mappings
        else:
            raw = candidate.evidence.get("mappings")
        if not isinstance(raw, list) or not all(isinstance(item, dict) for item in raw):
            raise ApplicationError(
                code="EXECUTION_PLAN_MAPPING_EVIDENCE_INVALID",
                status=409,
                title="文件映射证据无效",
                detail="execution gate 对应的候选缺少完整映射证据",
            )
        return tuple(deepcopy(item) for item in raw)

    def _execution_plan_site_binding(self, site_id: str) -> Any:
        bindings = tuple(
            item for item in self._site_service.enabled_adapters() if item.site_id == site_id
        )
        if len(bindings) != 1:
            raise ApplicationError(
                code="EXECUTION_PLAN_SITE_UNAVAILABLE",
                status=409,
                title="候选站点不可唯一确定",
                detail="生成执行计划要求候选对应且仅对应一个当前启用站点配置",
            )
        return bindings[0]

    def _build_execution_plan_actions(
        self,
        torrent_files: tuple[TorrentFile, ...],
        *,
        mapping_payload: tuple[dict[str, Any], ...],
        inventory: tuple[SourceFileCandidate, ...],
    ) -> tuple[tuple[ExecutionPlanAction, ...], tuple[ExecutionPlanBlockReason, ...]]:
        mappings: dict[str, dict[str, Any]] = {}
        malformed = False
        for item in mapping_payload:
            torrent_path = item.get("torrent_path")
            if not isinstance(torrent_path, str) or torrent_path in mappings:
                malformed = True
                continue
            mappings[torrent_path] = item
        inventory_by_path = {item.source_path: item for item in inventory}
        actions: list[ExecutionPlanAction] = []
        blockers: list[ExecutionPlanBlockReason] = []
        if malformed:
            blockers.append(ExecutionPlanBlockReason.MAPPING_UNSUPPORTED)

        for torrent_file in torrent_files:
            if torrent_file.padding:
                actions.append(
                    ExecutionPlanAction(
                        torrent_path=torrent_file.path,
                        kind=ExecutionPlanActionKind.PROTOCOL_PADDING,
                        length=torrent_file.length,
                    )
                )
                continue
            if torrent_file.zero_length:
                actions.append(
                    ExecutionPlanAction(
                        torrent_path=torrent_file.path,
                        kind=ExecutionPlanActionKind.ZERO_LENGTH,
                        length=0,
                    )
                )
                continue

            mapping = mappings.get(torrent_file.path)
            if mapping is None:
                blockers.append(ExecutionPlanBlockReason.MAPPING_UNSUPPORTED)
                actions.append(
                    ExecutionPlanAction(
                        torrent_path=torrent_file.path,
                        kind=ExecutionPlanActionKind.CLIENT_FETCH,
                        length=torrent_file.length,
                    )
                )
                continue
            state = mapping.get("state")
            if state == FileMappingState.MISSING.value:
                actions.append(
                    ExecutionPlanAction(
                        torrent_path=torrent_file.path,
                        kind=ExecutionPlanActionKind.CLIENT_FETCH,
                        length=torrent_file.length,
                    )
                )
                continue
            if state != FileMappingState.MAPPED.value:
                blockers.append(ExecutionPlanBlockReason.MAPPING_UNSUPPORTED)
                actions.append(
                    ExecutionPlanAction(
                        torrent_path=torrent_file.path,
                        kind=ExecutionPlanActionKind.CLIENT_FETCH,
                        length=torrent_file.length,
                    )
                )
                continue

            source_path = mapping.get("source_path")
            evidence_snapshot = mapping.get("snapshot")
            source = inventory_by_path.get(source_path) if isinstance(source_path, str) else None
            if (
                source is None
                or source.length != torrent_file.length
                or not _file_snapshot_matches_payload(source.snapshot, evidence_snapshot)
            ):
                blockers.append(ExecutionPlanBlockReason.MAPPING_UNSUPPORTED)
                actions.append(
                    ExecutionPlanAction(
                        torrent_path=torrent_file.path,
                        kind=ExecutionPlanActionKind.CLIENT_FETCH,
                        length=torrent_file.length,
                    )
                )
                continue
            actions.append(
                ExecutionPlanAction(
                    torrent_path=torrent_file.path,
                    kind=ExecutionPlanActionKind.HARDLINK,
                    length=torrent_file.length,
                    source_relative_path=source.relative_path,
                    source_snapshot=source.snapshot,
                )
            )

        return tuple(actions), tuple(blockers)

    def _inspect_target_layout(
        self,
        target_root: Path,
        actions: tuple[ExecutionPlanAction, ...],
    ) -> _TargetLayout:
        try:
            root_stat = target_root.stat(follow_symlinks=False)
        except OSError as exc:
            raise ApplicationError(
                code="EXECUTION_PLAN_TARGET_ROOT_NOT_FOUND",
                status=404,
                title="目标根目录不可用",
                detail="目标根目录在执行计划检查期间不可见",
            ) from exc
        if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
            raise _execution_target_root_invalid("目标根必须是真实目录且不能是符号链接")

        device = root_stat.st_dev
        directories: set[str] = set()
        blockers: set[ExecutionPlanBlockReason] = set()
        for action in actions:
            parts = action.torrent_path.split("/")
            current = target_root
            missing_parent = False
            unsafe_parent = False
            for index, part in enumerate(parts[:-1]):
                current = current / part
                relative = "/".join(parts[: index + 1])
                if missing_parent:
                    directories.add(relative)
                    continue
                if unsafe_parent:
                    break
                try:
                    item_stat = current.stat(follow_symlinks=False)
                except FileNotFoundError:
                    directories.add(relative)
                    missing_parent = True
                    continue
                except OSError:
                    blockers.add(ExecutionPlanBlockReason.TARGET_PARENT_UNSAFE)
                    unsafe_parent = True
                    continue
                if (
                    stat.S_ISLNK(item_stat.st_mode)
                    or not stat.S_ISDIR(item_stat.st_mode)
                    or item_stat.st_dev != device
                ):
                    blockers.add(ExecutionPlanBlockReason.TARGET_PARENT_UNSAFE)
                    unsafe_parent = True

            if not missing_parent and not unsafe_parent:
                target = target_root.joinpath(*parts)
                try:
                    target.stat(follow_symlinks=False)
                except FileNotFoundError:
                    pass
                except OSError:
                    blockers.add(ExecutionPlanBlockReason.TARGET_PARENT_UNSAFE)
                else:
                    blockers.add(ExecutionPlanBlockReason.TARGET_EXISTS)

            if (
                action.kind is ExecutionPlanActionKind.HARDLINK
                and action.source_snapshot is not None
                and action.source_snapshot.device != device
            ):
                blockers.add(ExecutionPlanBlockReason.CROSS_DEVICE)

        return _TargetLayout(
            device=device,
            create_directories=tuple(sorted(directories)),
            blocked_reasons=tuple(sorted(blockers, key=lambda item: item.value)),
        )

    def _assert_execution_plan_inputs(
        self,
        session: Session,
        snapshot: ExecutionPlanSnapshot,
    ) -> None:
        task = TaskRepository(session).get(snapshot.task_id)
        gate = TaskExecutionGateRepository(session).latest(snapshot.task_unit_id)
        candidate = TaskCandidateRepository(session).get(snapshot.candidate_id)
        unit = TaskUnitRepository(session).get(snapshot.task_unit_id)
        try:
            _, resolved_target = self._resolve_execution_target_root(snapshot.target_root)
            target_downloader = self._target_downloader_plan_binding(
                session,
                snapshot.target_downloader_id,
                resolved_target,
                require_client_verification=snapshot.client_check_required,
            )
        except ApplicationError as exc:
            raise _execution_plan_input_changed() from exc
        if (
            task is None
            or task.version != snapshot.task_version
            or gate is None
            or gate.id != snapshot.execution_gate_id
            or gate.gate_digest != snapshot.execution_gate_digest
            or not gate.eligible
            or candidate is None
            or candidate.preflight_snapshot_id != snapshot.preflight_snapshot_id
            or candidate.metainfo_digest != snapshot.metainfo_digest
            or unit is None
            or unit.source_inventory_digest != snapshot.source_inventory_digest
            or target_downloader.downloader_version != snapshot.target_downloader_version
            or target_downloader.binding_digest != snapshot.target_downloader_binding_digest
            or target_downloader.remote_save_path != snapshot.target_remote_save_path
        ):
            raise _execution_plan_input_changed()

    def _target_downloader_plan_binding(
        self,
        session: Session,
        downloader_id: str,
        resolved_target: Path,
        *,
        require_client_verification: bool = False,
    ) -> _TargetDownloaderPlanBinding:
        normalized_id = downloader_id.strip()
        if not normalized_id:
            raise ApplicationError(
                code="EXECUTION_PLAN_TARGET_DOWNLOADER_REQUIRED",
                status=422,
                title="缺少目标下载器",
                detail="execution plan 必须显式绑定目标 qBittorrent 配置",
            )
        record = DownloaderRepository(session).get(normalized_id)
        if record is None:
            raise ApplicationError(
                code="EXECUTION_PLAN_TARGET_DOWNLOADER_NOT_FOUND",
                status=404,
                title="目标下载器不存在",
                detail="指定目标下载器配置不存在",
            )
        if DownloaderKind(record.type) is not DownloaderKind.QBITTORRENT:
            raise ApplicationError(
                code="EXECUTION_PLAN_TARGET_DOWNLOADER_UNSUPPORTED",
                status=409,
                title="目标下载器类型暂不支持",
                detail="M3 当前 execution plan 写链只支持 qBittorrent",
            )
        if (
            not record.enabled
            or record.connection_status != ProbeStatus.OK.value
            or record.path_mapping_status != ProbeStatus.OK.value
            or record.secret_id is None
        ):
            raise ApplicationError(
                code="EXECUTION_PLAN_TARGET_DOWNLOADER_NOT_READY",
                status=409,
                title="目标下载器安全门未就绪",
                detail="目标 qBittorrent 必须已启用且连接、路径映射、凭证均保持有效",
            )
        if require_client_verification and (
            record.capabilities.get("supports_force_recheck") is not True
            or record.capabilities.get("supports_verify_progress") is not True
        ):
            raise ApplicationError(
                code="EXECUTION_PLAN_TARGET_DOWNLOADER_NOT_READY",
                status=409,
                title="目标下载器客户端校验能力未就绪",
                detail="CLIENT_VERIFYING 计划要求 qBittorrent 明确支持强制 recheck 与校验进度读取",
            )
        try:
            mappings = tuple(
                PathMappingRule(
                    remote_prefix=item["remote_prefix"],
                    container_prefix=item["container_prefix"],
                )
                for item in record.path_mappings
            )
            remote_save_path = reverse_map_container_path_unique(
                resolved_target,
                list(mappings),
                allowed_root=self._data_root,
            )
            binding_digest = downloader_execution_binding_digest(
                downloader_id=record.id,
                version=record.version,
                kind=DownloaderKind(record.type),
                enabled=record.enabled,
                connection_status=ProbeStatus(record.connection_status),
                path_mapping_status=ProbeStatus(record.path_mapping_status),
                path_mappings=mappings,
                capabilities=deepcopy(record.capabilities),
            )
        except (DomainViolation, KeyError, TypeError, ValueError) as exc:
            raise ApplicationError(
                code="EXECUTION_PLAN_TARGET_MAPPING_INVALID",
                status=409,
                title="目标下载器路径映射不可用",
                detail="target_root 必须能唯一反向映射到目标 qBittorrent 保存路径",
            ) from exc
        return _TargetDownloaderPlanBinding(
            downloader_id=record.id,
            downloader_version=record.version,
            binding_digest=binding_digest,
            remote_save_path=remote_save_path,
        )

    def submit_review(
        self,
        unit_id: str,
        *,
        expected_version: int,
        approved_candidate_id: str | None,
        rejected_candidate_ids: tuple[str, ...],
        manual_mappings: tuple[ManualReviewMapping, ...],
        note: str | None,
        actor_kind: str,
        actor_id: str,
    ) -> TaskReviewView:
        if expected_version < 0:
            raise _review_input_invalid("expected_version 不能小于 0")
        try:
            state = ReviewState(
                approved_candidate_id=approved_candidate_id,
                rejected_candidate_ids=rejected_candidate_ids,
                manual_mappings=manual_mappings,
                note=note,
            )
        except ValueError as exc:
            raise _review_input_invalid(str(exc)) from exc

        with self._session_factory() as session:
            unit = TaskUnitRepository(session).get(unit_id)
            if unit is None:
                raise _review_unit_not_found()
            task_id = unit.task_id
            snapshot = PreflightSnapshotRepository(session).latest_for_task(task_id)
            if snapshot is None:
                raise _review_preflight_not_found()
            _ensure_review_unit_matches_snapshot(unit, snapshot)
            snapshot_id = snapshot.id
            snapshot_inventory_digest = snapshot.source_inventory_digest
            unit_source_root = unit.source_root
            candidates = TaskCandidateRepository(session).list_for_snapshot(snapshot_id)
            candidate_state = {
                item.id: (
                    item.rejected,
                    item.selected_for_verification,
                    item.verification_level,
                    deepcopy(item.evidence),
                )
                for item in candidates
            }

        preflight = self.latest_preflight(task_id)
        if preflight.id != snapshot_id or not preflight.current:
            reasons = ",".join(preflight.stale_reasons) or "LATEST_SNAPSHOT_CHANGED"
            raise ApplicationError(
                code="REVIEW_PREFLIGHT_STALE",
                status=409,
                title="预演证据已失效",
                detail=f"人工审核只能绑定当前 preflight：{reasons}",
            )

        referenced_ids = set(state.rejected_candidate_ids)
        if state.approved_candidate_id is not None:
            referenced_ids.add(state.approved_candidate_id)
        unknown_ids = sorted(referenced_ids - candidate_state.keys())
        if unknown_ids:
            raise _review_input_invalid("审核引用了不属于当前 preflight 的候选")

        approved = (
            candidate_state[state.approved_candidate_id]
            if state.approved_candidate_id is not None
            else None
        )
        if approved is not None and approved[0]:
            raise ApplicationError(
                code="REVIEW_CANDIDATE_HARD_REJECTED",
                status=409,
                title="候选存在硬冲突",
                detail="硬冲突候选不能被人工批准绕过",
            )
        if state.manual_mappings:
            assert approved is not None
            self._validate_manual_mappings(
                source_root=unit_source_root,
                expected_source_inventory_digest=snapshot_inventory_digest,
                candidate_evidence=approved[3],
                mappings=state.manual_mappings,
            )

        requires_reverification = bool(state.manual_mappings)
        if approved is not None and (
            not approved[1] or approved[2] != "FULL_VERIFIED" or approved[3].get("error_code")
        ):
            requires_reverification = True

        final_preflight = self.latest_preflight(task_id)
        if final_preflight.id != snapshot_id or not final_preflight.current:
            reasons = ",".join(final_preflight.stale_reasons) or "LATEST_SNAPSHOT_CHANGED"
            raise ApplicationError(
                code="REVIEW_PREFLIGHT_STALE",
                status=409,
                title="预演证据已失效",
                detail=f"提交审核前 preflight 已变化：{reasons}",
            )

        with self._session_factory() as session:
            task_repository = TaskRepository(session)
            task = task_repository.get(task_id)
            if task is None:
                raise _task_not_found()
            latest_snapshot = PreflightSnapshotRepository(session).latest_for_task(task_id)
            if latest_snapshot is None or latest_snapshot.id != snapshot_id:
                raise ApplicationError(
                    code="REVIEW_PREFLIGHT_STALE",
                    status=409,
                    title="预演证据已变化",
                    detail="提交审核前已产生新的 preflight，请重新加载",
                )
            latest_event = task_repository.latest_event(task_id)
            bridge_open = (
                task.status == TaskStatus.PREFLIGHT.value and task.version == snapshot.task_version
            )
            if not bridge_open and not _review_bridge_is_current(
                task_version=task.version,
                task_status=task.status,
                snapshot_task_version=snapshot.task_version,
                latest_event=latest_event,
            ):
                raise ApplicationError(
                    code="REVIEW_STATE_INVALID",
                    status=409,
                    title="任务状态不允许审核",
                    detail="人工审核只能从当前 PREFLIGHT 或已打开的 AWAITING_CONFIRMATION 状态提交",
                )
            try:
                record = TaskReviewRepository(session).append(
                    task_id=task_id,
                    task_unit_id=unit_id,
                    preflight_snapshot_id=snapshot_id,
                    expected_version=expected_version,
                    state=state,
                    requires_reverification=requires_reverification,
                    actor_kind=actor_kind,
                    actor_id=actor_id,
                )
            except ValueError as exc:
                if str(exc) == "REVIEW_VERSION_CONFLICT":
                    raise ApplicationError(
                        code="REVIEW_VERSION_CONFLICT",
                        status=409,
                        title="审核版本冲突",
                        detail="审核 revision 已变化，请重新加载后再提交",
                    ) from exc
                raise
            if bridge_open:
                task_repository.transition(
                    task_id=task_id,
                    expected_version=task.version,
                    to_status=TaskStatus.AWAITING_CONFIRMATION,
                    event_type="REVIEW_OPENED",
                    reason="已提交首个人工审核 revision，进入待确认状态",
                )
            session.commit()
            return self._review_view(record)

    def _apply_review_mappings(
        self,
        automatic: tuple[AutoMappingDecision, ...],
        *,
        manual_mappings: tuple[dict[str, str], ...],
        inventory: tuple[SourceFileCandidate, ...],
    ) -> tuple[AutoMappingDecision, ...]:
        overrides: dict[str, str] = {}
        for item in manual_mappings:
            torrent_path = item.get("torrent_path")
            source_relative_path = item.get("source_relative_path")
            if not torrent_path or not source_relative_path:
                raise _review_mapping_invalid("审核 revision 中的人工映射证据无效")
            overrides[torrent_path] = source_relative_path
        inventory_by_relative = {item.relative_path: item for item in inventory}
        applied: list[AutoMappingDecision] = []
        used: set[str] = set()
        for mapping in automatic:
            source_relative_path = overrides.get(mapping.torrent_path)
            if source_relative_path is None:
                applied.append(mapping)
                continue
            if mapping.state is not FileMappingState.AMBIGUOUS:
                raise _review_mapping_invalid("人工映射只能覆盖当前重新计算出的 AMBIGUOUS 项")
            source = inventory_by_relative.get(source_relative_path)
            if source is None or source.source_path not in mapping.candidate_paths:
                raise _review_mapping_invalid("人工映射源文件已不属于当前歧义候选集合")
            applied.append(
                AutoMappingDecision(
                    torrent_path=mapping.torrent_path,
                    state=FileMappingState.MAPPED,
                    method=MappingMethod.MANUAL_REVIEW,
                    source_path=source.source_path,
                    snapshot=source.snapshot,
                    candidate_paths=(source.source_path,),
                )
            )
            used.add(mapping.torrent_path)
        if used != set(overrides):
            raise _review_mapping_invalid("审核 revision 引用了当前 torrent 中不存在的人工映射路径")
        return tuple(applied)

    def _validate_manual_mappings(
        self,
        *,
        source_root: str,
        expected_source_inventory_digest: str,
        candidate_evidence: dict[str, Any],
        mappings: tuple[ManualReviewMapping, ...],
    ) -> None:
        raw_mappings = candidate_evidence.get("mappings")
        if not isinstance(raw_mappings, list):
            raise _review_mapping_invalid("候选缺少可验证的文件映射证据")
        ambiguous: dict[str, tuple[str, ...]] = {}
        for item in raw_mappings:
            if not isinstance(item, dict) or item.get("state") != "AMBIGUOUS":
                continue
            torrent_path = item.get("torrent_path")
            candidate_paths = item.get("candidate_paths")
            if (
                isinstance(torrent_path, str)
                and isinstance(candidate_paths, list)
                and all(isinstance(value, str) for value in candidate_paths)
            ):
                ambiguous[torrent_path] = tuple(candidate_paths)

        _, resolved_root = self._resolve_source_root(source_root)
        try:
            inventory = scan_source_inventory(resolved_root)
        except DomainViolation as exc:
            raise ApplicationError(
                code="REVIEW_SOURCE_UNAVAILABLE",
                status=409,
                title="源文件不可用",
                detail="提交人工映射时无法重新确认源文件清单",
            ) from exc
        if source_inventory_digest(inventory) != expected_source_inventory_digest:
            raise ApplicationError(
                code="REVIEW_PREFLIGHT_STALE",
                status=409,
                title="源文件已经变化",
                detail="人工映射提交前源 inventory 已变化，请重新分析",
            )
        inventory_by_relative = {item.relative_path: item for item in inventory}
        for mapping in mappings:
            allowed_paths = ambiguous.get(mapping.torrent_path)
            if allowed_paths is None:
                raise _review_mapping_invalid("人工映射只能解决当前证据中的 AMBIGUOUS torrent path")
            source = inventory_by_relative.get(mapping.source_relative_path)
            if source is None or source.source_path not in allowed_paths:
                raise _review_mapping_invalid("人工映射源文件必须来自该歧义项的候选集合")

    def _task_unit_key(self, task_id: str) -> str:
        with self._session_factory() as session:
            task = TaskRepository(session).get(task_id)
            if task is None:
                raise _task_not_found()
            return task.normalized_unit_key

    def _task_version(self, task_id: str) -> int:
        with self._session_factory() as session:
            task = TaskRepository(session).get(task_id)
            if task is None:
                raise _task_not_found()
            return task.version

    def _require_task(self, task_id: str) -> None:
        self._task_version(task_id)

    def _resolve_source_root(self, value: str) -> tuple[str, Path]:
        normalized = unicodedata.normalize("NFC", value.strip())
        has_windows_drive = (
            len(normalized) >= 2 and normalized[0].isalpha() and normalized[1] == ":"
        )
        if (
            not normalized
            or "\x00" in normalized
            or "\\" in normalized
            or normalized.startswith("/")
            or has_windows_drive
        ):
            raise _source_root_invalid("source_root 必须是 /data 下的 POSIX 相对目录")
        if normalized == ".":
            parts: tuple[str, ...] = ()
        else:
            parts = tuple(normalized.split("/"))
            if any(not part or part in {".", ".."} for part in parts):
                raise _source_root_invalid("source_root 包含不安全路径段")

        try:
            base_stat = self._data_root.stat(follow_symlinks=False)
        except OSError as exc:
            raise _source_root_invalid("数据根目录不可用") from exc
        if stat.S_ISLNK(base_stat.st_mode) or not stat.S_ISDIR(base_stat.st_mode):
            raise _source_root_invalid("数据根目录必须是真实目录且不能是符号链接")
        try:
            base = self._data_root.resolve(strict=True)
        except OSError as exc:
            raise _source_root_invalid("数据根目录不可用") from exc
        current = self._data_root
        for index, part in enumerate(parts):
            current = current / part
            try:
                item_stat = current.stat(follow_symlinks=False)
            except OSError as exc:
                raise ApplicationError(
                    code="ANALYSIS_SOURCE_ROOT_NOT_FOUND",
                    status=404,
                    title="源目录不存在",
                    detail="source_root 指向的目录不可见",
                ) from exc
            if stat.S_ISLNK(item_stat.st_mode):
                raise _source_root_invalid("source_root 不能经过符号链接")
            if index < len(parts) - 1 and not stat.S_ISDIR(item_stat.st_mode):
                raise _source_root_invalid("source_root 的中间路径不是目录")
        resolved = current.resolve(strict=True)
        if not resolved.is_relative_to(base) or not resolved.is_dir():
            raise _source_root_invalid("source_root 必须解析到 /data 内的真实目录")
        return ("." if not parts else "/".join(parts), resolved)

    def _resolve_execution_target_root(self, value: str) -> tuple[str, Path]:
        normalized = unicodedata.normalize("NFC", value.strip())
        has_windows_drive = (
            len(normalized) >= 2 and normalized[0].isalpha() and normalized[1] == ":"
        )
        if (
            not normalized
            or "\x00" in normalized
            or "\\" in normalized
            or normalized.startswith("/")
            or has_windows_drive
        ):
            raise _execution_target_root_invalid("target_root 必须是 /data 下的 POSIX 相对目录")
        if normalized == ".":
            parts: tuple[str, ...] = ()
        else:
            parts = tuple(normalized.split("/"))
            if any(not part or part in {".", ".."} for part in parts):
                raise _execution_target_root_invalid("target_root 包含不安全路径段")

        try:
            base_stat = self._data_root.stat(follow_symlinks=False)
        except OSError as exc:
            raise _execution_target_root_invalid("数据根目录不可用") from exc
        if stat.S_ISLNK(base_stat.st_mode) or not stat.S_ISDIR(base_stat.st_mode):
            raise _execution_target_root_invalid("数据根目录必须是真实目录且不能是符号链接")
        try:
            base = self._data_root.resolve(strict=True)
        except OSError as exc:
            raise _execution_target_root_invalid("数据根目录不可用") from exc

        current = self._data_root
        for index, part in enumerate(parts):
            current = current / part
            try:
                item_stat = current.stat(follow_symlinks=False)
            except OSError as exc:
                raise ApplicationError(
                    code="EXECUTION_PLAN_TARGET_ROOT_NOT_FOUND",
                    status=404,
                    title="目标根目录不存在",
                    detail="target_root 指向的目录不可见",
                ) from exc
            if stat.S_ISLNK(item_stat.st_mode):
                raise _execution_target_root_invalid("target_root 不能经过符号链接")
            if not stat.S_ISDIR(item_stat.st_mode):
                detail = (
                    "target_root 的中间路径不是目录"
                    if index < len(parts) - 1
                    else "target_root 必须指向目录"
                )
                raise _execution_target_root_invalid(detail)
        resolved = current.resolve(strict=True)
        if not resolved.is_relative_to(base) or not resolved.is_dir():
            raise _execution_target_root_invalid("target_root 必须解析到 /data 内的真实目录")
        return ("." if not parts else "/".join(parts), resolved)

    @staticmethod
    def _unit_view(record: Any) -> TaskUnitView:
        return TaskUnitView(
            id=record.id,
            normalized_unit_key=record.normalized_unit_key,
            kind=record.kind,
            source_root=record.source_root,
            source_relative_path=record.source_relative_path,
            length=record.length,
            source_inventory_digest=record.source_inventory_digest,
            descriptor=deepcopy(record.descriptor),
            discovered_at=record.discovered_at,
        )

    @staticmethod
    def _candidate_view(record: Any) -> TaskCandidateView:
        return TaskCandidateView(
            id=record.id,
            snapshot_id=record.preflight_snapshot_id,
            normalized_unit_key=record.normalized_unit_key,
            site_id=record.site_id,
            torrent_id=record.torrent_id,
            display_name=record.display_name,
            score=record.score,
            rejected=record.rejected,
            selected_for_verification=record.selected_for_verification,
            verification_level=record.verification_level,
            metainfo_digest=record.metainfo_digest,
            error_code=record.error_code,
            evidence=deepcopy(record.evidence),
            created_at=record.created_at,
        )

    @staticmethod
    def _review_view(record: Any) -> TaskReviewView:
        return TaskReviewView(
            id=record.id,
            task_id=record.task_id,
            task_unit_id=record.task_unit_id,
            preflight_snapshot_id=record.preflight_snapshot_id,
            approved_candidate_id=record.approved_candidate_id,
            rejected_candidate_ids=tuple(record.rejected_candidate_ids),
            manual_mappings=tuple(
                ManualReviewMappingView(
                    torrent_path=item["torrent_path"],
                    source_relative_path=item["source_relative_path"],
                )
                for item in record.manual_mappings
            ),
            note=record.note,
            requires_reverification=record.requires_reverification,
            execution_allowed=False,
            actor_kind=record.actor_kind,
            version=record.version,
            created_at=record.created_at,
        )

    @staticmethod
    def _review_verification_view(record: Any) -> ReviewVerificationView:
        return ReviewVerificationView(
            id=record.id,
            review_revision_id=record.review_revision_id,
            review_version=record.review_version,
            candidate_id=record.candidate_id,
            verification_digest=record.verification_digest,
            verification_level=record.verification_level,
            metainfo_digest=record.metainfo_digest,
            execution_allowed=False,
            created_at=record.created_at,
        )

    @staticmethod
    def _execution_gate_view(record: Any, *, current: bool) -> ExecutionGateView:
        verification_source = record.payload.get("verification_source")
        stale_reasons = record.payload.get("preflight_stale_reasons")
        return ExecutionGateView(
            id=record.id,
            gate_digest=record.gate_digest,
            eligible=record.eligible,
            current=current,
            client_check_required=record.client_check_required,
            verification_level=record.verification_level,
            verification_source=(
                verification_source if isinstance(verification_source, str) else None
            ),
            blocked_reasons=tuple(record.blocked_reasons),
            preflight_stale_reasons=(
                tuple(value for value in stale_reasons if isinstance(value, str))
                if isinstance(stale_reasons, list)
                else ()
            ),
            candidate_id=record.candidate_id,
            metainfo_digest=record.metainfo_digest,
            review_revision_id=record.review_revision_id,
            review_version=int(record.payload.get("review_version", 0)),
            side_effects_started=False,
            created_at=record.created_at,
        )

    @staticmethod
    def _execution_plan_view(
        record: Any,
        *,
        current: bool,
        current_reasons: tuple[str, ...],
    ) -> ExecutionPlanView:
        actions = _execution_plan_actions_from_payload(record.payload) or ()
        safe_actions = tuple(
            {
                "torrent_path": action.torrent_path,
                "kind": action.kind.value,
                "length": action.length,
                "source_relative_path": action.source_relative_path,
            }
            for action in actions
        )
        return ExecutionPlanView(
            id=record.id,
            plan_digest=record.plan_digest,
            ready=record.ready,
            current=current,
            current_reasons=current_reasons,
            target_root=record.target_root,
            target_device=record.target_device,
            target_downloader_id=_optional_text(record.payload.get("target_downloader_id")),
            target_downloader_version=_optional_int(
                record.payload.get("target_downloader_version")
            ),
            target_remote_save_path=_optional_text(record.payload.get("target_remote_save_path")),
            verification_level=record.verification_level,
            client_check_required=record.client_check_required,
            hardlink_count=sum(
                action.kind is ExecutionPlanActionKind.HARDLINK for action in actions
            ),
            client_fetch_count=sum(
                action.kind is ExecutionPlanActionKind.CLIENT_FETCH for action in actions
            ),
            create_directory_count=len(
                _string_tuple(record.payload.get("create_directories")) or ()
            ),
            estimated_download_bytes_upper_bound=record.estimated_download_bytes_upper_bound,
            blocked_reasons=tuple(record.blocked_reasons),
            actions=safe_actions,
            execution_allowed=False,
            side_effects_started=False,
            created_at=record.created_at,
        )

    @staticmethod
    def _task_view(record: Any) -> TaskView:
        return TaskView(
            id=record.id,
            type=record.type,
            source_downloader_id=record.source_downloader_id,
            source_hash=record.source_hash,
            normalized_unit_key=record.normalized_unit_key,
            status=record.status,
            error_code=record.error_code,
            version=record.version,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )


def _site_versions_from_payload(payload: dict[str, Any]) -> tuple[tuple[str, int], ...] | None:
    raw = payload.get("site_versions")
    if not isinstance(raw, list):
        return None
    values: list[tuple[str, int]] = []
    for item in raw:
        if (
            not isinstance(item, list)
            or len(item) != 2
            or not isinstance(item[0], str)
            or not isinstance(item[1], int)
            or item[1] < 1
        ):
            return None
        values.append((item[0], item[1]))
    return tuple(sorted(values))


def _execution_plan_actions_from_payload(
    payload: dict[str, Any],
) -> tuple[ExecutionPlanAction, ...] | None:
    try:
        return execution_plan_actions_from_payload(payload)
    except ValueError:
        return None


def _file_snapshot_from_payload(value: object) -> FileSnapshot | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        return None
    device = value.get("device")
    inode = value.get("inode")
    size = value.get("size")
    mtime_ns = value.get("mtime_ns")
    file_type = value.get("file_type")
    if (
        not isinstance(device, int)
        or not isinstance(inode, int)
        or not isinstance(size, int)
        or not isinstance(mtime_ns, int)
        or not isinstance(file_type, str)
    ):
        return None
    return FileSnapshot(
        device=device,
        inode=inode,
        size=size,
        mtime_ns=mtime_ns,
        file_type=file_type,
    )


def _file_snapshot_matches_payload(snapshot: FileSnapshot, value: object) -> bool:
    expected = _file_snapshot_from_payload(value)
    return expected == snapshot


def _string_tuple(value: object) -> tuple[str, ...] | None:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None
    return tuple(value)


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 1 else None


def _execution_plan_blockers(
    value: object,
) -> tuple[ExecutionPlanBlockReason, ...] | None:
    strings = _string_tuple(value)
    if strings is None:
        return None
    try:
        return tuple(ExecutionPlanBlockReason(item) for item in strings)
    except ValueError:
        return None


def _review_bridge_is_current(
    *,
    task_version: int,
    task_status: str,
    snapshot_task_version: int,
    latest_event: TaskEvent | None,
) -> bool:
    return (
        task_status == TaskStatus.AWAITING_CONFIRMATION.value
        and task_version == snapshot_task_version + 1
        and latest_event is not None
        and latest_event.event_type == "REVIEW_OPENED"
        and latest_event.from_status == TaskStatus.PREFLIGHT.value
        and latest_event.to_status == TaskStatus.AWAITING_CONFIRMATION.value
    )


def _ensure_review_unit_matches_snapshot(unit: Any, snapshot: PreflightSnapshotRecord) -> None:
    if (
        unit.task_id != snapshot.task_id
        or unit.normalized_unit_key != snapshot.normalized_unit_key
        or unit.source_inventory_digest != snapshot.source_inventory_digest
    ):
        raise ApplicationError(
            code="REVIEW_UNIT_STALE",
            status=409,
            title="处理单元不是当前预演单元",
            detail="人工审核只能绑定最新 preflight 对应的 TaskUnit",
        )


def _review_unit_not_found() -> ApplicationError:
    return ApplicationError(
        code="REVIEW_UNIT_NOT_FOUND",
        status=404,
        title="处理单元不存在",
        detail="未找到指定 TaskUnit",
    )


def _review_preflight_not_found() -> ApplicationError:
    return ApplicationError(
        code="REVIEW_PREFLIGHT_NOT_FOUND",
        status=404,
        title="预演不存在",
        detail="处理单元所属任务尚未生成 preflight",
    )


def _review_input_invalid(detail: str) -> ApplicationError:
    return ApplicationError(
        code="REVIEW_INPUT_INVALID",
        status=422,
        title="审核输入无效",
        detail=detail,
    )


def _review_mapping_invalid(detail: str) -> ApplicationError:
    return ApplicationError(
        code="REVIEW_MAPPING_INVALID",
        status=422,
        title="人工文件映射无效",
        detail=detail,
    )


def _task_not_found() -> ApplicationError:
    return ApplicationError(
        code="TASK_NOT_FOUND",
        status=404,
        title="任务不存在",
        detail="未找到指定任务",
    )


def _source_root_invalid(detail: str) -> ApplicationError:
    return ApplicationError(
        code="ANALYSIS_SOURCE_ROOT_INVALID",
        status=422,
        title="源目录无效",
        detail=detail,
    )


def _execution_target_root_invalid(detail: str) -> ApplicationError:
    return ApplicationError(
        code="EXECUTION_PLAN_TARGET_ROOT_INVALID",
        status=422,
        title="执行计划目标根无效",
        detail=detail,
    )


def _execution_plan_input_changed() -> ApplicationError:
    return ApplicationError(
        code="EXECUTION_PLAN_INPUT_CHANGED",
        status=409,
        title="执行计划输入已经变化",
        detail="生成执行计划期间 task、gate、候选、源文件或目标树发生变化，请重新检查",
    )
