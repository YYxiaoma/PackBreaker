from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.errors import ApplicationError
from backend.app.domain.errors import DomainViolation
from backend.app.domain.task_state import (
    ACTIVE_ANALYSIS_STATUSES,
    PRE_SIDE_EFFECT_CANCELLATION_SCHEMA_VERSION,
    TaskCancellationMode,
    TaskStatus,
)
from backend.app.domain.verification import DownloaderKind
from backend.app.infrastructure.persistence.repositories import (
    OperationJournalRepository,
    TaskRepository,
)
from backend.app.infrastructure.persistence.task_analysis_repositories import (
    TaskExecutionPlanRepository,
)

_RECOVERABLE_STATUSES = (
    TaskStatus.LINKING,
    TaskStatus.ADDING,
    TaskStatus.CLIENT_VERIFYING,
    TaskStatus.SEEDING,
    TaskStatus.ROLLING_BACK,
)
_STARTUP_RECOVERABLE_STATUSES = (TaskStatus.CANCELLING, *_RECOVERABLE_STATUSES)
_DEFAULT_RECOVERY_LIMIT = 100
_DEFAULT_MAX_STEPS_PER_TASK = 4


class RecoveryOutcome(StrEnum):
    COMPLETED = "COMPLETED"
    WAITING = "WAITING"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True, slots=True)
class TaskRecoveryItem:
    task_id: str
    execution_plan_id: str | None
    initial_status: TaskStatus
    final_status: TaskStatus
    outcome: RecoveryOutcome
    steps: tuple[TaskStatus, ...]
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class TaskRecoveryReport:
    items: tuple[TaskRecoveryItem, ...]
    scanned_count: int
    completed_count: int
    waiting_count: int
    blocked_count: int
    truncated: bool


class LinkingRecoveryPort(Protocol):
    def execute(self, unit_id: str, *, execution_plan_id: str) -> object: ...


class AsyncTaskRecoveryPort(Protocol):
    async def execute(self, unit_id: str, *, execution_plan_id: str) -> object: ...


class CancellationRecoveryPort(Protocol):
    async def resume(self, task_id: str) -> object: ...


@dataclass(frozen=True, slots=True)
class _RecoveryTarget:
    task_id: str
    unit_id: str
    execution_plan_id: str
    status: TaskStatus


class TaskRecoveryCoordinator:
    """启动时对已授权活动任务做有界幂等恢复；具体副作用仍由各 stage coordinator 承担。"""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        linking: LinkingRecoveryPort,
        adding: AsyncTaskRecoveryPort,
        client_verification: AsyncTaskRecoveryPort,
        seeding: AsyncTaskRecoveryPort,
        cancellation: CancellationRecoveryPort | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._linking = linking
        self._adding = adding
        self._client_verification = client_verification
        self._seeding = seeding
        self._cancellation = cancellation

    async def reconcile_once(
        self,
        *,
        limit: int = _DEFAULT_RECOVERY_LIMIT,
        max_steps_per_task: int = _DEFAULT_MAX_STEPS_PER_TASK,
        recover_abandoned_analysis: bool = False,
    ) -> TaskRecoveryReport:
        if limit <= 0:
            raise ValueError("limit 必须大于 0")
        if max_steps_per_task <= 0:
            raise ValueError("max_steps_per_task 必须大于 0")

        recoverable_statuses = _recovery_statuses(recover_abandoned_analysis)
        task_ids, truncated = self._load_recoverable_task_ids(limit, recoverable_statuses)
        items: list[TaskRecoveryItem] = []
        for task_id in task_ids:
            items.append(
                await self.reconcile_task(
                    task_id,
                    max_steps=max_steps_per_task,
                    recover_abandoned_analysis=recover_abandoned_analysis,
                )
            )
        return _report(tuple(items), truncated=truncated)

    async def reconcile_task(
        self,
        task_id: str,
        *,
        max_steps: int = _DEFAULT_MAX_STEPS_PER_TASK,
        recover_abandoned_analysis: bool = False,
    ) -> TaskRecoveryItem:
        if max_steps <= 0:
            raise ValueError("max_steps 必须大于 0")

        recoverable_statuses = _recovery_statuses(recover_abandoned_analysis)
        initial_status = self._load_task_status(task_id)
        if initial_status not in recoverable_statuses:
            raise ApplicationError(
                code="RECOVERY_TASK_STATE_INVALID",
                status=409,
                title="任务状态不需要启动恢复",
                detail=(
                    "仅 LINKING/ADDING/CLIENT_VERIFYING/SEEDING/ROLLING_BACK 进入普通恢复；"
                    "启动期可额外恢复遗留的协作式 CANCELLING"
                ),
            )

        steps: list[TaskStatus] = []
        execution_plan_id: str | None = None
        current_status = initial_status
        try:
            for _ in range(max_steps):
                current_status = self._load_task_status(task_id)
                if current_status not in recoverable_statuses:
                    break
                if current_status is TaskStatus.CANCELLING:
                    if not recover_abandoned_analysis:
                        raise AssertionError("普通周期恢复不应扫描 CANCELLING")
                    steps.append(current_status)
                    self._recover_abandoned_analysis_cancellation(task_id)
                    next_status = self._load_task_status(task_id)
                    current_status = next_status
                    if next_status is TaskStatus.CANCELLING:
                        return TaskRecoveryItem(
                            task_id=task_id,
                            execution_plan_id=None,
                            initial_status=initial_status,
                            final_status=next_status,
                            outcome=RecoveryOutcome.WAITING,
                            steps=tuple(steps),
                        )
                    if next_status not in recoverable_statuses:
                        return TaskRecoveryItem(
                            task_id=task_id,
                            execution_plan_id=None,
                            initial_status=initial_status,
                            final_status=next_status,
                            outcome=RecoveryOutcome.COMPLETED,
                            steps=tuple(steps),
                        )
                    continue

                target = self._load_target(task_id)
                execution_plan_id = target.execution_plan_id
                current_status = target.status
                if current_status not in recoverable_statuses:
                    break

                steps.append(current_status)
                await self._execute_stage(target)
                next_status = self._load_task_status(task_id)
                current_status = next_status

                if next_status == target.status:
                    return TaskRecoveryItem(
                        task_id=task_id,
                        execution_plan_id=execution_plan_id,
                        initial_status=initial_status,
                        final_status=next_status,
                        outcome=RecoveryOutcome.WAITING,
                        steps=tuple(steps),
                    )
                if next_status not in recoverable_statuses:
                    return TaskRecoveryItem(
                        task_id=task_id,
                        execution_plan_id=execution_plan_id,
                        initial_status=initial_status,
                        final_status=next_status,
                        outcome=RecoveryOutcome.COMPLETED,
                        steps=tuple(steps),
                    )

            final_status = self._load_task_status(task_id)
            return TaskRecoveryItem(
                task_id=task_id,
                execution_plan_id=execution_plan_id,
                initial_status=initial_status,
                final_status=final_status,
                outcome=(
                    RecoveryOutcome.WAITING
                    if final_status in recoverable_statuses
                    else RecoveryOutcome.COMPLETED
                ),
                steps=tuple(steps),
            )
        except ApplicationError as exc:
            return TaskRecoveryItem(
                task_id=task_id,
                execution_plan_id=execution_plan_id,
                initial_status=initial_status,
                final_status=self._load_task_status(task_id),
                outcome=RecoveryOutcome.BLOCKED,
                steps=tuple(steps),
                error_code=exc.code,
            )
        except DomainViolation as exc:
            return TaskRecoveryItem(
                task_id=task_id,
                execution_plan_id=execution_plan_id,
                initial_status=initial_status,
                final_status=self._load_task_status(task_id),
                outcome=RecoveryOutcome.BLOCKED,
                steps=tuple(steps),
                error_code=exc.code.value,
            )

    async def _execute_stage(self, target: _RecoveryTarget) -> None:
        if target.status is TaskStatus.LINKING:
            self._linking.execute(
                target.unit_id,
                execution_plan_id=target.execution_plan_id,
            )
            return
        if target.status is TaskStatus.ADDING:
            await self._adding.execute(
                target.unit_id,
                execution_plan_id=target.execution_plan_id,
            )
            return
        if target.status is TaskStatus.CLIENT_VERIFYING:
            await self._client_verification.execute(
                target.unit_id,
                execution_plan_id=target.execution_plan_id,
            )
            return
        if target.status is TaskStatus.SEEDING:
            if self._transmission_seeding_is_parked(target.task_id):
                return
            await self._seeding.execute(
                target.unit_id,
                execution_plan_id=target.execution_plan_id,
            )
            return
        if target.status is TaskStatus.ROLLING_BACK:
            if self._cancellation is None:
                raise ApplicationError(
                    code="RECOVERY_CANCELLATION_UNAVAILABLE",
                    status=409,
                    title="启动恢复缺少取消协调器",
                    detail="ROLLING_BACK 任务不能在未注册取消协调器时自动恢复",
                )
            await self._cancellation.resume(target.task_id)
            return
        raise AssertionError(f"未处理的恢复状态: {target.status.value}")

    def _load_recoverable_task_ids(
        self,
        limit: int,
        statuses: tuple[TaskStatus, ...],
    ) -> tuple[tuple[str, ...], bool]:
        with self._session_factory() as session:
            tasks = TaskRepository(session).list_for_recovery(
                statuses=statuses,
                limit=limit + 1,
            )
            truncated = len(tasks) > limit
            return tuple(task.id for task in tasks[:limit]), truncated

    def _load_task_status(self, task_id: str) -> TaskStatus:
        with self._session_factory() as session:
            task = TaskRepository(session).get(task_id)
            if task is None:
                raise ApplicationError(
                    code="RECOVERY_TASK_NOT_FOUND",
                    status=404,
                    title="启动恢复任务不存在",
                    detail="恢复扫描期间任务已不存在",
                )
            try:
                return TaskStatus(task.status)
            except ValueError as exc:
                raise ApplicationError(
                    code="RECOVERY_TASK_STATE_INVALID",
                    status=409,
                    title="启动恢复任务状态无效",
                    detail="任务保存了未知状态",
                ) from exc

    def _transmission_seeding_is_parked(self, task_id: str) -> bool:
        with self._session_factory() as session:
            task = TaskRepository(session).get(task_id)
            if task is None:
                raise ApplicationError(
                    code="RECOVERY_TASK_NOT_FOUND",
                    status=404,
                    title="启动恢复任务不存在",
                    detail="检查 Transmission SEEDING 停靠状态时任务已不存在",
                )
            value = task.checkpoint.get("downloader_kind")
            if value is None or value == DownloaderKind.QBITTORRENT.value:
                return False
            if value == DownloaderKind.TRANSMISSION.value:
                return True
            raise ApplicationError(
                code="RECOVERY_CHECKPOINT_INVALID",
                status=409,
                title="启动恢复检查点无效",
                detail="SEEDING checkpoint 包含未知 downloader_kind，禁止猜测下载器类型",
            )

    def _recover_abandoned_analysis_cancellation(self, task_id: str) -> None:
        with self._session_factory() as session:
            repository = TaskRepository(session)
            task = repository.get(task_id)
            if task is None:
                raise ApplicationError(
                    code="RECOVERY_TASK_NOT_FOUND",
                    status=404,
                    title="启动恢复任务不存在",
                    detail="恢复协作式取消时任务已不存在",
                )
            if task.status != TaskStatus.CANCELLING.value:
                raise _recovery_cancelling_evidence_invalid("任务已不处于 CANCELLING")

            checkpoint = deepcopy(task.checkpoint)
            requested_from = checkpoint.get("requested_from_status")
            analysis_version = checkpoint.get("analysis_version")
            if (
                checkpoint.get("schema_version") != PRE_SIDE_EFFECT_CANCELLATION_SCHEMA_VERSION
                or checkpoint.get("stage") != TaskStatus.CANCELLING.value
                or checkpoint.get("mode") != TaskCancellationMode.COOPERATIVE_ANALYSIS.value
                or checkpoint.get("remove_downloader_task") is not False
                or checkpoint.get("rollback_created_resources") is not False
                or requested_from not in {status.value for status in ACTIVE_ANALYSIS_STATUSES}
                or not isinstance(analysis_version, int)
                or isinstance(analysis_version, bool)
                or analysis_version < 1
                or analysis_version != task.version - 1
            ):
                raise _recovery_cancelling_evidence_invalid(
                    "协作式取消 checkpoint 未与遗留分析 stage/version 和零资源选项精确绑定"
                )

            latest_event = repository.latest_event(task.id)
            if (
                latest_event is None
                or latest_event.event_type != "CANCELLATION_STARTED"
                or latest_event.from_status != requested_from
                or latest_event.to_status != TaskStatus.CANCELLING.value
            ):
                raise _recovery_cancelling_evidence_invalid(
                    "最近 TaskEvent 不能证明当前 CANCELLING 来自同一轮协作式分析取消"
                )
            if OperationJournalRepository(session).list_for_task(task.id):
                raise ApplicationError(
                    code="CANCELLATION_EVIDENCE_CONFLICT",
                    status=409,
                    title="分析取消证据与副作用日志冲突",
                    detail="启动恢复发现 operation journal；拒绝把遗留分析取消自动收敛到 CANCELLED",
                )

            cancelled_checkpoint = deepcopy(checkpoint)
            cancelled_checkpoint["stage"] = TaskStatus.CANCELLED.value
            try:
                repository.transition(
                    task_id=task.id,
                    expected_version=task.version,
                    to_status=TaskStatus.CANCELLED,
                    event_type="CANCELLATION_COMPLETED",
                    reason="进程重启后确认只读分析已不存在；零副作用取消安全收敛完成",
                    checkpoint=cancelled_checkpoint,
                )
            except DomainViolation as exc:
                raise ApplicationError(
                    code="CANCELLATION_TASK_CHANGED",
                    status=409,
                    title="分析取消启动恢复期间任务发生变化",
                    detail="无法确认遗留协作式取消仍绑定当前任务版本",
                ) from exc
            session.commit()

    def _load_target(self, task_id: str) -> _RecoveryTarget:
        with self._session_factory() as session:
            task = TaskRepository(session).get(task_id)
            if task is None:
                raise ApplicationError(
                    code="RECOVERY_TASK_NOT_FOUND",
                    status=404,
                    title="启动恢复任务不存在",
                    detail="恢复扫描期间任务已不存在",
                )
            try:
                status = TaskStatus(task.status)
            except ValueError as exc:
                raise ApplicationError(
                    code="RECOVERY_TASK_STATE_INVALID",
                    status=409,
                    title="启动恢复任务状态无效",
                    detail="任务保存了未知状态",
                ) from exc
            checkpoint = deepcopy(task.checkpoint)
            plan_id = checkpoint.get("execution_plan_id")
            if not isinstance(plan_id, str) or not plan_id:
                raise ApplicationError(
                    code="RECOVERY_CHECKPOINT_INVALID",
                    status=409,
                    title="启动恢复检查点无效",
                    detail="活动任务缺少 execution_plan_id，禁止猜测恢复目标",
                )
            plan = TaskExecutionPlanRepository(session).get(plan_id)
            if plan is None or plan.task_id != task.id:
                raise ApplicationError(
                    code="RECOVERY_PLAN_MISMATCH",
                    status=409,
                    title="启动恢复 execution plan 不匹配",
                    detail="检查点引用的 execution plan 不属于当前任务",
                )
            if checkpoint.get("execution_plan_digest") != plan.plan_digest:
                raise ApplicationError(
                    code="RECOVERY_PLAN_MISMATCH",
                    status=409,
                    title="启动恢复 execution plan 不匹配",
                    detail="检查点中的 execution plan digest 与持久化计划不一致",
                )
            return _RecoveryTarget(
                task_id=task.id,
                unit_id=plan.task_unit_id,
                execution_plan_id=plan.id,
                status=status,
            )


def _report(items: tuple[TaskRecoveryItem, ...], *, truncated: bool) -> TaskRecoveryReport:
    return TaskRecoveryReport(
        items=items,
        scanned_count=len(items),
        completed_count=sum(item.outcome is RecoveryOutcome.COMPLETED for item in items),
        waiting_count=sum(item.outcome is RecoveryOutcome.WAITING for item in items),
        blocked_count=sum(item.outcome is RecoveryOutcome.BLOCKED for item in items),
        truncated=truncated,
    )


def _recovery_statuses(recover_abandoned_analysis: bool) -> tuple[TaskStatus, ...]:
    return _STARTUP_RECOVERABLE_STATUSES if recover_abandoned_analysis else _RECOVERABLE_STATUSES


def _recovery_cancelling_evidence_invalid(detail: str) -> ApplicationError:
    return ApplicationError(
        code="RECOVERY_CANCELLING_EVIDENCE_INVALID",
        status=409,
        title="遗留分析取消恢复证据无效",
        detail=detail,
    )
