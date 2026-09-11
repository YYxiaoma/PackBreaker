from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.errors import ApplicationError
from backend.app.domain.errors import DomainViolation
from backend.app.domain.task_state import TaskStatus
from backend.app.infrastructure.persistence.repositories import TaskRepository
from backend.app.infrastructure.persistence.task_analysis_repositories import (
    TaskExecutionPlanRepository,
)

_RECOVERABLE_STATUSES = (
    TaskStatus.LINKING,
    TaskStatus.ADDING,
    TaskStatus.CLIENT_VERIFYING,
    TaskStatus.SEEDING,
)
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
    ) -> None:
        self._session_factory = session_factory
        self._linking = linking
        self._adding = adding
        self._client_verification = client_verification
        self._seeding = seeding

    async def reconcile_once(
        self,
        *,
        limit: int = _DEFAULT_RECOVERY_LIMIT,
        max_steps_per_task: int = _DEFAULT_MAX_STEPS_PER_TASK,
    ) -> TaskRecoveryReport:
        if limit <= 0:
            raise ValueError("limit 必须大于 0")
        if max_steps_per_task <= 0:
            raise ValueError("max_steps_per_task 必须大于 0")

        task_ids, truncated = self._load_recoverable_task_ids(limit)
        items: list[TaskRecoveryItem] = []
        for task_id in task_ids:
            items.append(
                await self.reconcile_task(
                    task_id,
                    max_steps=max_steps_per_task,
                )
            )
        return _report(tuple(items), truncated=truncated)

    async def reconcile_task(
        self,
        task_id: str,
        *,
        max_steps: int = _DEFAULT_MAX_STEPS_PER_TASK,
    ) -> TaskRecoveryItem:
        if max_steps <= 0:
            raise ValueError("max_steps 必须大于 0")

        initial_status = self._load_task_status(task_id)
        if initial_status not in _RECOVERABLE_STATUSES:
            raise ApplicationError(
                code="RECOVERY_TASK_STATE_INVALID",
                status=409,
                title="任务状态不需要启动恢复",
                detail="只有 LINKING/ADDING/CLIENT_VERIFYING/SEEDING 会进入启动恢复扫描",
            )

        steps: list[TaskStatus] = []
        execution_plan_id: str | None = None
        current_status = initial_status
        try:
            for _ in range(max_steps):
                target = self._load_target(task_id)
                execution_plan_id = target.execution_plan_id
                current_status = target.status
                if current_status not in _RECOVERABLE_STATUSES:
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
                if next_status not in _RECOVERABLE_STATUSES:
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
                    if final_status in _RECOVERABLE_STATUSES
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
            await self._seeding.execute(
                target.unit_id,
                execution_plan_id=target.execution_plan_id,
            )
            return
        raise AssertionError(f"未处理的恢复状态: {target.status.value}")

    def _load_recoverable_task_ids(self, limit: int) -> tuple[tuple[str, ...], bool]:
        with self._session_factory() as session:
            tasks = TaskRepository(session).list_for_recovery(
                statuses=_RECOVERABLE_STATUSES,
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
