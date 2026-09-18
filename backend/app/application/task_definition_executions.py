from __future__ import annotations

import asyncio
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from fnmatch import fnmatch
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any, Protocol
from uuid import NAMESPACE_URL, uuid5
from weakref import WeakValueDictionary

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.downloaders import DownloaderService
from backend.app.application.errors import ApplicationError
from backend.app.application.task_actions import (
    ExecuteTaskAction,
    RerunTaskAction,
    TaskActionActor,
    TaskActionService,
)
from backend.app.application.tasks import ExecutionPlanView, TaskAnalysisService
from backend.app.domain.errors import DomainViolation
from backend.app.domain.file_mapping import SourceFileCandidate
from backend.app.domain.operation import OperationKind, OperationStatus, operation_kind
from backend.app.domain.task_approval import (
    TaskApprovalDecisionSource,
    TaskApprovalState,
    build_task_risk_summary,
    preauthorization_covers,
)
from backend.app.domain.task_definition import (
    TaskDefinitionKind,
    TaskDefinitionStatus,
    TaskExecutionPhase,
    TaskExecutionStatus,
    TaskExecutionTrigger,
    TaskInitialScope,
    TaskOverlapPolicy,
    TaskSourceKind,
    TaskStorageMode,
    next_cron_run,
)
from backend.app.domain.task_lifecycle import (
    TaskLifecycleAuthorization,
    TaskLifecycleProjection,
    TaskLifecycleRiskLevel,
    TaskLifecycleStage,
    project_task_lifecycle,
)
from backend.app.domain.task_state import TaskStatus
from backend.app.domain.task_units import SourceTaskFile, TaskUnit, identify_task_units
from backend.app.infrastructure.persistence.models import (
    OperationJournal,
    Site,
    TaskApprovalRecord,
    TaskDefinition,
    TaskExecution,
    TaskExecutionEvent,
    TaskExecutionItem,
    TaskExecutionPolicy,
    TaskFilter,
    TaskOutputPolicy,
    TaskRiskSummaryRecord,
    TaskSchedule,
    TaskSource,
    UnpackTask,
    new_uuid,
    utc_now,
)
from backend.app.infrastructure.persistence.repositories import TaskCreate, TaskRepository
from backend.app.infrastructure.persistence.task_analysis_repositories import (
    TaskExecutionPlanRepository,
    TaskUnitRepository,
)
from backend.app.infrastructure.source_inventory import (
    SourceInventoryPage,
    scan_source_inventory,
    scan_source_inventory_page,
    source_inventory_digest,
)

_MONITOR_LOCKS: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()


class SourceFilterSpec(Protocol):
    @property
    def file_types(self) -> Sequence[str]: ...

    @property
    def video_extensions(self) -> Sequence[str]: ...

    @property
    def archive_extensions(self) -> Sequence[str]: ...

    @property
    def min_size_bytes(self) -> int | None: ...

    @property
    def max_size_bytes(self) -> int | None: ...

    @property
    def include_name(self) -> str | None: ...

    @property
    def exclude_names(self) -> Sequence[str]: ...

    @property
    def ignore_temp_files(self) -> bool: ...

    @property
    def temp_patterns(self) -> Sequence[str]: ...

    @property
    def include_subdirectories(self) -> bool: ...

    @property
    def max_scan_depth(self) -> int | None: ...


def filter_source_inventory(
    filters: SourceFilterSpec,
    inventory: tuple[SourceFileCandidate, ...],
) -> tuple[SourceFileCandidate, ...]:
    selected: list[SourceFileCandidate] = []
    video_extensions = {value.casefold() for value in filters.video_extensions}
    archive_extensions = {value.casefold() for value in filters.archive_extensions}
    for item in inventory:
        path = PurePosixPath(item.relative_path)
        depth = max(0, len(path.parts) - 1)
        if not filters.include_subdirectories and depth > 0:
            continue
        if filters.max_scan_depth is not None and depth > filters.max_scan_depth:
            continue
        if filters.min_size_bytes is not None and item.length < filters.min_size_bytes:
            continue
        if filters.max_size_bytes is not None and item.length > filters.max_size_bytes:
            continue
        lowered = item.relative_path.casefold()
        if filters.include_name and filters.include_name.casefold() not in lowered:
            continue
        if any(value.casefold() in lowered for value in filters.exclude_names):
            continue
        if filters.ignore_temp_files and (
            any(part.startswith(".") for part in path.parts)
            or any(fnmatch(path.name, pattern) for pattern in filters.temp_patterns)
        ):
            continue
        suffix = path.suffix.casefold()
        file_type = (
            "VIDEO"
            if suffix in video_extensions
            else "ARCHIVE"
            if suffix in archive_extensions
            else "ISO"
            if suffix == ".iso"
            else "OTHER"
        )
        if file_type not in filters.file_types:
            continue
        selected.append(item)
    return tuple(selected)


@dataclass(frozen=True, slots=True)
class TaskRiskSummaryView:
    id: str
    execution_plan_id: str
    plan_digest: str
    risk_level: str
    reason_codes: tuple[str, ...]
    action_kinds: tuple[str, ...]
    hardlink_count: int
    client_fetch_count: int
    create_directory_count: int
    estimated_download_bytes_upper_bound: int
    risk_digest: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class TaskApprovalView:
    id: str
    execution_plan_id: str
    plan_digest: str
    state: str
    decision_source: str | None
    actor_kind: str | None
    actor_id: str | None
    decision_note: str | None
    decided_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class TaskExecutionItemView:
    id: str
    unpack_task_id: str | None
    source_object_key: str
    name: str
    source: str
    size_bytes: int | None
    phase: str
    progress: int | None
    result: str | None
    error_code: str | None
    error_summary_zh: str | None
    technical_detail: str | None
    retryable: bool
    retry_count: int
    lifecycle_stage: str
    risk_level: str
    authorization_status: str
    execution_plan_id: str | None
    execution_plan_ready: bool | None
    side_effects_started: bool
    lifecycle_blocked_reasons: tuple[str, ...]
    risk_summary: TaskRiskSummaryView | None
    approval: TaskApprovalView | None
    closure: TaskExecutionClosureView


@dataclass(frozen=True, slots=True)
class TaskExecutionClosureView:
    status: str
    filesystem_status: str
    downloader_status: str
    operation_attention_count: int
    reconcile_required_count: int
    rollback_blocked_count: int
    retention_candidate_count: int
    manual_attention_required: bool
    issue_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TaskExecutionEventView:
    id: str
    event_code: str
    message: str
    trace_id: str
    context: dict[str, Any]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class TaskExecutionView:
    id: str
    task_definition_id: str | None
    task_name: str
    trigger: str
    status: str
    phase: str
    source_execution_id: str | None
    discovered_count: int
    success_count: int
    failed_count: int
    skipped_count: int
    trace_id: str
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    config_snapshot: dict[str, Any]
    items: tuple[TaskExecutionItemView, ...]
    events: tuple[TaskExecutionEventView, ...]


@dataclass(frozen=True, slots=True)
class TaskExecutionListItemView:
    id: str
    task_definition_id: str | None
    task_name: str
    trigger: str
    status: str
    phase: str
    source_execution_id: str | None
    discovered_count: int
    success_count: int
    failed_count: int
    skipped_count: int
    trace_id: str
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class TaskExecutionPageView:
    items: tuple[TaskExecutionListItemView, ...]
    page: int
    page_size: int
    total: int


@dataclass(frozen=True, slots=True)
class TaskMonitorScanView:
    task_definition_id: str
    trigger: str
    outcome: str
    discovered_count: int
    new_count: int
    next_run_at: datetime | None
    execution: TaskExecutionView | None


@dataclass(frozen=True, slots=True)
class AutoRetryRequest:
    execution_id: str
    item_ids: frozenset[str]
    attempt: int


@dataclass(frozen=True, slots=True)
class DueMonitorScan:
    task_definition_id: str
    trigger: TaskExecutionTrigger


@dataclass(frozen=True, slots=True)
class DueLifecycleAdvance:
    task_definition_id: str
    execution_id: str


_FILESYSTEM_OPERATION_KINDS = frozenset(
    {
        OperationKind.FILESYSTEM_DIRECTORY,
        OperationKind.FILESYSTEM_HARDLINK,
        OperationKind.FILESYSTEM_REPAIR_ISOLATION,
        OperationKind.FILESYSTEM_REPAIR_CLEANUP,
    }
)
_DOWNLOADER_OPERATION_KINDS = frozenset(
    kind
    for kind in OperationKind
    if kind not in _FILESYSTEM_OPERATION_KINDS and kind is not OperationKind.OTHER
)
_CLOSURE_ATTENTION_STATUSES = frozenset(
    {
        OperationStatus.INTENT_RECORDED,
        OperationStatus.ROLLBACK_PENDING,
        OperationStatus.RECONCILE_REQUIRED,
        OperationStatus.ROLLBACK_BLOCKED,
    }
)
_RETENTION_CANDIDATE_STATUSES = frozenset({OperationStatus.NOOP, OperationStatus.ROLLED_BACK})


def _task_execution_closure(
    session: Session,
    task: UnpackTask | None,
) -> TaskExecutionClosureView:
    if task is None:
        return TaskExecutionClosureView(
            status="ATTENTION_REQUIRED",
            filesystem_status="UNKNOWN",
            downloader_status="UNKNOWN",
            operation_attention_count=1,
            reconcile_required_count=0,
            rollback_blocked_count=0,
            retention_candidate_count=0,
            manual_attention_required=True,
            issue_codes=("UNPACK_TASK_NOT_FOUND",),
        )
    try:
        task_status = TaskStatus(task.status)
    except ValueError:
        return TaskExecutionClosureView(
            status="ATTENTION_REQUIRED",
            filesystem_status="UNKNOWN",
            downloader_status="UNKNOWN",
            operation_attention_count=1,
            reconcile_required_count=0,
            rollback_blocked_count=0,
            retention_candidate_count=0,
            manual_attention_required=True,
            issue_codes=("TASK_STATUS_UNKNOWN",),
        )
    if task_status not in {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED}:
        return TaskExecutionClosureView(
            status="PENDING",
            filesystem_status="PENDING",
            downloader_status="PENDING",
            operation_attention_count=0,
            reconcile_required_count=0,
            rollback_blocked_count=0,
            retention_candidate_count=0,
            manual_attention_required=False,
            issue_codes=(),
        )

    journals = tuple(
        session.scalars(
            select(OperationJournal)
            .where(OperationJournal.task_id == task.id)
            .order_by(OperationJournal.created_at, OperationJournal.id)
        )
    )
    statuses = tuple(OperationStatus(journal.status) for journal in journals)
    attention = tuple(status for status in statuses if status in _CLOSURE_ATTENTION_STATUSES)
    reconcile_required_count = sum(
        status is OperationStatus.RECONCILE_REQUIRED for status in statuses
    )
    rollback_blocked_count = sum(status is OperationStatus.ROLLBACK_BLOCKED for status in statuses)
    retention_candidate_count = sum(status in _RETENTION_CANDIDATE_STATUSES for status in statuses)

    filesystem_statuses: list[OperationStatus] = []
    downloader_statuses: list[OperationStatus] = []
    unknown_attention = False
    for journal, status in zip(journals, statuses, strict=True):
        kind = operation_kind(journal.operation_type)
        if kind in _FILESYSTEM_OPERATION_KINDS:
            filesystem_statuses.append(status)
        elif kind in _DOWNLOADER_OPERATION_KINDS:
            downloader_statuses.append(status)
        elif status in _CLOSURE_ATTENTION_STATUSES:
            unknown_attention = True

    def subsystem_status(values: list[OperationStatus]) -> str:
        if any(value in _CLOSURE_ATTENTION_STATUSES for value in values):
            return "ATTENTION_REQUIRED"
        if values:
            return "OK"
        return "NOT_APPLICABLE"

    issue_codes: list[str] = []
    if OperationStatus.INTENT_RECORDED in attention:
        issue_codes.append("OPERATION_INTENT_INCOMPLETE")
    if OperationStatus.ROLLBACK_PENDING in attention:
        issue_codes.append("OPERATION_ROLLBACK_PENDING")
    if OperationStatus.RECONCILE_REQUIRED in attention:
        issue_codes.append("OPERATION_RECONCILE_REQUIRED")
    if OperationStatus.ROLLBACK_BLOCKED in attention:
        issue_codes.append("OPERATION_ROLLBACK_BLOCKED")
    if unknown_attention:
        issue_codes.append("OPERATION_KIND_UNKNOWN")

    return TaskExecutionClosureView(
        status="ATTENTION_REQUIRED" if attention else "COMPLETE",
        filesystem_status=subsystem_status(filesystem_statuses),
        downloader_status=subsystem_status(downloader_statuses),
        operation_attention_count=len(attention),
        reconcile_required_count=reconcile_required_count,
        rollback_blocked_count=rollback_blocked_count,
        retention_candidate_count=retention_candidate_count,
        manual_attention_required=rollback_blocked_count > 0 or unknown_attention,
        issue_codes=tuple(issue_codes),
    )


def reconcile_task_execution(
    session: Session,
    record: TaskExecution,
) -> tuple[TaskExecutionItem, ...]:
    """把底层 UnpackTask 当前状态投影回 v0.1.5 执行记录。"""

    items = tuple(
        session.scalars(
            select(TaskExecutionItem)
            .where(TaskExecutionItem.execution_id == record.id)
            .order_by(TaskExecutionItem.created_at, TaskExecutionItem.id)
        )
    )
    now = utc_now()
    for item in items:
        if item.unpack_task_id is None:
            continue
        before = (
            item.phase,
            item.progress,
            item.result,
            item.error_code,
            item.error_summary_zh,
            item.retryable,
        )
        task = session.get(UnpackTask, item.unpack_task_id)
        if task is None:
            item.phase = TaskExecutionPhase.FAILED.value
            item.result = "FAILED"
            item.error_code = "UNPACK_TASK_NOT_FOUND"
            item.error_summary_zh = "底层安全 Run 已不存在"
            item.retryable = False
            item.updated_at = now
            continue
        status = TaskStatus(task.status)
        if status is TaskStatus.DONE:
            closure = _task_execution_closure(session, task)
            item.phase = TaskExecutionPhase.COMPLETED.value
            item.progress = 100
            if closure.status == "ATTENTION_REQUIRED":
                item.result = "PARTIAL_FAILED"
                item.error_code = "TASK_FINALIZATION_ATTENTION_REQUIRED"
                item.error_summary_zh = "执行已完成，但自动校验或对账发现需要处理的异常"
                item.retryable = False
            else:
                item.result = "SUCCESS"
                item.error_code = None
                item.error_summary_zh = None
                item.retryable = False
        elif status is TaskStatus.FAILED:
            item.phase = TaskExecutionPhase.FAILED.value
            item.result = "FAILED"
            item.error_code = task.error_code or "UNPACK_TASK_FAILED"
            item.error_summary_zh = "底层安全 Run 执行失败"
            item.retryable = True
        elif status is TaskStatus.CANCELLED:
            item.phase = TaskExecutionPhase.SKIPPED.value
            item.result = "SKIPPED"
            item.error_code = task.error_code
            item.error_summary_zh = "底层安全 Run 已取消"
            item.retryable = False
        else:
            item.phase = _execution_phase_for_task(status)
            item.result = None
            item.error_code = task.error_code
            item.error_summary_zh = None
            item.retryable = False
        after = (
            item.phase,
            item.progress,
            item.result,
            item.error_code,
            item.error_summary_zh,
            item.retryable,
        )
        if after != before:
            item.updated_at = now
            session.add(
                TaskExecutionEvent(
                    id=new_uuid(),
                    execution_id=record.id,
                    event_code="TASK_EXECUTION_ITEM_STATE_CHANGED",
                    message="Execution item state changed while reconciling the safe unpack run",
                    trace_id=record.trace_id,
                    context={
                        "execution_item_id": item.id,
                        "unpack_task_id": item.unpack_task_id,
                        "from_phase": before[0],
                        "to_phase": after[0],
                        "from_result": before[2],
                        "to_result": after[2],
                        "error_code": after[3],
                    },
                    created_at=now,
                )
            )

    record.discovered_count = len(items)
    record.success_count = sum(item.result == "SUCCESS" for item in items)
    partial_failed_count = sum(item.result == "PARTIAL_FAILED" for item in items)
    record.failed_count = sum(item.result in {"FAILED", "PARTIAL_FAILED"} for item in items)
    record.skipped_count = sum(item.result == "SKIPPED" for item in items)
    terminal = bool(items) and all(
        item.result in {"SUCCESS", "FAILED", "PARTIAL_FAILED", "SKIPPED"} for item in items
    )
    if terminal:
        if record.failed_count == len(items) and partial_failed_count == 0:
            record.status = TaskExecutionStatus.FAILED.value
            record.phase = TaskExecutionPhase.FAILED.value
        elif record.failed_count or partial_failed_count:
            record.status = TaskExecutionStatus.PARTIAL_FAILED.value
            record.phase = TaskExecutionPhase.COMPLETED.value
        else:
            record.status = TaskExecutionStatus.COMPLETED.value
            record.phase = TaskExecutionPhase.COMPLETED.value
        record.finished_at = record.finished_at or now
    else:
        active_phases = {item.phase for item in items if item.result is None}
        record.status = (
            TaskExecutionStatus.RUNNING.value
            if active_phases - {TaskExecutionPhase.WAITING.value}
            else TaskExecutionStatus.PENDING.value
        )
        record.phase = _aggregate_execution_phase(active_phases)
        record.finished_at = None
    return items


def _execution_phase_for_task(status: TaskStatus) -> str:
    if status in {
        TaskStatus.PENDING,
        TaskStatus.PAUSED,
        TaskStatus.RETRY,
        TaskStatus.AWAITING_CONFIRMATION,
    }:
        return TaskExecutionPhase.WAITING.value
    if status is TaskStatus.ANALYZING:
        return TaskExecutionPhase.ANALYZING.value
    if status in {
        TaskStatus.SEARCHING,
        TaskStatus.MATCHING,
        TaskStatus.VERIFYING,
        TaskStatus.PREFLIGHT,
    }:
        return TaskExecutionPhase.SCANNING_SITE.value
    if status in {TaskStatus.LINKING, TaskStatus.ADDING}:
        return TaskExecutionPhase.OUTPUTTING.value
    if status in {TaskStatus.CLIENT_VERIFYING, TaskStatus.SEEDING}:
        return TaskExecutionPhase.VERIFYING.value
    return TaskExecutionPhase.PREPARING.value


def _aggregate_execution_phase(phases: set[str]) -> str:
    if not phases or phases == {TaskExecutionPhase.WAITING.value}:
        return TaskExecutionPhase.WAITING.value
    priority = (
        TaskExecutionPhase.VERIFYING.value,
        TaskExecutionPhase.OUTPUTTING.value,
        TaskExecutionPhase.PREPARING.value,
        TaskExecutionPhase.SCANNING_SITE.value,
        TaskExecutionPhase.ANALYZING.value,
    )
    return next((phase for phase in priority if phase in phases), TaskExecutionPhase.WAITING.value)


class TaskDefinitionExecutionService:
    """把长期任务的来源快照物化为既有 UnpackTask；不越过预演/人工确认安全门。"""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        downloader_service: DownloaderService,
        task_action_service: TaskActionService,
        task_analysis_service: TaskAnalysisService,
        *,
        data_root: Path,
        directory_scan_batch_size: int = 250,
    ) -> None:
        self._session_factory = session_factory
        self._downloader_service = downloader_service
        self._task_action_service = task_action_service
        self._task_analysis_service = task_analysis_service
        self._data_root = data_root.resolve(strict=False)
        if directory_scan_batch_size <= 0:
            raise ValueError("directory_scan_batch_size 必须大于 0")
        self._directory_scan_batch_size = directory_scan_batch_size

    async def create_execution_plan(
        self,
        definition_id: str,
        execution_id: str,
        item_id: str,
    ) -> ExecutionPlanView:
        snapshot = self._load_definition_snapshot(definition_id)
        if snapshot.output.storage_mode != TaskStorageMode.HARDLINK.value:
            raise ApplicationError(
                code="TASK_STORAGE_MODE_NOT_EXECUTABLE",
                status=409,
                title="存放方式尚未接入安全执行器",
                detail="v0.1.5 当前只允许 HARDLINK 进入既有 journal-backed 安全执行链",
            )
        target_downloader_id = (
            snapshot.source.downloader_id
            if snapshot.source.kind == TaskSourceKind.DOWNLOADER.value
            else snapshot.source.config.get("target_downloader_id")
        )
        if not isinstance(target_downloader_id, str) or not target_downloader_id:
            raise ApplicationError(
                code="TASK_TARGET_DOWNLOADER_REQUIRED",
                status=409,
                title="缺少目标下载器",
                detail="目录来源任务必须显式选择目标下载器后才能生成安全执行计划",
            )
        unit_id, _task_id = self._execution_item_unit(
            definition_id=definition_id,
            execution_id=execution_id,
            item_id=item_id,
        )
        return await self._task_analysis_service.create_execution_plan(
            unit_id,
            target_root=snapshot.output.output_directory,
            target_downloader_id=target_downloader_id,
        )

    def _ensure_risk_and_approval(
        self,
        *,
        definition_id: str,
        unit_id: str,
        task_id: str,
        plan: ExecutionPlanView,
    ) -> tuple[TaskRiskSummaryView, TaskApprovalView | None]:
        action_kinds = tuple(
            str(action["kind"]) for action in plan.actions if isinstance(action.get("kind"), str)
        )
        summary = build_task_risk_summary(
            execution_plan_id=plan.id,
            plan_digest=plan.plan_digest,
            action_kinds=action_kinds,
            blocked_reasons=plan.blocked_reasons,
            hardlink_count=plan.hardlink_count,
            client_fetch_count=plan.client_fetch_count,
            create_directory_count=plan.create_directory_count,
            estimated_download_bytes_upper_bound=plan.estimated_download_bytes_upper_bound,
        )
        with self._session_factory() as session:
            now = utc_now()
            stale_pending = tuple(
                session.scalars(
                    select(TaskApprovalRecord).where(
                        TaskApprovalRecord.task_id == task_id,
                        TaskApprovalRecord.execution_plan_id != plan.id,
                        TaskApprovalRecord.state == TaskApprovalState.PENDING.value,
                    )
                )
            )
            for stale in stale_pending:
                stale.state = TaskApprovalState.EXPIRED.value
                stale.actor_kind = "SYSTEM"
                stale.actor_id = "execution-plan-superseded"
                stale.decision_note = "Execution Plan superseded before approval"
                stale.decided_at = now
                stale.updated_at = now

            risk_record = session.scalar(
                select(TaskRiskSummaryRecord).where(
                    TaskRiskSummaryRecord.execution_plan_id == plan.id
                )
            )
            if risk_record is None:
                candidate = TaskRiskSummaryRecord(
                    id=new_uuid(),
                    task_id=task_id,
                    task_unit_id=unit_id,
                    execution_plan_id=plan.id,
                    plan_digest=plan.plan_digest,
                    risk_level=summary.risk_level.value,
                    reason_codes=list(summary.reason_codes),
                    action_kinds=list(summary.action_kinds),
                    hardlink_count=summary.hardlink_count,
                    client_fetch_count=summary.client_fetch_count,
                    create_directory_count=summary.create_directory_count,
                    estimated_download_bytes_upper_bound=(
                        summary.estimated_download_bytes_upper_bound
                    ),
                    risk_digest=summary.risk_digest,
                    created_at=utc_now(),
                )
                try:
                    with session.begin_nested():
                        session.add(candidate)
                        session.flush()
                    risk_record = candidate
                except IntegrityError:
                    risk_record = session.scalar(
                        select(TaskRiskSummaryRecord).where(
                            TaskRiskSummaryRecord.execution_plan_id == plan.id
                        )
                    )
            if risk_record is None:
                raise RuntimeError("risk summary disappeared during create-or-get")
            if (
                risk_record.plan_digest != plan.plan_digest
                or risk_record.risk_digest != summary.risk_digest
            ):
                raise ApplicationError(
                    code="TASK_RISK_SUMMARY_STALE",
                    status=409,
                    title="风险摘要已失效",
                    detail="当前风险摘要与 Execution Plan digest 不一致，拒绝继续授权",
                )

            approval_record = session.scalar(
                select(TaskApprovalRecord).where(TaskApprovalRecord.execution_plan_id == plan.id)
            )
            if summary.risk_level is TaskLifecycleRiskLevel.HIGH and approval_record is None:
                definition = session.get(TaskDefinition, definition_id)
                policy = session.scalar(
                    select(TaskExecutionPolicy).where(
                        TaskExecutionPolicy.task_definition_id == definition_id
                    )
                )
                preauthorized = bool(
                    definition is not None
                    and definition.kind == TaskDefinitionKind.MONITOR.value
                    and policy is not None
                    and preauthorization_covers(
                        enabled=policy.high_risk_preauthorization_enabled,
                        allowed_action_kinds=tuple(policy.high_risk_allowed_action_kinds),
                        action_kinds=summary.action_kinds,
                    )
                )
                candidate_approval = TaskApprovalRecord(
                    id=new_uuid(),
                    task_id=task_id,
                    task_unit_id=unit_id,
                    execution_plan_id=plan.id,
                    risk_summary_id=risk_record.id,
                    plan_digest=plan.plan_digest,
                    state=(
                        TaskApprovalState.APPROVED.value
                        if preauthorized
                        else TaskApprovalState.PENDING.value
                    ),
                    decision_source=(
                        TaskApprovalDecisionSource.PREAUTHORIZED.value if preauthorized else None
                    ),
                    actor_kind="SYSTEM" if preauthorized else None,
                    actor_id="task-definition-policy" if preauthorized else None,
                    decision_note=(
                        "Matched monitor high-risk action allowlist" if preauthorized else None
                    ),
                    idempotency_key_digest=None,
                    decided_at=now if preauthorized else None,
                    created_at=now,
                    updated_at=now,
                )
                try:
                    with session.begin_nested():
                        session.add(candidate_approval)
                        session.flush()
                    approval_record = candidate_approval
                except IntegrityError:
                    approval_record = session.scalar(
                        select(TaskApprovalRecord).where(
                            TaskApprovalRecord.execution_plan_id == plan.id
                        )
                    )
            if approval_record is not None and approval_record.plan_digest != plan.plan_digest:
                raise ApplicationError(
                    code="TASK_APPROVAL_STALE",
                    status=409,
                    title="审批证据已失效",
                    detail="当前 Approval 与 Execution Plan digest 不一致，拒绝继续执行",
                )
            risk_view = self._risk_summary_view(risk_record)
            approval_view = (
                self._approval_view(approval_record) if approval_record is not None else None
            )
            session.commit()
            return risk_view, approval_view

    async def advance_execution(
        self,
        definition_id: str,
        execution_id: str,
        *,
        actor: TaskActionActor,
        idempotency_key: str | None,
    ) -> TaskExecutionView:
        normalized_key = _require_lifecycle_idempotency_key(idempotency_key)
        current = self.get_execution(execution_id)
        if current.task_definition_id != definition_id:
            raise ApplicationError(
                code="TASK_EXECUTION_NOT_FOUND",
                status=404,
                title="执行记录不存在",
                detail="未找到该任务定义下的指定执行记录",
            )
        for item in current.items:
            if item.unpack_task_id is None or item.result is not None:
                continue
            await self._advance_execution_item(
                definition_id=definition_id,
                execution_id=execution_id,
                item_id=item.id,
                actor=actor,
                idempotency_key=normalized_key,
            )
        return self.get_execution(execution_id)

    async def decide_execution_approval(
        self,
        definition_id: str,
        execution_id: str,
        item_id: str,
        *,
        execution_plan_id: str,
        approve: bool,
        note: str | None,
        actor: TaskActionActor,
        idempotency_key: str | None,
        decision_source: TaskApprovalDecisionSource = TaskApprovalDecisionSource.WEB,
    ) -> TaskExecutionView:
        normalized_key = _require_lifecycle_idempotency_key(idempotency_key)
        normalized_note = note.strip() if note is not None else None
        if normalized_note == "":
            normalized_note = None
        if normalized_note is not None and len(normalized_note) > 500:
            raise ApplicationError(
                code="TASK_APPROVAL_NOTE_INVALID",
                status=422,
                title="审批备注无效",
                detail="审批备注不能超过 500 个字符",
            )
        unit_id, task_id = self._execution_item_unit(
            definition_id=definition_id,
            execution_id=execution_id,
            item_id=item_id,
        )
        task = self._load_unpack_task(task_id)
        if TaskStatus(task.status) is not TaskStatus.AWAITING_CONFIRMATION:
            raise ApplicationError(
                code="TASK_APPROVAL_NOT_READY",
                status=409,
                title="当前任务不可审批",
                detail="只有已经通过候选审核并等待执行确认的任务才能进行高风险审批",
            )
        plan = await self.create_execution_plan(definition_id, execution_id, item_id)
        if (
            plan.id != execution_plan_id
            or not plan.current
            or not plan.ready
            or plan.blocked_reasons
        ):
            raise ApplicationError(
                code="TASK_APPROVAL_PLAN_STALE",
                status=409,
                title="审批计划已失效",
                detail="页面中的 Execution Plan 已不是当前可执行计划，请刷新后重新确认",
            )
        risk_summary, approval = self._ensure_risk_and_approval(
            definition_id=definition_id,
            unit_id=unit_id,
            task_id=task_id,
            plan=plan,
        )
        if risk_summary.risk_level != TaskLifecycleRiskLevel.HIGH.value or approval is None:
            raise ApplicationError(
                code="TASK_APPROVAL_NOT_REQUIRED",
                status=409,
                title="当前计划不需要高风险审批",
                detail="只有 HIGH 风险 Execution Plan 才能使用审批接口",
            )

        desired_state = (
            TaskApprovalState.APPROVED.value if approve else TaskApprovalState.REJECTED.value
        )
        key_digest = sha256(normalized_key.encode()).hexdigest()
        with self._session_factory() as session:
            record = session.scalar(
                select(TaskApprovalRecord).where(
                    TaskApprovalRecord.execution_plan_id == execution_plan_id
                )
            )
            if record is None or record.plan_digest != plan.plan_digest:
                raise ApplicationError(
                    code="TASK_APPROVAL_PLAN_STALE",
                    status=409,
                    title="审批计划已失效",
                    detail="Approval 与当前 Execution Plan digest 不一致",
                )
            if record.state != TaskApprovalState.PENDING.value:
                replay = (
                    record.state == desired_state
                    and record.idempotency_key_digest == key_digest
                    and record.actor_kind == actor.kind
                    and record.actor_id == actor.subject_id
                    and record.decision_source == decision_source.value
                )
                if not replay:
                    raise ApplicationError(
                        code="TASK_APPROVAL_ALREADY_DECIDED",
                        status=409,
                        title="该计划已经完成审批",
                        detail="同一 Execution Plan 的审批决定不可被后续请求覆盖",
                    )
            else:
                now = utc_now()
                record.state = desired_state
                record.decision_source = decision_source.value
                record.actor_kind = actor.kind
                record.actor_id = actor.subject_id
                record.decision_note = normalized_note
                record.idempotency_key_digest = key_digest
                record.decided_at = now
                record.updated_at = now
                session.commit()

        self._append_lifecycle_event(
            execution_id=execution_id,
            event_code=("TASK_APPROVAL_APPROVED" if approve else "TASK_APPROVAL_REJECTED"),
            message=(
                f"{decision_source.value} approval authorized the current execution plan"
                if approve
                else f"{decision_source.value} approval rejected the current execution plan"
            ),
            context={
                "execution_item_id": item_id,
                "execution_plan_id": execution_plan_id,
                "risk_digest": risk_summary.risk_digest,
                "decision": desired_state,
                "actor_kind": actor.kind,
                "actor_id": actor.subject_id,
                "decision_source": decision_source.value,
            },
        )
        if approve:
            await self._advance_execution_item(
                definition_id=definition_id,
                execution_id=execution_id,
                item_id=item_id,
                actor=actor,
                idempotency_key=normalized_key,
            )
        return self.get_execution(execution_id)

    async def _advance_execution_item(
        self,
        *,
        definition_id: str,
        execution_id: str,
        item_id: str,
        actor: TaskActionActor,
        idempotency_key: str,
    ) -> None:
        unit_id, task_id = self._execution_item_unit(
            definition_id=definition_id,
            execution_id=execution_id,
            item_id=item_id,
        )
        task = self._load_unpack_task(task_id)
        status = TaskStatus(task.status)
        if status in {TaskStatus.PENDING, TaskStatus.RETRY}:
            source_root = task.checkpoint.get("source_root")
            if not isinstance(source_root, str) or not source_root:
                raise ApplicationError(
                    code="TASK_LIFECYCLE_SOURCE_ROOT_MISSING",
                    status=409,
                    title="统一任务生命周期缺少来源快照",
                    detail="安全 Run 缺少 materialize 阶段冻结的 source_root，拒绝猜测分析路径",
                )
            await self._task_analysis_service.analyze(task.id, source_root=source_root)
            self._append_lifecycle_event(
                execution_id=execution_id,
                event_code="TASK_LIFECYCLE_ANALYZED",
                message="Lifecycle analysis and preflight completed without starting side effects",
                context={"execution_item_id": item_id, "unpack_task_id": task.id},
            )
            task = self._load_unpack_task(task_id)
            status = TaskStatus(task.status)

        if status is not TaskStatus.AWAITING_CONFIRMATION:
            return

        gate = self._task_analysis_service.refresh_execution_gate(unit_id)
        if not gate.current or not gate.eligible:
            self._append_lifecycle_event(
                execution_id=execution_id,
                event_code="TASK_LIFECYCLE_GATE_BLOCKED",
                message="Lifecycle stopped because the execution gate is not eligible",
                context={
                    "execution_item_id": item_id,
                    "blocked_reasons": list(gate.blocked_reasons),
                },
            )
            return

        plan = await self.create_execution_plan(definition_id, execution_id, item_id)
        risk_summary, approval = self._ensure_risk_and_approval(
            definition_id=definition_id,
            unit_id=unit_id,
            task_id=task_id,
            plan=plan,
        )
        risk = TaskLifecycleRiskLevel(risk_summary.risk_level)
        if not plan.current or not plan.ready or plan.blocked_reasons:
            self._append_lifecycle_event(
                execution_id=execution_id,
                event_code="TASK_LIFECYCLE_PLAN_BLOCKED",
                message="Lifecycle stopped because the execution plan is not ready",
                context={
                    "execution_item_id": item_id,
                    "execution_plan_id": plan.id,
                    "blocked_reasons": list(plan.blocked_reasons),
                },
            )
            return
        if risk is not TaskLifecycleRiskLevel.LOW:
            if approval is not None and approval.state == TaskApprovalState.REJECTED.value:
                self._append_lifecycle_event(
                    execution_id=execution_id,
                    event_code="TASK_LIFECYCLE_APPROVAL_REJECTED",
                    message="Lifecycle stopped because the current execution plan was rejected",
                    context={
                        "execution_item_id": item_id,
                        "execution_plan_id": plan.id,
                        "risk_level": risk.value,
                        "approval_id": approval.id,
                    },
                )
                return
            if approval is not None and approval.state == TaskApprovalState.APPROVED.value:
                self._append_lifecycle_event(
                    execution_id=execution_id,
                    event_code=(
                        "TASK_LIFECYCLE_PREAUTHORIZED"
                        if approval.decision_source
                        == TaskApprovalDecisionSource.PREAUTHORIZED.value
                        else "TASK_LIFECYCLE_APPROVED"
                    ),
                    message="High-risk current execution plan has valid approval evidence",
                    context={
                        "execution_item_id": item_id,
                        "execution_plan_id": plan.id,
                        "risk_level": risk.value,
                        "approval_id": approval.id,
                        "decision_source": approval.decision_source,
                    },
                )
            else:
                self._append_lifecycle_event(
                    execution_id=execution_id,
                    event_code="TASK_LIFECYCLE_APPROVAL_REQUIRED",
                    message=(
                        "Lifecycle stopped before side effects because explicit approval "
                        "is required"
                    ),
                    context={
                        "execution_item_id": item_id,
                        "execution_plan_id": plan.id,
                        "risk_level": risk.value,
                        "approval_id": approval.id if approval is not None else None,
                    },
                )
                return

        await self._task_action_service.execute(
            ExecuteTaskAction(task_id=task_id, execution_plan_id=plan.id),
            actor=actor,
            idempotency_key=_lifecycle_item_idempotency_key(
                idempotency_key,
                execution_id=execution_id,
                item_id=item_id,
                plan_id=plan.id,
            ),
        )
        self._append_lifecycle_event(
            execution_id=execution_id,
            event_code=(
                "TASK_LIFECYCLE_AUTO_AUTHORIZED"
                if risk is TaskLifecycleRiskLevel.LOW
                else "TASK_LIFECYCLE_APPROVED_EXECUTION"
            ),
            message=(
                "Current execution plan was authorized and handed to the journal-backed executor"
            ),
            context={
                "execution_item_id": item_id,
                "execution_plan_id": plan.id,
                "risk_level": risk.value,
            },
        )

    async def materialize_manual(self, definition_id: str, *, trace_id: str) -> TaskExecutionView:
        snapshot = self._load_definition_snapshot(definition_id)
        if snapshot.definition.kind != TaskDefinitionKind.MANUAL.value:
            raise ApplicationError(
                code="TASK_EXECUTION_TRIGGER_INVALID",
                status=409,
                title="任务不能手动执行",
                detail="该入口只用于手动拆包任务；监控任务由立即扫描或 Cron 触发",
            )
        if snapshot.definition.status != TaskDefinitionStatus.ENABLED.value:
            raise ApplicationError(
                code="TASK_DEFINITION_NOT_RUNNABLE",
                status=409,
                title="任务当前不可执行",
                detail="任务必须处于 ENABLED，且扫描站点必须可用",
            )
        if (
            snapshot.site is None
            or not snapshot.site.enabled
            or snapshot.site.connection_status == "FAILED"
        ):
            raise ApplicationError(
                code="TASK_DEFINITION_SITE_UNAVAILABLE",
                status=409,
                title="扫描站点不可用",
                detail="手动执行前需要恢复任务绑定站点的可用状态",
            )

        execution_id = new_uuid()
        now = utc_now()
        execution = TaskExecution(
            id=execution_id,
            task_definition_id=snapshot.definition.id,
            task_name=snapshot.definition.name,
            trigger=TaskExecutionTrigger.MANUAL.value,
            status=TaskExecutionStatus.RUNNING.value,
            phase=TaskExecutionPhase.DISCOVERING.value,
            source_execution_id=None,
            trace_id=trace_id,
            config_snapshot=self._config_snapshot(snapshot),
            discovered_count=0,
            success_count=0,
            failed_count=0,
            skipped_count=0,
            started_at=now,
            finished_at=None,
            created_at=now,
        )
        with self._session_factory() as session:
            session.add(execution)
            self._event(
                session,
                execution_id=execution_id,
                trace_id=trace_id,
                event_code="TASK_EXECUTION_STARTED",
                message="Manual task source discovery started",
                context={"task_definition_id": snapshot.definition.id},
            )
            session.commit()

        try:
            if snapshot.source.kind == TaskSourceKind.DOWNLOADER.value:
                items = await self._materialize_downloader(snapshot, execution_id, trace_id)
            else:
                items = self._materialize_directory(snapshot, execution_id, trace_id)
        except Exception as exc:
            self._mark_execution_failed(execution_id, trace_id, exc)
            raise

        with self._session_factory() as session:
            record = session.get(TaskExecution, execution_id)
            if record is None:
                raise RuntimeError("task execution disappeared during materialization")
            record.discovered_count = len(items)
            record.failed_count = sum(item.result == "FAILED" for item in items)
            record.skipped_count = sum(item.result == "SKIPPED" for item in items)
            record.status = (
                TaskExecutionStatus.FAILED.value
                if record.failed_count and record.failed_count == len(items)
                else TaskExecutionStatus.PENDING.value
            )
            record.phase = (
                TaskExecutionPhase.FAILED.value
                if record.status == TaskExecutionStatus.FAILED.value
                else TaskExecutionPhase.WAITING.value
            )
            if record.status == TaskExecutionStatus.FAILED.value:
                record.finished_at = utc_now()
            self._event(
                session,
                execution_id=execution_id,
                trace_id=trace_id,
                event_code="TASK_EXECUTION_MATERIALIZED",
                message="Source objects were materialized into safe unpack runs",
                context={
                    "items": len(items),
                    "failed": record.failed_count,
                    "skipped": record.skipped_count,
                },
            )
            session.commit()
        return self.get_execution(execution_id)

    def get_execution(self, execution_id: str) -> TaskExecutionView:
        with self._session_factory() as session:
            record = session.get(TaskExecution, execution_id)
            if record is None:
                raise ApplicationError(
                    code="TASK_EXECUTION_NOT_FOUND",
                    status=404,
                    title="执行记录不存在",
                    detail="未找到指定任务执行记录",
                )
            items = reconcile_task_execution(session, record)
            # reconcile_task_execution may append a structured state-change event.  The
            # task-center Session intentionally does not rely on autoflush, so make the
            # newly reconciled event visible to this very same detail response instead
            # of requiring a second GET.
            session.flush()
            events = tuple(
                session.scalars(
                    select(TaskExecutionEvent)
                    .where(TaskExecutionEvent.execution_id == record.id)
                    .order_by(TaskExecutionEvent.created_at, TaskExecutionEvent.id)
                )
            )
            session.commit()
            item_views_list: list[TaskExecutionItemView] = []
            for item in items:
                closure = self._closure_evidence(session, item)
                lifecycle, risk_summary, approval = self._lifecycle_evidence(
                    session,
                    item,
                    closure=closure,
                )
                item_views_list.append(
                    self._item_view(
                        item,
                        lifecycle,
                        risk_summary=risk_summary,
                        approval=approval,
                        closure=closure,
                    )
                )
            item_views = tuple(item_views_list)
            return TaskExecutionView(
                id=record.id,
                task_definition_id=record.task_definition_id,
                task_name=record.task_name,
                trigger=record.trigger,
                status=record.status,
                phase=record.phase,
                source_execution_id=record.source_execution_id,
                discovered_count=record.discovered_count,
                success_count=record.success_count,
                failed_count=record.failed_count,
                skipped_count=record.skipped_count,
                trace_id=record.trace_id,
                started_at=record.started_at,
                finished_at=record.finished_at,
                created_at=record.created_at,
                config_snapshot=dict(record.config_snapshot or {}),
                items=item_views,
                events=tuple(self._event_view(event) for event in events),
            )

    def _execution_item_unit(
        self,
        *,
        definition_id: str,
        execution_id: str,
        item_id: str,
    ) -> tuple[str, str]:
        with self._session_factory() as session:
            execution = session.get(TaskExecution, execution_id)
            item = session.get(TaskExecutionItem, item_id)
            if (
                execution is None
                or execution.task_definition_id != definition_id
                or item is None
                or item.execution_id != execution_id
                or item.unpack_task_id is None
            ):
                raise ApplicationError(
                    code="TASK_EXECUTION_ITEM_NOT_FOUND",
                    status=404,
                    title="执行对象不存在",
                    detail="未找到属于当前任务执行且已物化安全 Run 的执行对象",
                )
            task = session.get(UnpackTask, item.unpack_task_id)
            if task is None:
                raise ApplicationError(
                    code="TASK_EXECUTION_RUN_NOT_FOUND",
                    status=409,
                    title="底层安全 Run 不存在",
                    detail="执行对象绑定的底层安全 Run 已不存在，不能继续推进生命周期",
                )
            units = TaskUnitRepository(session).list_latest(task.id)
            matching_units = [
                unit for unit in units if unit.normalized_unit_key == task.normalized_unit_key
            ]
            if len(matching_units) != 1:
                raise ApplicationError(
                    code="TASK_EXECUTION_UNIT_NOT_FOUND",
                    status=409,
                    title="无法定位安全执行单元",
                    detail="当前执行对象不能唯一映射到底层 TaskUnit，拒绝猜测生成执行计划",
                )
            return matching_units[0].id, task.id

    def _load_unpack_task(self, task_id: str) -> UnpackTask:
        with self._session_factory() as session:
            task = session.get(UnpackTask, task_id)
            if task is None:
                raise ApplicationError(
                    code="TASK_EXECUTION_RUN_NOT_FOUND",
                    status=409,
                    title="底层安全 Run 不存在",
                    detail="执行对象绑定的底层安全 Run 已不存在，不能继续推进生命周期",
                )
            session.expunge(task)
            return task

    def _append_lifecycle_event(
        self,
        *,
        execution_id: str,
        event_code: str,
        message: str,
        context: dict[str, Any],
    ) -> None:
        with self._session_factory() as session:
            execution = session.get(TaskExecution, execution_id)
            if execution is None:
                raise ApplicationError(
                    code="TASK_EXECUTION_NOT_FOUND",
                    status=404,
                    title="执行记录不存在",
                    detail="统一生命周期写入事件时执行记录已不存在",
                )
            recent_same_code = tuple(
                session.scalars(
                    select(TaskExecutionEvent)
                    .where(
                        TaskExecutionEvent.execution_id == execution_id,
                        TaskExecutionEvent.event_code == event_code,
                    )
                    .order_by(TaskExecutionEvent.created_at.desc(), TaskExecutionEvent.id.desc())
                    .limit(20)
                )
            )
            if any(dict(event.context or {}) == context for event in recent_same_code):
                return
            self._event(
                session,
                execution_id=execution_id,
                trace_id=execution.trace_id,
                event_code=event_code,
                message=message,
                context=context,
            )
            session.commit()

    @staticmethod
    def _lifecycle_evidence(
        session: Session,
        item: TaskExecutionItem,
        *,
        closure: TaskExecutionClosureView,
    ) -> tuple[TaskLifecycleProjection, TaskRiskSummaryView | None, TaskApprovalView | None]:
        if item.unpack_task_id is None:
            return (
                TaskLifecycleProjection(
                    stage=TaskLifecycleStage.COMPLETE,
                    risk_level=TaskLifecycleRiskLevel.UNKNOWN,
                    authorization=TaskLifecycleAuthorization.COMPLETE,
                    execution_plan_id=None,
                    plan_ready=None,
                    side_effects_started=False,
                    blocked_reasons=(),
                ),
                None,
                None,
            )
        task = session.get(UnpackTask, item.unpack_task_id)
        if task is None:
            return (
                TaskLifecycleProjection(
                    stage=TaskLifecycleStage.COMPLETE,
                    risk_level=TaskLifecycleRiskLevel.UNKNOWN,
                    authorization=TaskLifecycleAuthorization.BLOCKED,
                    execution_plan_id=None,
                    plan_ready=None,
                    side_effects_started=False,
                    blocked_reasons=("UNPACK_TASK_NOT_FOUND",),
                ),
                None,
                None,
            )
        units = TaskUnitRepository(session).list_latest(task.id)
        unit = next(
            (value for value in units if value.normalized_unit_key == task.normalized_unit_key),
            None,
        )
        plan = TaskExecutionPlanRepository(session).latest(unit.id) if unit is not None else None
        risk_record = (
            session.scalar(
                select(TaskRiskSummaryRecord).where(
                    TaskRiskSummaryRecord.execution_plan_id == plan.id
                )
            )
            if plan is not None
            else None
        )
        approval_record = (
            session.scalar(
                select(TaskApprovalRecord).where(TaskApprovalRecord.execution_plan_id == plan.id)
            )
            if plan is not None
            else None
        )
        action_kinds: tuple[str, ...] = ()
        blocked_reasons: tuple[str, ...] = ()
        if plan is not None:
            raw_actions = plan.payload.get("actions")
            if isinstance(raw_actions, list):
                action_kinds = tuple(
                    kind
                    for value in raw_actions
                    if isinstance(value, dict) and isinstance((kind := value.get("kind")), str)
                )
            blocked_reasons = tuple(plan.blocked_reasons)
        try:
            status = TaskStatus(task.status)
        except ValueError:
            return (
                TaskLifecycleProjection(
                    stage=TaskLifecycleStage.FINALIZE,
                    risk_level=TaskLifecycleRiskLevel.UNKNOWN,
                    authorization=TaskLifecycleAuthorization.BLOCKED,
                    execution_plan_id=plan.id if plan is not None else None,
                    plan_ready=plan.ready if plan is not None else None,
                    side_effects_started=False,
                    blocked_reasons=("TASK_STATUS_UNKNOWN",),
                ),
                (
                    TaskDefinitionExecutionService._risk_summary_view(risk_record)
                    if risk_record is not None
                    else None
                ),
                (
                    TaskDefinitionExecutionService._approval_view(approval_record)
                    if approval_record is not None
                    else None
                ),
            )
        projection = project_task_lifecycle(
            status=status,
            execution_plan_id=plan.id if plan is not None else None,
            plan_ready=plan.ready if plan is not None else None,
            plan_action_kinds=action_kinds,
            blocked_reasons=blocked_reasons,
            approval_state=approval_record.state if approval_record is not None else None,
        )
        if (
            status in {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED}
            and closure.status == "ATTENTION_REQUIRED"
        ):
            projection = TaskLifecycleProjection(
                stage=TaskLifecycleStage.FINALIZE,
                risk_level=projection.risk_level,
                authorization=TaskLifecycleAuthorization.BLOCKED,
                execution_plan_id=projection.execution_plan_id,
                plan_ready=projection.plan_ready,
                side_effects_started=projection.side_effects_started,
                blocked_reasons=tuple(
                    dict.fromkeys((*projection.blocked_reasons, *closure.issue_codes))
                ),
            )
        return (
            projection,
            (
                TaskDefinitionExecutionService._risk_summary_view(risk_record)
                if risk_record is not None
                else None
            ),
            (
                TaskDefinitionExecutionService._approval_view(approval_record)
                if approval_record is not None
                else None
            ),
        )

    @staticmethod
    def _closure_evidence(
        session: Session,
        item: TaskExecutionItem,
    ) -> TaskExecutionClosureView:
        if item.unpack_task_id is None:
            return TaskExecutionClosureView(
                status="COMPLETE",
                filesystem_status="NOT_APPLICABLE",
                downloader_status="NOT_APPLICABLE",
                operation_attention_count=0,
                reconcile_required_count=0,
                rollback_blocked_count=0,
                retention_candidate_count=0,
                manual_attention_required=False,
                issue_codes=(),
            )
        return _task_execution_closure(session, session.get(UnpackTask, item.unpack_task_id))

    def list_executions(
        self,
        definition_id: str,
        *,
        page: int,
        page_size: int,
        status: str | None = None,
        trigger: str | None = None,
        search: str | None = None,
        started_from: datetime | None = None,
        started_to: datetime | None = None,
    ) -> TaskExecutionPageView:
        if page < 1:
            raise ValueError("page 必须大于等于 1")
        if page_size < 1 or page_size > 200:
            raise ValueError("page_size 必须位于 1..200")
        with self._session_factory() as session:
            if session.get(TaskDefinition, definition_id) is None:
                raise ApplicationError(
                    code="TASK_DEFINITION_NOT_FOUND",
                    status=404,
                    title="任务定义不存在",
                    detail="未找到指定的 v0.1.5 任务定义",
                )
            for execution in session.scalars(
                select(TaskExecution).where(TaskExecution.task_definition_id == definition_id)
            ):
                reconcile_task_execution(session, execution)
            session.flush()
            filters = [TaskExecution.task_definition_id == definition_id]
            if status:
                filters.append(TaskExecution.status == status.strip().upper())
            if trigger:
                filters.append(TaskExecution.trigger == trigger.strip().upper())
            if started_from is not None:
                filters.append(TaskExecution.started_at >= started_from)
            if started_to is not None:
                filters.append(TaskExecution.started_at <= started_to)
            if search and search.strip():
                pattern = f"%{search.strip()}%"
                matching_execution_ids = select(TaskExecutionItem.execution_id).where(
                    or_(
                        TaskExecutionItem.name.ilike(pattern),
                        TaskExecutionItem.source.ilike(pattern),
                        TaskExecutionItem.source_object_key.ilike(pattern),
                    )
                )
                filters.append(TaskExecution.id.in_(matching_execution_ids))

            total = int(session.scalar(select(func.count(TaskExecution.id)).where(*filters)) or 0)
            records = tuple(
                session.scalars(
                    select(TaskExecution)
                    .where(*filters)
                    .order_by(TaskExecution.created_at.desc(), TaskExecution.id.desc())
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            )
            views: list[TaskExecutionListItemView] = []
            for record in records:
                reconcile_task_execution(session, record)
                views.append(self._list_item_view(record))
            session.commit()
            return TaskExecutionPageView(
                items=tuple(views), page=page, page_size=page_size, total=total
            )

    async def scan_monitor(
        self,
        definition_id: str,
        *,
        trigger: TaskExecutionTrigger,
        trace_id: str,
        now: datetime | None = None,
    ) -> TaskMonitorScanView:
        if trigger not in {TaskExecutionTrigger.CRON, TaskExecutionTrigger.IMMEDIATE_SCAN}:
            raise ApplicationError(
                code="TASK_EXECUTION_TRIGGER_INVALID",
                status=409,
                title="监控扫描触发方式无效",
                detail="监控任务只允许由 Cron 或立即扫描触发",
            )
        snapshot = self._load_definition_snapshot(definition_id)
        self._assert_monitor_runnable(snapshot)
        lock = _monitor_lock(definition_id)
        if lock.locked():
            if snapshot.policy.overlap_policy == TaskOverlapPolicy.RUN_ONCE_AFTER.value:
                self._set_run_once_after_pending(definition_id, True)
                outcome = "OVERLAP_QUEUED"
            else:
                outcome = "OVERLAP_SKIPPED"
            return TaskMonitorScanView(
                task_definition_id=definition_id,
                trigger=trigger.value,
                outcome=outcome,
                discovered_count=0,
                new_count=0,
                next_run_at=self._schedule_next_run(definition_id),
                execution=None,
            )
        async with lock:
            # Consume any previously queued RUN_ONCE_AFTER request before this scan starts.
            # A new overlapping request may set it back to True while this scan is running.
            self._set_run_once_after_pending(definition_id, False)
            return await self._scan_monitor_locked(
                snapshot,
                trigger=trigger,
                trace_id=trace_id,
                now=now or utc_now(),
            )

    def list_due_monitor_scans(
        self,
        *,
        now: datetime,
        limit: int,
    ) -> tuple[DueMonitorScan, ...]:
        if limit <= 0:
            raise ValueError("limit 必须大于 0")
        with self._session_factory() as session:
            schedules = tuple(
                session.scalars(
                    select(TaskSchedule)
                    .join(
                        TaskDefinition,
                        TaskDefinition.id == TaskSchedule.task_definition_id,
                    )
                    .where(
                        TaskDefinition.kind == TaskDefinitionKind.MONITOR.value,
                        TaskDefinition.status == TaskDefinitionStatus.ENABLED.value,
                        TaskSchedule.enabled.is_(True),
                    )
                    .order_by(TaskSchedule.next_run_at, TaskSchedule.id)
                    .limit(1000)
                )
            )
            due: list[DueMonitorScan] = []
            for schedule in schedules:
                checkpoint = dict(schedule.scan_checkpoint or {})
                queued = checkpoint.get("run_once_after_pending") is True
                directory_scan_due = checkpoint.get("directory_scan_continuation") is True
                stability_due = False
                raw_stability_next = checkpoint.get("stability_next_check_at")
                if isinstance(raw_stability_next, str):
                    try:
                        stability_next = datetime.fromisoformat(raw_stability_next)
                    except ValueError:
                        stability_next = None
                    if stability_next is not None and stability_next.tzinfo is not None:
                        stability_due = stability_next <= now
                debounce_due = False
                raw_debounce_next = checkpoint.get("debounce_next_check_at")
                if isinstance(raw_debounce_next, str):
                    try:
                        debounce_next = datetime.fromisoformat(raw_debounce_next)
                    except ValueError:
                        debounce_next = None
                    if debounce_next is not None and debounce_next.tzinfo is not None:
                        debounce_due = debounce_next <= now
                cron_due = schedule.next_run_at is None or schedule.next_run_at <= now
                if (
                    not queued
                    and not directory_scan_due
                    and not stability_due
                    and not debounce_due
                    and not cron_due
                ):
                    continue
                due.append(
                    DueMonitorScan(
                        task_definition_id=schedule.task_definition_id,
                        trigger=(
                            TaskExecutionTrigger.CRON
                            if cron_due
                            else TaskExecutionTrigger.IMMEDIATE_SCAN
                        ),
                    )
                )
                if len(due) >= limit:
                    break
            return tuple(due)

    def list_due_lifecycle_advances(self, *, limit: int) -> tuple[DueLifecycleAdvance, ...]:
        if limit <= 0:
            raise ValueError("limit 必须大于 0")
        due: list[DueLifecycleAdvance] = []
        seen_execution_ids: set[str] = set()
        with self._session_factory() as session:
            items = tuple(
                session.scalars(
                    select(TaskExecutionItem)
                    .where(
                        TaskExecutionItem.unpack_task_id.is_not(None),
                        TaskExecutionItem.result.is_(None),
                    )
                    .order_by(TaskExecutionItem.created_at, TaskExecutionItem.id)
                    .limit(2000)
                )
            )
            for item in items:
                if item.execution_id in seen_execution_ids or item.unpack_task_id is None:
                    continue
                execution = session.get(TaskExecution, item.execution_id)
                task = session.get(UnpackTask, item.unpack_task_id)
                if (
                    execution is None
                    or execution.task_definition_id is None
                    or task is None
                    or task.status
                    not in {
                        TaskStatus.PENDING.value,
                        TaskStatus.AWAITING_CONFIRMATION.value,
                    }
                ):
                    continue
                seen_execution_ids.add(execution.id)
                due.append(
                    DueLifecycleAdvance(
                        task_definition_id=execution.task_definition_id,
                        execution_id=execution.id,
                    )
                )
                if len(due) >= limit:
                    break
        return tuple(due)

    def list_due_auto_retries(
        self,
        *,
        now: datetime,
        limit: int,
    ) -> tuple[AutoRetryRequest, ...]:
        if limit <= 0:
            raise ValueError("limit 必须大于 0")
        with self._session_factory() as session:
            executions = tuple(
                session.scalars(
                    select(TaskExecution)
                    .where(TaskExecution.task_definition_id.is_not(None))
                    .order_by(TaskExecution.created_at.desc(), TaskExecution.id.desc())
                    .limit(1000)
                )
            )
            parent_ids = {
                execution.source_execution_id
                for execution in executions
                if execution.source_execution_id is not None
            }
            policy_cache: dict[str, TaskExecutionPolicy | None] = {}
            requests: list[AutoRetryRequest] = []
            for execution in executions:
                if execution.id in parent_ids or execution.task_definition_id is None:
                    continue
                definition_id = execution.task_definition_id
                if definition_id not in policy_cache:
                    policy_cache[definition_id] = session.scalar(
                        select(TaskExecutionPolicy).where(
                            TaskExecutionPolicy.task_definition_id == definition_id
                        )
                    )
                policy = policy_cache[definition_id]
                if policy is None or not policy.auto_retry_enabled or policy.max_auto_retries <= 0:
                    continue
                items = reconcile_task_execution(session, execution)
                eligible: list[TaskExecutionItem] = []
                for item in items:
                    if (
                        item.result != "FAILED"
                        or not item.retryable
                        or item.unpack_task_id is None
                        or item.retry_count >= policy.max_auto_retries
                    ):
                        continue
                    delay = policy.retry_intervals_seconds[item.retry_count]
                    if item.updated_at + timedelta(seconds=delay) <= now:
                        eligible.append(item)
                if not eligible:
                    continue
                requests.append(
                    AutoRetryRequest(
                        execution_id=execution.id,
                        item_ids=frozenset(item.id for item in eligible),
                        attempt=max(item.retry_count for item in eligible) + 1,
                    )
                )
                if len(requests) >= limit:
                    break
            session.commit()
            return tuple(requests)

    async def retry_auto(
        self,
        request: AutoRetryRequest,
        *,
        trace_id: str,
    ) -> TaskExecutionView:
        return await self.retry_failed(
            request.execution_id,
            actor=TaskActionActor("system", "task-definition-driver"),
            idempotency_key=f"auto-retry:{request.execution_id}:{request.attempt}",
            trace_id=trace_id,
            eligible_item_ids=request.item_ids,
        )

    async def retry_failed(
        self,
        execution_id: str,
        *,
        actor: TaskActionActor,
        idempotency_key: str | None,
        trace_id: str,
        eligible_item_ids: frozenset[str] | None = None,
    ) -> TaskExecutionView:
        if idempotency_key is None or not idempotency_key.strip():
            raise ApplicationError(
                code="IDEMPOTENCY_KEY_REQUIRED",
                status=428,
                title="缺少幂等键",
                detail="重试失败对象必须携带 Idempotency-Key",
            )
        key = idempotency_key.strip()
        retry_execution_id = str(
            uuid5(
                NAMESPACE_URL,
                f"packbreaker:task-execution-retry:{execution_id}:{actor.kind}:{actor.subject_id}:{key}",
            )
        )
        with self._session_factory() as session:
            source = session.get(TaskExecution, execution_id)
            if source is None:
                raise ApplicationError(
                    code="TASK_EXECUTION_NOT_FOUND",
                    status=404,
                    title="执行记录不存在",
                    detail="未找到需要重试的来源执行记录",
                )
            source_items = reconcile_task_execution(session, source)
            retryable_items = tuple(
                item
                for item in source_items
                if item.result == "FAILED"
                and item.retryable
                and item.unpack_task_id is not None
                and (eligible_item_ids is None or item.id in eligible_item_ids)
            )
            if not retryable_items:
                session.commit()
                raise ApplicationError(
                    code="TASK_EXECUTION_NO_RETRYABLE_FAILURES",
                    status=409,
                    title="没有可重试失败对象",
                    detail="最近执行中不存在已失败且关联底层安全 Run 的对象",
                )
            retry_execution = session.get(TaskExecution, retry_execution_id)
            if retry_execution is None:
                now = utc_now()
                retry_execution = TaskExecution(
                    id=retry_execution_id,
                    task_definition_id=source.task_definition_id,
                    task_name=source.task_name,
                    trigger=TaskExecutionTrigger.FAILED_RETRY.value,
                    status=TaskExecutionStatus.RUNNING.value,
                    phase=TaskExecutionPhase.PREPARING.value,
                    source_execution_id=source.id,
                    trace_id=trace_id,
                    config_snapshot=dict(source.config_snapshot),
                    discovered_count=len(retryable_items),
                    success_count=0,
                    failed_count=0,
                    skipped_count=0,
                    started_at=now,
                    finished_at=None,
                    created_at=now,
                )
                session.add(retry_execution)
                self._event(
                    session,
                    execution_id=retry_execution_id,
                    trace_id=trace_id,
                    event_code="TASK_FAILED_RETRY_STARTED",
                    message="Retry execution for failed objects started",
                    context={"source_execution_id": execution_id, "items": len(retryable_items)},
                )
                session.commit()
            existing_keys = set(
                session.scalars(
                    select(TaskExecutionItem.source_object_key).where(
                        TaskExecutionItem.execution_id == retry_execution_id
                    )
                )
            )

        for source_item in retryable_items:
            if source_item.source_object_key in existing_keys:
                continue
            assert source_item.unpack_task_id is not None
            item_key = sha256(
                f"{key}\0{execution_id}\0{source_item.id}\0{source_item.retry_count + 1}".encode()
            ).hexdigest()
            try:
                result = await self._task_action_service.rerun(
                    RerunTaskAction(task_id=source_item.unpack_task_id),
                    actor=actor,
                    idempotency_key=item_key,
                )
            except ApplicationError as exc:
                self._record_retry_failure(
                    retry_execution_id,
                    trace_id=trace_id,
                    source_item=source_item,
                    error=exc,
                )
                continue
            with self._session_factory() as session:
                rerun_task = session.get(UnpackTask, result.task_id)
                if rerun_task is None:
                    raise RuntimeError("rerun task disappeared after creation")
                checkpoint = dict(rerun_task.checkpoint)
                checkpoint["task_execution_id"] = retry_execution_id
                rerun_task.checkpoint = checkpoint
                now = utc_now()
                session.add(
                    TaskExecutionItem(
                        id=new_uuid(),
                        execution_id=retry_execution_id,
                        unpack_task_id=rerun_task.id,
                        source_object_key=source_item.source_object_key,
                        name=source_item.name,
                        source=source_item.source,
                        size_bytes=source_item.size_bytes,
                        phase=TaskExecutionPhase.WAITING.value,
                        progress=None,
                        result=None,
                        error_code=None,
                        error_summary_zh=None,
                        technical_detail=None,
                        retryable=False,
                        retry_count=source_item.retry_count + 1,
                        created_at=now,
                        updated_at=now,
                    )
                )
                self._event(
                    session,
                    execution_id=retry_execution_id,
                    trace_id=trace_id,
                    event_code="TASK_FAILED_OBJECT_RERUN_CREATED",
                    message="Failed object was rerun through audited task action service",
                    context={
                        "source_execution_item_id": source_item.id,
                        "parent_unpack_task_id": source_item.unpack_task_id,
                        "unpack_task_id": rerun_task.id,
                    },
                )
                session.commit()

        with self._session_factory() as session:
            record = session.get(TaskExecution, retry_execution_id)
            if record is None:
                raise RuntimeError("retry execution disappeared")
            items = reconcile_task_execution(session, record)
            if any(item.result is None for item in items):
                record.status = TaskExecutionStatus.PENDING.value
                record.phase = TaskExecutionPhase.WAITING.value
            session.commit()
        return self.get_execution(retry_execution_id)

    def _record_retry_failure(
        self,
        execution_id: str,
        *,
        trace_id: str,
        source_item: TaskExecutionItem,
        error: ApplicationError,
    ) -> None:
        now = utc_now()
        with self._session_factory() as session:
            session.add(
                TaskExecutionItem(
                    id=new_uuid(),
                    execution_id=execution_id,
                    unpack_task_id=None,
                    source_object_key=source_item.source_object_key,
                    name=source_item.name,
                    source=source_item.source,
                    size_bytes=source_item.size_bytes,
                    phase=TaskExecutionPhase.FAILED.value,
                    progress=None,
                    result="FAILED",
                    error_code=error.code,
                    error_summary_zh=error.title,
                    technical_detail=error.detail,
                    retryable=False,
                    retry_count=source_item.retry_count + 1,
                    created_at=now,
                    updated_at=now,
                )
            )
            self._event(
                session,
                execution_id=execution_id,
                trace_id=trace_id,
                event_code="TASK_FAILED_OBJECT_RERUN_REJECTED",
                message="Failed object could not create an audited rerun",
                context={
                    "source_execution_item_id": source_item.id,
                    "parent_unpack_task_id": source_item.unpack_task_id,
                    "error_code": error.code,
                },
            )
            session.commit()

    async def _scan_monitor_locked(
        self,
        snapshot: _DefinitionSnapshot,
        *,
        trigger: TaskExecutionTrigger,
        trace_id: str,
        now: datetime,
    ) -> TaskMonitorScanView:
        with self._session_factory() as session:
            schedule = session.scalar(
                select(TaskSchedule).where(
                    TaskSchedule.task_definition_id == snapshot.definition.id
                )
            )
            if schedule is None:
                raise self._source_invalid("监控任务缺少调度记录")
            checkpoint = dict(schedule.scan_checkpoint or {})

        checkpoint.setdefault("schema_version", "packbreaker-monitor-watermark-v1")
        try:
            discovery = await self._discover_monitor_candidates(snapshot, checkpoint)
        except Exception:
            self._update_monitor_schedule(
                snapshot.definition.id,
                trigger=trigger,
                now=now,
                checkpoint=None,
                successful=False,
            )
            raise
        candidates = discovery.candidates
        if discovery.directory_page is not None:
            # Consume the continuation flag for the page being processed. It is set again
            # only after this page is fully accounted for and more pages remain.
            checkpoint["directory_scan_continuation"] = False
            discovered_count = self._directory_scan_discovered_count(
                checkpoint,
                len(candidates),
            )
        else:
            discovered_count = len(candidates)
        seen = {
            value
            for value in checkpoint.get("seen_object_keys", [])
            if isinstance(value, str) and value
        }
        initialized = checkpoint.get("watermark_initialized") is True
        if not initialized and snapshot.policy.initial_scope == TaskInitialScope.NEW_ONLY.value:
            seen.update(candidate.source_object_key for candidate in candidates)
            checkpoint["seen_object_keys"] = sorted(seen)
            checkpoint.pop("stability", None)
            checkpoint.pop("stability_next_check_at", None)
            checkpoint.pop("debounce_next_check_at", None)
            has_more = self._advance_directory_scan(
                checkpoint,
                discovery,
                discovered_count=discovered_count,
            )
            checkpoint["watermark_initialized"] = not has_more
            checkpoint["run_once_after_pending"] = False
            next_run_at = self._update_monitor_schedule(
                snapshot.definition.id,
                trigger=trigger,
                now=now,
                checkpoint=checkpoint,
                successful=True,
            )
            return TaskMonitorScanView(
                task_definition_id=snapshot.definition.id,
                trigger=trigger.value,
                outcome="BASELINE_CONTINUING" if has_more else "BASELINE_ESTABLISHED",
                discovered_count=discovered_count,
                new_count=0,
                next_run_at=next_run_at,
                execution=None,
            )

        checkpoint["watermark_initialized"] = True
        unseen = tuple(
            candidate for candidate in candidates if candidate.source_object_key not in seen
        )
        if not unseen:
            checkpoint.pop("stability", None)
            checkpoint.pop("stability_next_check_at", None)
            checkpoint.pop("debounce_next_check_at", None)
            has_more = self._advance_directory_scan(
                checkpoint,
                discovery,
                discovered_count=discovered_count,
            )
            checkpoint["run_once_after_pending"] = False
            next_run_at = self._update_monitor_schedule(
                snapshot.definition.id,
                trigger=trigger,
                now=now,
                checkpoint=checkpoint,
                successful=True,
            )
            return TaskMonitorScanView(
                task_definition_id=snapshot.definition.id,
                trigger=trigger.value,
                outcome="SCAN_CONTINUING" if has_more else "NO_CHANGES",
                discovered_count=discovered_count,
                new_count=0,
                next_run_at=next_run_at,
                execution=None,
            )

        ready = unseen
        if (
            snapshot.source.kind == TaskSourceKind.DIRECTORY.value
            and snapshot.policy.debounce_seconds > 0
        ):
            quiet_ready: list[_MonitorCandidate] = []
            debounce_checks: list[datetime] = []
            for candidate in unseen:
                if candidate.mtime_ns is None:
                    quiet_ready.append(candidate)
                    continue
                quiet_at = datetime.fromtimestamp(
                    candidate.mtime_ns / 1_000_000_000, tz=UTC
                ) + timedelta(seconds=snapshot.policy.debounce_seconds)
                if quiet_at <= now:
                    quiet_ready.append(candidate)
                else:
                    debounce_checks.append(quiet_at)
            ready = tuple(quiet_ready)
            if debounce_checks:
                checkpoint["debounce_next_check_at"] = min(debounce_checks).isoformat()
            else:
                checkpoint.pop("debounce_next_check_at", None)
            if not ready:
                checkpoint["run_once_after_pending"] = False
                next_run_at = self._update_monitor_schedule(
                    snapshot.definition.id,
                    trigger=trigger,
                    now=now,
                    checkpoint=checkpoint,
                    successful=True,
                )
                return TaskMonitorScanView(
                    task_definition_id=snapshot.definition.id,
                    trigger=trigger.value,
                    outcome="DEBOUNCE_WAIT",
                    discovered_count=discovered_count,
                    new_count=len(unseen),
                    next_run_at=next_run_at,
                    execution=None,
                )

        if (
            snapshot.source.kind == TaskSourceKind.DIRECTORY.value
            and snapshot.policy.stability_detection_enabled
            and snapshot.policy.stability_wait_seconds > 0
        ):
            stability = checkpoint.get("stability")
            if not isinstance(stability, dict):
                stability = {}
            active_stability_keys = {candidate.stability_key for candidate in ready}
            stability = {
                key: value for key, value in stability.items() if key in active_stability_keys
            }
            ready_items: list[_MonitorCandidate] = []
            for candidate in ready:
                state = stability.get(candidate.stability_key)
                first_seen_at: datetime | None = None
                if (
                    isinstance(state, dict)
                    and state.get("object_key") == candidate.source_object_key
                ):
                    raw = state.get("first_seen_at")
                    if isinstance(raw, str):
                        try:
                            first_seen_at = datetime.fromisoformat(raw)
                        except ValueError:
                            first_seen_at = None
                if (
                    first_seen_at is not None
                    and first_seen_at.tzinfo is not None
                    and now - first_seen_at
                    >= timedelta(seconds=snapshot.policy.stability_wait_seconds)
                ):
                    ready_items.append(candidate)
                    stability.pop(candidate.stability_key, None)
                elif first_seen_at is None:
                    stability[candidate.stability_key] = {
                        "object_key": candidate.source_object_key,
                        "first_seen_at": now.isoformat(),
                    }
            checkpoint["stability"] = stability
            stability_checks: list[datetime] = []
            for state in stability.values():
                if not isinstance(state, dict):
                    continue
                raw = state.get("first_seen_at")
                if not isinstance(raw, str):
                    continue
                try:
                    first_seen = datetime.fromisoformat(raw)
                except ValueError:
                    continue
                if first_seen.tzinfo is None:
                    continue
                stability_checks.append(
                    first_seen + timedelta(seconds=snapshot.policy.stability_wait_seconds)
                )
            if stability_checks:
                checkpoint["stability_next_check_at"] = min(stability_checks).isoformat()
            else:
                checkpoint.pop("stability_next_check_at", None)
            ready = tuple(ready_items)

        if not ready:
            checkpoint["run_once_after_pending"] = False
            next_run_at = self._update_monitor_schedule(
                snapshot.definition.id,
                trigger=trigger,
                now=now,
                checkpoint=checkpoint,
                successful=True,
            )
            return TaskMonitorScanView(
                task_definition_id=snapshot.definition.id,
                trigger=trigger.value,
                outcome="STABILITY_WAIT",
                discovered_count=discovered_count,
                new_count=len(unseen),
                next_run_at=next_run_at,
                execution=None,
            )

        execution_id = new_uuid()
        with self._session_factory() as session:
            session.add(
                TaskExecution(
                    id=execution_id,
                    task_definition_id=snapshot.definition.id,
                    task_name=snapshot.definition.name,
                    trigger=trigger.value,
                    status=TaskExecutionStatus.RUNNING.value,
                    phase=TaskExecutionPhase.DISCOVERING.value,
                    source_execution_id=None,
                    trace_id=trace_id,
                    config_snapshot=self._config_snapshot(snapshot),
                    discovered_count=len(ready),
                    success_count=0,
                    failed_count=0,
                    skipped_count=0,
                    started_at=now,
                    finished_at=None,
                    created_at=now,
                )
            )
            self._event(
                session,
                execution_id=execution_id,
                trace_id=trace_id,
                event_code="TASK_MONITOR_SCAN_MATERIALIZING",
                message="Monitor scan discovered new source objects",
                context={
                    "task_definition_id": snapshot.definition.id,
                    "trigger": trigger.value,
                    "discovered": discovered_count,
                    "new": len(unseen),
                    "ready": len(ready),
                },
            )
            session.commit()

        materialized: list[TaskExecutionItemView] = []
        try:
            for candidate in ready:
                materialized.append(
                    self._materialize_unit(
                        snapshot=snapshot,
                        execution_id=execution_id,
                        trace_id=trace_id,
                        source_downloader_id=candidate.source_downloader_id,
                        source_hash=candidate.source_hash,
                        source_root=candidate.source_root,
                        source_label=candidate.source_label,
                        source_object_key=candidate.source_object_key,
                        unit=candidate.unit,
                        all_units=candidate.all_units,
                        inventory_digest=candidate.inventory_digest,
                    )
                )
        except Exception as exc:
            self._mark_execution_failed(execution_id, trace_id, exc)
            self._update_monitor_schedule(
                snapshot.definition.id,
                trigger=trigger,
                now=now,
                checkpoint=checkpoint,
                successful=False,
            )
            raise

        seen.update(
            item.source_object_key for item in materialized if item.unpack_task_id is not None
        )
        checkpoint["seen_object_keys"] = sorted(seen)
        remaining_page_candidates = tuple(
            candidate for candidate in candidates if candidate.source_object_key not in seen
        )
        if not remaining_page_candidates:
            self._advance_directory_scan(
                checkpoint,
                discovery,
                discovered_count=discovered_count,
            )
        checkpoint["run_once_after_pending"] = False
        next_run_at = self._update_monitor_schedule(
            snapshot.definition.id,
            trigger=trigger,
            now=now,
            checkpoint=checkpoint,
            successful=True,
        )
        with self._session_factory() as session:
            record = session.get(TaskExecution, execution_id)
            if record is None:
                raise RuntimeError("monitor execution disappeared during materialization")
            record.status = TaskExecutionStatus.PENDING.value
            record.phase = TaskExecutionPhase.WAITING.value
            self._event(
                session,
                execution_id=execution_id,
                trace_id=trace_id,
                event_code="TASK_MONITOR_SCAN_MATERIALIZED",
                message="Monitor scan materialized new objects into safe unpack runs",
                context={"items": len(materialized)},
            )
            session.commit()
        execution = self.get_execution(execution_id)
        return TaskMonitorScanView(
            task_definition_id=snapshot.definition.id,
            trigger=trigger.value,
            outcome="MATERIALIZED",
            discovered_count=discovered_count,
            new_count=len(unseen),
            next_run_at=next_run_at,
            execution=execution,
        )

    @staticmethod
    def _directory_scan_discovered_count(
        checkpoint: dict[str, Any],
        page_candidate_count: int,
    ) -> int:
        state = checkpoint.get("directory_scan")
        if not isinstance(state, dict):
            return page_candidate_count
        completed = state.get("discovered_count")
        if not isinstance(completed, int) or completed < 0:
            completed = 0
        return completed + page_candidate_count

    @staticmethod
    def _advance_directory_scan(
        checkpoint: dict[str, Any],
        discovery: _MonitorDiscovery,
        *,
        discovered_count: int,
    ) -> bool:
        page = discovery.directory_page
        if page is None:
            return False
        state = checkpoint.get("directory_scan")
        state = {} if not isinstance(state, dict) else dict(state)
        generation = state.get("generation")
        if not isinstance(generation, int) or generation < 0:
            generation = 0
        if page.has_more:
            if page.next_cursor is None:
                raise RuntimeError("directory scan page has_more without cursor")
            state["cursor"] = page.next_cursor
            state["generation"] = generation
            state["discovered_count"] = discovered_count
            checkpoint["directory_scan_continuation"] = True
        else:
            state["cursor"] = None
            state["generation"] = generation + 1
            state["discovered_count"] = 0
            checkpoint["directory_scan_continuation"] = False
        checkpoint["directory_scan"] = state
        return page.has_more

    async def _discover_monitor_candidates(
        self,
        snapshot: _DefinitionSnapshot,
        checkpoint: dict[str, Any],
    ) -> _MonitorDiscovery:
        if snapshot.source.kind == TaskSourceKind.DOWNLOADER.value:
            candidates = await self._discover_monitor_downloader_candidates(snapshot)
            return _MonitorDiscovery(candidates=candidates, directory_page=None)
        return await asyncio.to_thread(
            self._discover_monitor_directory_candidates,
            snapshot,
            checkpoint,
        )

    async def _discover_monitor_downloader_candidates(
        self,
        snapshot: _DefinitionSnapshot,
    ) -> tuple[_MonitorCandidate, ...]:
        downloader_id = snapshot.source.downloader_id
        if downloader_id is None:
            raise self._source_invalid("监控任务下载器来源已丢失绑定")
        binding = self._downloader_service.write_binding(downloader_id)
        torrents = await self._downloader_service.list_all_torrents(downloader_id)
        candidates: list[_MonitorCandidate] = []
        for torrent in torrents:
            if snapshot.policy.only_completed_downloads and torrent.progress < 1.0:
                continue
            remote_content = torrent.content_path or torrent.save_path
            try:
                container_path = binding.container_path(remote_content)
                source_root, inventory = self._inventory_for_path(container_path)
                filtered = filter_source_inventory(snapshot.filters, inventory)
                units = identify_task_units(
                    tuple(SourceTaskFile(item.relative_path, item.length) for item in filtered)
                )
            except (ApplicationError, DomainViolation, OSError, ValueError):
                continue
            source_root_relative = self._relative_to_data_root(source_root)
            digest = source_inventory_digest(inventory)
            for unit in units:
                candidates.append(
                    _MonitorCandidate(
                        source_object_key=f"{torrent.torrent_hash}:{unit.normalized_unit_key}",
                        stability_key=f"{torrent.torrent_hash}:{unit.source_relative_path}",
                        source_downloader_id=downloader_id,
                        source_hash=torrent.torrent_hash,
                        source_root=source_root_relative,
                        source_label=torrent.name,
                        unit=unit,
                        all_units=units,
                        inventory_digest=digest,
                        mtime_ns=None,
                    )
                )
        return tuple(candidates)

    def _discover_monitor_directory_candidates(
        self,
        snapshot: _DefinitionSnapshot,
        checkpoint: dict[str, Any],
    ) -> _MonitorDiscovery:
        directory_path = snapshot.source.directory_path
        if directory_path is None:
            raise self._source_invalid("监控任务目录来源已丢失路径")
        root = self._resolve_directory_root(directory_path)
        scan_state = checkpoint.get("directory_scan")
        if not isinstance(scan_state, dict):
            scan_state = {}
        raw_cursor = scan_state.get("cursor")
        cursor = raw_cursor if isinstance(raw_cursor, str) and raw_cursor else None
        try:
            page = scan_source_inventory_page(
                root,
                after=cursor,
                limit=self._directory_scan_batch_size,
            )
        except DomainViolation as exc:
            raise self._source_invalid(str(exc)) from exc
        inventory = page.candidates
        filtered = filter_source_inventory(snapshot.filters, inventory)
        units = identify_task_units(
            tuple(SourceTaskFile(item.relative_path, item.length) for item in filtered)
        )
        digest = source_inventory_digest(inventory)
        source_by_path = {item.relative_path: item for item in filtered}
        candidates: list[_MonitorCandidate] = []
        for unit in units:
            source_file = source_by_path[unit.source_relative_path]
            object_key = _directory_monitor_object_key(
                snapshot.definition.id,
                source_file,
            )
            candidates.append(
                _MonitorCandidate(
                    source_object_key=object_key,
                    stability_key=unit.source_relative_path,
                    source_downloader_id=_directory_source_identity(directory_path),
                    source_hash=object_key,
                    source_root=directory_path,
                    source_label=directory_path,
                    unit=unit,
                    all_units=units,
                    inventory_digest=digest,
                    mtime_ns=source_file.snapshot.mtime_ns,
                )
            )
        return _MonitorDiscovery(candidates=tuple(candidates), directory_page=page)

    def _assert_monitor_runnable(self, snapshot: _DefinitionSnapshot) -> None:
        if snapshot.definition.kind != TaskDefinitionKind.MONITOR.value:
            raise ApplicationError(
                code="TASK_EXECUTION_TRIGGER_INVALID",
                status=409,
                title="任务不是监控任务",
                detail="立即扫描与 Cron 只适用于监控拆包任务",
            )
        if snapshot.schedule is None:
            raise self._source_invalid("监控任务缺少 Cron 调度记录")
        if (
            snapshot.definition.status != TaskDefinitionStatus.ENABLED.value
            or not snapshot.schedule.enabled
        ):
            raise ApplicationError(
                code="TASK_DEFINITION_NOT_RUNNABLE",
                status=409,
                title="监控任务当前不可执行",
                detail="任务与调度都必须处于启用状态",
            )
        if (
            snapshot.site is None
            or not snapshot.site.enabled
            or snapshot.site.connection_status == "FAILED"
        ):
            raise ApplicationError(
                code="TASK_DEFINITION_SITE_UNAVAILABLE",
                status=409,
                title="扫描站点不可用",
                detail="执行监控扫描前需要恢复任务绑定站点的可用状态",
            )

    def _update_monitor_schedule(
        self,
        definition_id: str,
        *,
        trigger: TaskExecutionTrigger,
        now: datetime,
        checkpoint: dict[str, Any] | None,
        successful: bool,
    ) -> datetime | None:
        with self._session_factory() as session:
            schedule = session.scalar(
                select(TaskSchedule).where(TaskSchedule.task_definition_id == definition_id)
            )
            if schedule is None:
                return None
            schedule.last_scan_at = now
            if successful:
                schedule.last_successful_scan_at = now
            if checkpoint is not None:
                merged_checkpoint = dict(checkpoint)
                current_checkpoint = dict(schedule.scan_checkpoint or {})
                if current_checkpoint.get("run_once_after_pending") is True:
                    # Do not let the finishing scan erase a RUN_ONCE_AFTER request that
                    # arrived concurrently after this scan consumed the previous flag.
                    merged_checkpoint["run_once_after_pending"] = True
                schedule.scan_checkpoint = merged_checkpoint
            if trigger is TaskExecutionTrigger.CRON:
                schedule.next_run_at = next_cron_run(
                    schedule.cron_expression,
                    now,
                    timezone=schedule.timezone,
                )
            schedule.updated_at = utc_now()
            next_run_at = schedule.next_run_at
            session.commit()
            return next_run_at

    def _set_run_once_after_pending(self, definition_id: str, pending: bool) -> None:
        with self._session_factory() as session:
            schedule = session.scalar(
                select(TaskSchedule).where(TaskSchedule.task_definition_id == definition_id)
            )
            if schedule is None:
                return
            checkpoint = dict(schedule.scan_checkpoint or {})
            checkpoint["run_once_after_pending"] = pending
            schedule.scan_checkpoint = checkpoint
            schedule.updated_at = utc_now()
            session.commit()

    def _schedule_next_run(self, definition_id: str) -> datetime | None:
        with self._session_factory() as session:
            schedule = session.scalar(
                select(TaskSchedule).where(TaskSchedule.task_definition_id == definition_id)
            )
            return schedule.next_run_at if schedule is not None else None

    async def _materialize_downloader(
        self,
        snapshot: _DefinitionSnapshot,
        execution_id: str,
        trace_id: str,
    ) -> tuple[TaskExecutionItemView, ...]:
        downloader_id = snapshot.source.downloader_id
        if downloader_id is None:
            raise self._source_invalid("下载器来源已丢失绑定")
        selected_hashes = self._selected_hashes(snapshot.source.config)
        binding = self._downloader_service.write_binding(downloader_id)
        live_items = await self._downloader_service.list_all_torrents(downloader_id)
        live_by_hash = {item.torrent_hash: item for item in live_items}
        results: list[TaskExecutionItemView] = []
        for torrent_hash in selected_hashes:
            torrent = live_by_hash.get(torrent_hash)
            if torrent is None:
                results.append(
                    self._record_terminal_item(
                        execution_id=execution_id,
                        trace_id=trace_id,
                        source_object_key=torrent_hash,
                        name=torrent_hash,
                        source=f"downloader:{downloader_id}",
                        size_bytes=None,
                        result="FAILED",
                        error_code="SOURCE_TORRENT_NOT_FOUND",
                        error_summary_zh="已选择种子当前已不在下载器中",
                    )
                )
                continue
            if snapshot.policy.only_completed_downloads and torrent.progress < 1.0:
                results.append(
                    self._record_terminal_item(
                        execution_id=execution_id,
                        trace_id=trace_id,
                        source_object_key=torrent_hash,
                        name=torrent.name,
                        source=torrent.save_path,
                        size_bytes=torrent.size_bytes,
                        result="SKIPPED",
                        error_code="SOURCE_TORRENT_INCOMPLETE",
                        error_summary_zh="种子尚未完成，按任务策略跳过",
                    )
                )
                continue
            remote_content = torrent.content_path or torrent.save_path
            try:
                container_path = binding.container_path(remote_content)
                source_root, inventory = self._inventory_for_path(container_path)
                filtered = filter_source_inventory(snapshot.filters, inventory)
                units = identify_task_units(
                    tuple(SourceTaskFile(item.relative_path, item.length) for item in filtered)
                )
            except (ApplicationError, DomainViolation, OSError, ValueError) as exc:
                results.append(
                    self._record_terminal_item(
                        execution_id=execution_id,
                        trace_id=trace_id,
                        source_object_key=torrent_hash,
                        name=torrent.name,
                        source=remote_content,
                        size_bytes=torrent.size_bytes,
                        result="FAILED",
                        error_code="SOURCE_DISCOVERY_FAILED",
                        error_summary_zh="种子内容无法安全扫描",
                        technical_detail=str(exc),
                    )
                )
                continue
            if not units:
                results.append(
                    self._record_terminal_item(
                        execution_id=execution_id,
                        trace_id=trace_id,
                        source_object_key=torrent_hash,
                        name=torrent.name,
                        source=remote_content,
                        size_bytes=torrent.size_bytes,
                        result="SKIPPED",
                        error_code="SOURCE_NO_MATCHING_VIDEO",
                        error_summary_zh="种子中没有符合当前过滤规则的视频处理单元",
                    )
                )
                continue
            source_root_relative = self._relative_to_data_root(source_root)
            for unit in units:
                results.append(
                    self._materialize_unit(
                        snapshot=snapshot,
                        execution_id=execution_id,
                        trace_id=trace_id,
                        source_downloader_id=downloader_id,
                        source_hash=torrent.torrent_hash,
                        source_root=source_root_relative,
                        source_label=torrent.name,
                        source_object_key=f"{torrent.torrent_hash}:{unit.normalized_unit_key}",
                        unit=unit,
                        all_units=units,
                        inventory_digest=source_inventory_digest(inventory),
                    )
                )
        return tuple(results)

    def _materialize_directory(
        self,
        snapshot: _DefinitionSnapshot,
        execution_id: str,
        trace_id: str,
    ) -> tuple[TaskExecutionItemView, ...]:
        directory_path = snapshot.source.directory_path
        if directory_path is None:
            raise self._source_invalid("目录来源已丢失路径")
        root = self._resolve_directory_root(directory_path)
        try:
            inventory = scan_source_inventory(root)
        except DomainViolation as exc:
            raise self._source_invalid(str(exc)) from exc
        filtered = filter_source_inventory(snapshot.filters, inventory)
        terminal_items: list[TaskExecutionItemView] = []
        selected_files = snapshot.source.config.get("selected_files")
        if snapshot.definition.kind == TaskDefinitionKind.MANUAL.value and isinstance(
            selected_files, list
        ):
            current_by_path = {item.relative_path: item for item in filtered}
            selected_inventory: list[SourceFileCandidate] = []
            for raw in selected_files:
                if not isinstance(raw, dict):
                    continue
                relative_path = raw.get("relative_path")
                if not isinstance(relative_path, str):
                    continue
                current = current_by_path.get(relative_path)
                expected = (
                    raw.get("size_bytes"),
                    raw.get("device"),
                    raw.get("inode"),
                    raw.get("mtime_ns"),
                )
                observed = (
                    current.length if current is not None else None,
                    current.snapshot.device if current is not None else None,
                    current.snapshot.inode if current is not None else None,
                    current.snapshot.mtime_ns if current is not None else None,
                )
                if current is None or expected != observed:
                    terminal_items.append(
                        self._record_terminal_item(
                            execution_id=execution_id,
                            trace_id=trace_id,
                            source_object_key=relative_path,
                            name=relative_path,
                            source=directory_path,
                            size_bytes=(
                                int(raw["size_bytes"])
                                if isinstance(raw.get("size_bytes"), int)
                                else None
                            ),
                            result="FAILED",
                            error_code="SOURCE_SNAPSHOT_CHANGED",
                            error_summary_zh="文件已与保存任务时的扫描快照不一致",
                        )
                    )
                    continue
                selected_inventory.append(current)
            filtered = tuple(selected_inventory)
        units = identify_task_units(
            tuple(SourceTaskFile(item.relative_path, item.length) for item in filtered)
        )
        if not units and not terminal_items:
            raise ApplicationError(
                code="TASK_SOURCE_EMPTY",
                status=409,
                title="没有可执行对象",
                detail="来源目录中没有符合当前过滤规则的视频处理单元",
            )
        digest = source_inventory_digest(inventory)
        source_hash = sha256(
            f"directory\0{snapshot.definition.id}\0{directory_path}\0{digest}".encode()
        ).hexdigest()
        materialized = tuple(
            self._materialize_unit(
                snapshot=snapshot,
                execution_id=execution_id,
                trace_id=trace_id,
                source_downloader_id=_directory_source_identity(directory_path),
                source_hash=source_hash,
                source_root=directory_path,
                source_label=directory_path,
                source_object_key=unit.normalized_unit_key,
                unit=unit,
                all_units=units,
                inventory_digest=digest,
            )
            for unit in units
        )
        return (*terminal_items, *materialized)

    def _materialize_unit(
        self,
        *,
        snapshot: _DefinitionSnapshot,
        execution_id: str,
        trace_id: str,
        source_downloader_id: str,
        source_hash: str,
        source_root: str,
        source_label: str,
        source_object_key: str,
        unit: TaskUnit,
        all_units: tuple[TaskUnit, ...],
        inventory_digest: str,
    ) -> TaskExecutionItemView:
        with self._session_factory() as session:
            task, _ = TaskRepository(session).create_or_get(
                TaskCreate(
                    task_type="PACKAGE_UNPACK",
                    source_downloader_id=source_downloader_id,
                    source_hash=source_hash,
                    normalized_unit_key=unit.normalized_unit_key,
                    trace_id=trace_id,
                    checkpoint={
                        "schema_version": "packbreaker-task-definition-bridge-v1",
                        "task_definition_id": snapshot.definition.id,
                        "task_execution_id": execution_id,
                        "site_id": snapshot.definition.site_id,
                        "source_root": source_root,
                        "source_inventory_digest": inventory_digest,
                        "source_kind": snapshot.source.kind,
                        "source_identity": source_downloader_id,
                        "source_directory": (
                            source_root
                            if snapshot.source.kind == TaskSourceKind.DIRECTORY.value
                            else None
                        ),
                    },
                )
            )
            TaskUnitRepository(session).record_batch(
                task_id=task.id,
                source_root=source_root,
                source_inventory_digest=inventory_digest,
                units=all_units,
            )
            item = TaskExecutionItem(
                id=new_uuid(),
                execution_id=execution_id,
                unpack_task_id=task.id,
                source_object_key=source_object_key,
                name=unit.source_relative_path,
                source=source_label,
                size_bytes=unit.length,
                phase=TaskExecutionPhase.WAITING.value,
                progress=None,
                result=None,
                error_code=None,
                error_summary_zh=None,
                technical_detail=None,
                retryable=False,
                retry_count=0,
                created_at=utc_now(),
                updated_at=utc_now(),
            )
            session.add(item)
            self._event(
                session,
                execution_id=execution_id,
                trace_id=trace_id,
                event_code="TASK_UNPACK_RUN_MATERIALIZED",
                message="Safe unpack run created and waiting for preflight",
                context={
                    "unpack_task_id": task.id,
                    "source_object_key": source_object_key,
                    "source_root": source_root,
                },
            )
            session.commit()
            return self._item_view(item)

    def _record_terminal_item(
        self,
        *,
        execution_id: str,
        trace_id: str,
        source_object_key: str,
        name: str,
        source: str,
        size_bytes: int | None,
        result: str,
        error_code: str,
        error_summary_zh: str,
        technical_detail: str | None = None,
    ) -> TaskExecutionItemView:
        phase = (
            TaskExecutionPhase.FAILED.value
            if result == "FAILED"
            else TaskExecutionPhase.SKIPPED.value
        )
        now = utc_now()
        with self._session_factory() as session:
            item = TaskExecutionItem(
                id=new_uuid(),
                execution_id=execution_id,
                unpack_task_id=None,
                source_object_key=source_object_key,
                name=name,
                source=source,
                size_bytes=size_bytes,
                phase=phase,
                progress=None,
                result=result,
                error_code=error_code,
                error_summary_zh=error_summary_zh,
                technical_detail=technical_detail,
                retryable=False,
                retry_count=0,
                created_at=now,
                updated_at=now,
            )
            session.add(item)
            self._event(
                session,
                execution_id=execution_id,
                trace_id=trace_id,
                event_code="TASK_SOURCE_OBJECT_REJECTED",
                message="Source object could not be materialized",
                context={"source_object_key": source_object_key, "error_code": error_code},
            )
            session.commit()
            return self._item_view(item)

    def _resolve_directory_root(self, directory_path: str) -> Path:
        current = self._data_root
        parts = () if directory_path == "." else tuple(directory_path.split("/"))
        try:
            base_stat = self._data_root.stat(follow_symlinks=False)
            base = self._data_root.resolve(strict=True)
        except OSError as exc:
            raise self._source_invalid("授权数据根目录不可用") from exc
        if stat.S_ISLNK(base_stat.st_mode) or not stat.S_ISDIR(base_stat.st_mode):
            raise self._source_invalid("授权数据根目录必须是真实目录且不能是符号链接")
        for part in parts:
            if not part or part in {".", ".."}:
                raise self._source_invalid("来源目录包含不安全路径段")
            current = current / part
            try:
                item_stat = current.stat(follow_symlinks=False)
            except OSError as exc:
                raise self._source_invalid("来源目录不存在或不可读取") from exc
            if stat.S_ISLNK(item_stat.st_mode) or not stat.S_ISDIR(item_stat.st_mode):
                raise self._source_invalid("来源目录不能经过符号链接或非目录路径")
        try:
            resolved = current.resolve(strict=True)
        except OSError as exc:
            raise self._source_invalid("来源目录不存在或不可读取") from exc
        if not resolved.is_relative_to(base):
            raise self._source_invalid("来源目录越过授权数据根目录")
        return resolved

    def _inventory_for_path(self, path: Path) -> tuple[Path, tuple[SourceFileCandidate, ...]]:
        if path.is_dir():
            return path, scan_source_inventory(path)
        if path.is_file():
            parent = path.parent
            inventory = tuple(
                item for item in scan_source_inventory(parent) if Path(item.source_path) == path
            )
            if not inventory:
                raise self._source_invalid("下载器内容文件当前不可读取")
            return parent, inventory
        raise self._source_invalid("下载器内容路径当前不存在")

    def _load_definition_snapshot(self, definition_id: str) -> _DefinitionSnapshot:
        with self._session_factory() as session:
            definition = session.get(TaskDefinition, definition_id)
            if definition is None:
                raise ApplicationError(
                    code="TASK_DEFINITION_NOT_FOUND",
                    status=404,
                    title="任务定义不存在",
                    detail="未找到指定的 v0.1.5 任务定义",
                )
            source = session.scalar(
                select(TaskSource).where(TaskSource.task_definition_id == definition_id)
            )
            filters = session.scalar(
                select(TaskFilter).where(TaskFilter.task_definition_id == definition_id)
            )
            output = session.scalar(
                select(TaskOutputPolicy).where(TaskOutputPolicy.task_definition_id == definition_id)
            )
            if source is None or filters is None or output is None:
                raise ApplicationError(
                    code="TASK_DEFINITION_CORRUPT",
                    status=500,
                    title="任务定义数据不完整",
                    detail="任务定义缺少来源、过滤或输出策略记录",
                )
            site = session.get(Site, definition.site_id) if definition.site_id is not None else None
            policy_record = session.scalar(
                select(TaskExecutionPolicy).where(
                    TaskExecutionPolicy.task_definition_id == definition_id
                )
            )
            schedule = session.scalar(
                select(TaskSchedule).where(TaskSchedule.task_definition_id == definition_id)
            )
            if policy_record is None:
                raise ApplicationError(
                    code="TASK_DEFINITION_CORRUPT",
                    status=500,
                    title="任务定义数据不完整",
                    detail="任务定义缺少执行策略记录",
                )
            session.expunge(definition)
            session.expunge(source)
            session.expunge(filters)
            session.expunge(output)
            session.expunge(policy_record)
            if schedule is not None:
                session.expunge(schedule)
            if site is not None:
                session.expunge(site)
            return _DefinitionSnapshot(
                definition=definition,
                source=source,
                filters=filters,
                output=output,
                site=site,
                policy=policy_record,
                schedule=schedule,
            )

    @staticmethod
    def _selected_hashes(config: dict[str, Any]) -> tuple[str, ...]:
        raw = config.get("selected_torrent_hashes")
        if not isinstance(raw, list) or not raw:
            raise TaskDefinitionExecutionService._source_invalid(
                "手动下载器任务没有保存已选择种子快照"
            )
        hashes: list[str] = []
        for value in raw:
            if not isinstance(value, str):
                raise TaskDefinitionExecutionService._source_invalid("种子 hash 快照格式无效")
            normalized = value.strip().lower()
            if len(normalized) not in {40, 64} or any(
                char not in "0123456789abcdef" for char in normalized
            ):
                raise TaskDefinitionExecutionService._source_invalid("种子 hash 快照格式无效")
            hashes.append(normalized)
        return tuple(dict.fromkeys(hashes))

    def _relative_to_data_root(self, path: Path) -> str:
        resolved = path.resolve(strict=False)
        try:
            relative = resolved.relative_to(self._data_root)
        except ValueError as exc:
            raise self._source_invalid("来源路径越过授权数据根目录") from exc
        return relative.as_posix() or "."

    @staticmethod
    def _config_snapshot(snapshot: _DefinitionSnapshot) -> dict[str, Any]:
        return {
            "task_definition_id": snapshot.definition.id,
            "task_definition_version": snapshot.definition.version,
            "site_id": snapshot.definition.site_id,
            "source": {
                "kind": snapshot.source.kind,
                "downloader_id": snapshot.source.downloader_id,
                "directory_path": snapshot.source.directory_path,
                "config": dict(snapshot.source.config),
            },
            "filters": {
                "file_types": list(snapshot.filters.file_types),
                "video_extensions": list(snapshot.filters.video_extensions),
                "archive_extensions": list(snapshot.filters.archive_extensions),
                "min_size_bytes": snapshot.filters.min_size_bytes,
                "max_size_bytes": snapshot.filters.max_size_bytes,
                "include_name": snapshot.filters.include_name,
                "exclude_names": list(snapshot.filters.exclude_names),
                "ignore_temp_files": snapshot.filters.ignore_temp_files,
                "temp_patterns": list(snapshot.filters.temp_patterns),
                "include_subdirectories": snapshot.filters.include_subdirectories,
                "max_scan_depth": snapshot.filters.max_scan_depth,
            },
            "output": {
                "output_directory": snapshot.output.output_directory,
                "storage_mode": snapshot.output.storage_mode,
                "preserve_structure": snapshot.output.preserve_structure,
                "conflict_policy": snapshot.output.conflict_policy,
            },
        }

    @staticmethod
    def _event(
        session: Session,
        *,
        execution_id: str,
        trace_id: str,
        event_code: str,
        message: str,
        context: dict[str, Any],
    ) -> None:
        session.add(
            TaskExecutionEvent(
                id=new_uuid(),
                execution_id=execution_id,
                event_code=event_code,
                message=message,
                trace_id=trace_id,
                context=context,
                created_at=utc_now(),
            )
        )

    def _mark_execution_failed(self, execution_id: str, trace_id: str, exc: Exception) -> None:
        with self._session_factory() as session:
            record = session.get(TaskExecution, execution_id)
            if record is None:
                return
            record.status = TaskExecutionStatus.FAILED.value
            record.phase = TaskExecutionPhase.FAILED.value
            record.finished_at = utc_now()
            record.failed_count = max(1, record.failed_count)
            self._event(
                session,
                execution_id=execution_id,
                trace_id=trace_id,
                event_code="TASK_EXECUTION_FAILED",
                message="Task source materialization failed",
                context={"error_type": type(exc).__name__, "detail": str(exc)},
            )
            session.commit()

    @staticmethod
    def _item_view(
        item: TaskExecutionItem,
        lifecycle: TaskLifecycleProjection | None = None,
        risk_summary: TaskRiskSummaryView | None = None,
        approval: TaskApprovalView | None = None,
        closure: TaskExecutionClosureView | None = None,
    ) -> TaskExecutionItemView:
        resolved = lifecycle or TaskLifecycleProjection(
            stage=TaskLifecycleStage.DISCOVER,
            risk_level=TaskLifecycleRiskLevel.UNKNOWN,
            authorization=TaskLifecycleAuthorization.NOT_READY,
            execution_plan_id=None,
            plan_ready=None,
            side_effects_started=False,
            blocked_reasons=(),
        )
        resolved_closure = closure or TaskExecutionClosureView(
            status="PENDING",
            filesystem_status="PENDING",
            downloader_status="PENDING",
            operation_attention_count=0,
            reconcile_required_count=0,
            rollback_blocked_count=0,
            retention_candidate_count=0,
            manual_attention_required=False,
            issue_codes=(),
        )
        return TaskExecutionItemView(
            id=item.id,
            unpack_task_id=item.unpack_task_id,
            source_object_key=item.source_object_key,
            name=item.name,
            source=item.source,
            size_bytes=item.size_bytes,
            phase=item.phase,
            progress=item.progress,
            result=item.result,
            error_code=item.error_code,
            error_summary_zh=item.error_summary_zh,
            technical_detail=item.technical_detail,
            retryable=item.retryable,
            retry_count=item.retry_count,
            lifecycle_stage=resolved.stage.value,
            risk_level=resolved.risk_level.value,
            authorization_status=resolved.authorization.value,
            execution_plan_id=resolved.execution_plan_id,
            execution_plan_ready=resolved.plan_ready,
            side_effects_started=resolved.side_effects_started,
            lifecycle_blocked_reasons=resolved.blocked_reasons,
            risk_summary=risk_summary,
            approval=approval,
            closure=resolved_closure,
        )

    @staticmethod
    def _risk_summary_view(record: TaskRiskSummaryRecord) -> TaskRiskSummaryView:
        return TaskRiskSummaryView(
            id=record.id,
            execution_plan_id=record.execution_plan_id,
            plan_digest=record.plan_digest,
            risk_level=record.risk_level,
            reason_codes=tuple(record.reason_codes),
            action_kinds=tuple(record.action_kinds),
            hardlink_count=record.hardlink_count,
            client_fetch_count=record.client_fetch_count,
            create_directory_count=record.create_directory_count,
            estimated_download_bytes_upper_bound=record.estimated_download_bytes_upper_bound,
            risk_digest=record.risk_digest,
            created_at=record.created_at,
        )

    @staticmethod
    def _approval_view(record: TaskApprovalRecord) -> TaskApprovalView:
        return TaskApprovalView(
            id=record.id,
            execution_plan_id=record.execution_plan_id,
            plan_digest=record.plan_digest,
            state=record.state,
            decision_source=record.decision_source,
            actor_kind=record.actor_kind,
            actor_id=record.actor_id,
            decision_note=record.decision_note,
            decided_at=record.decided_at,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )

    @staticmethod
    def _event_view(event: TaskExecutionEvent) -> TaskExecutionEventView:
        return TaskExecutionEventView(
            id=event.id,
            event_code=event.event_code,
            message=event.message,
            trace_id=event.trace_id,
            context=dict(event.context or {}),
            created_at=event.created_at,
        )

    @staticmethod
    def _list_item_view(record: TaskExecution) -> TaskExecutionListItemView:
        return TaskExecutionListItemView(
            id=record.id,
            task_definition_id=record.task_definition_id,
            task_name=record.task_name,
            trigger=record.trigger,
            status=record.status,
            phase=record.phase,
            source_execution_id=record.source_execution_id,
            discovered_count=record.discovered_count,
            success_count=record.success_count,
            failed_count=record.failed_count,
            skipped_count=record.skipped_count,
            trace_id=record.trace_id,
            started_at=record.started_at,
            finished_at=record.finished_at,
            created_at=record.created_at,
        )

    @staticmethod
    def _source_invalid(detail: str) -> ApplicationError:
        return ApplicationError(
            code="TASK_SOURCE_INVALID",
            status=409,
            title="任务来源不可执行",
            detail=detail,
        )


@dataclass(frozen=True, slots=True)
class _DefinitionSnapshot:
    definition: TaskDefinition
    source: TaskSource
    filters: TaskFilter
    output: TaskOutputPolicy
    site: Site | None
    policy: TaskExecutionPolicy
    schedule: TaskSchedule | None


@dataclass(frozen=True, slots=True)
class _MonitorCandidate:
    source_object_key: str
    stability_key: str
    source_downloader_id: str
    source_hash: str
    source_root: str
    source_label: str
    unit: TaskUnit
    all_units: tuple[TaskUnit, ...]
    inventory_digest: str
    mtime_ns: int | None


@dataclass(frozen=True, slots=True)
class _MonitorDiscovery:
    candidates: tuple[_MonitorCandidate, ...]
    directory_page: SourceInventoryPage | None


def _monitor_lock(definition_id: str) -> asyncio.Lock:
    lock = _MONITOR_LOCKS.get(definition_id)
    if lock is None:
        lock = asyncio.Lock()
        _MONITOR_LOCKS[definition_id] = lock
    return lock


def _directory_source_identity(directory_path: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"packbreaker:directory-source:{directory_path}"))


def _directory_monitor_object_key(
    definition_id: str,
    source_file: SourceFileCandidate,
) -> str:
    snapshot = source_file.snapshot
    payload = "\0".join(
        (
            "packbreaker-monitor-directory-object-v1",
            definition_id,
            source_file.relative_path,
            str(snapshot.device),
            str(snapshot.inode),
            str(snapshot.size),
            str(snapshot.mtime_ns),
        )
    ).encode()
    return sha256(payload).hexdigest()


def _require_lifecycle_idempotency_key(value: str | None) -> str:
    if value is None:
        raise ApplicationError(
            code="IDEMPOTENCY_KEY_REQUIRED",
            status=400,
            title="缺少幂等键",
            detail="推进统一任务生命周期必须携带 Idempotency-Key",
        )
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > 200
        or normalized != value
        or any(ord(char) < 0x21 or ord(char) > 0x7E for char in normalized)
    ):
        raise ApplicationError(
            code="IDEMPOTENCY_KEY_INVALID",
            status=422,
            title="幂等键无效",
            detail="Idempotency-Key 必须是 1..200 个不含空白/控制字符的可见 ASCII 字符",
        )
    return normalized


def _lifecycle_item_idempotency_key(
    base_key: str,
    *,
    execution_id: str,
    item_id: str,
    plan_id: str,
) -> str:
    payload = "\0".join(
        (
            "packbreaker-task-lifecycle-v1",
            base_key,
            execution_id,
            item_id,
            plan_id,
        )
    ).encode()
    return f"lifecycle-{sha256(payload).hexdigest()}"
