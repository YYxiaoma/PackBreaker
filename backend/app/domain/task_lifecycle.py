from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from backend.app.domain.execution_plan import ExecutionPlanActionKind
from backend.app.domain.task_state import TaskStatus


class TaskLifecycleStage(StrEnum):
    DISCOVER = "DISCOVER"
    ANALYZE = "ANALYZE"
    REVIEW = "REVIEW"
    PLAN = "PLAN"
    AUTHORIZE = "AUTHORIZE"
    EXECUTE = "EXECUTE"
    VERIFY = "VERIFY"
    FINALIZE = "FINALIZE"
    COMPLETE = "COMPLETE"


class TaskLifecycleRiskLevel(StrEnum):
    UNKNOWN = "UNKNOWN"
    LOW = "LOW"
    HIGH = "HIGH"


class TaskLifecycleAuthorization(StrEnum):
    NOT_READY = "NOT_READY"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    AUTO_AUTHORIZED = "AUTO_AUTHORIZED"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    REJECTED = "REJECTED"
    BLOCKED = "BLOCKED"
    AUTHORIZED = "AUTHORIZED"
    COMPLETE = "COMPLETE"


@dataclass(frozen=True, slots=True)
class TaskLifecycleProjection:
    stage: TaskLifecycleStage
    risk_level: TaskLifecycleRiskLevel
    authorization: TaskLifecycleAuthorization
    execution_plan_id: str | None
    plan_ready: bool | None
    side_effects_started: bool
    blocked_reasons: tuple[str, ...]


_LOW_RISK_PLAN_ACTIONS = frozenset(
    {
        ExecutionPlanActionKind.HARDLINK.value,
        ExecutionPlanActionKind.CLIENT_FETCH.value,
        ExecutionPlanActionKind.PROTOCOL_PADDING.value,
        ExecutionPlanActionKind.ZERO_LENGTH.value,
    }
)

_SIDE_EFFECT_STATUSES = frozenset(
    {
        TaskStatus.LINKING,
        TaskStatus.ADDING,
        TaskStatus.CLIENT_VERIFYING,
        TaskStatus.SEEDING,
        TaskStatus.CANCELLING,
        TaskStatus.ROLLING_BACK,
        TaskStatus.DONE,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    }
)


def classify_execution_plan_risk(
    *,
    action_kinds: tuple[str, ...],
    blocked_reasons: tuple[str, ...],
) -> TaskLifecycleRiskLevel:
    if blocked_reasons:
        return TaskLifecycleRiskLevel.UNKNOWN
    if not action_kinds:
        return TaskLifecycleRiskLevel.LOW
    if all(kind in _LOW_RISK_PLAN_ACTIONS for kind in action_kinds):
        return TaskLifecycleRiskLevel.LOW
    return TaskLifecycleRiskLevel.HIGH


def project_task_lifecycle(
    *,
    status: TaskStatus,
    execution_plan_id: str | None = None,
    plan_ready: bool | None = None,
    plan_action_kinds: tuple[str, ...] = (),
    blocked_reasons: tuple[str, ...] = (),
    approval_state: str | None = None,
) -> TaskLifecycleProjection:
    side_effects_started = status in _SIDE_EFFECT_STATUSES
    risk = (
        classify_execution_plan_risk(
            action_kinds=plan_action_kinds,
            blocked_reasons=blocked_reasons,
        )
        if execution_plan_id is not None
        else TaskLifecycleRiskLevel.UNKNOWN
    )

    if status in {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED}:
        return TaskLifecycleProjection(
            stage=TaskLifecycleStage.COMPLETE,
            risk_level=risk,
            authorization=TaskLifecycleAuthorization.COMPLETE,
            execution_plan_id=execution_plan_id,
            plan_ready=plan_ready,
            side_effects_started=side_effects_started,
            blocked_reasons=blocked_reasons,
        )
    if status in {TaskStatus.CANCELLING, TaskStatus.ROLLING_BACK}:
        stage = TaskLifecycleStage.FINALIZE
        authorization = TaskLifecycleAuthorization.AUTHORIZED
    elif status in {TaskStatus.CLIENT_VERIFYING, TaskStatus.SEEDING}:
        stage = TaskLifecycleStage.VERIFY
        authorization = TaskLifecycleAuthorization.AUTHORIZED
    elif status in {TaskStatus.LINKING, TaskStatus.ADDING}:
        stage = TaskLifecycleStage.EXECUTE
        authorization = TaskLifecycleAuthorization.AUTHORIZED
    elif status is TaskStatus.AWAITING_CONFIRMATION:
        if execution_plan_id is None:
            stage = TaskLifecycleStage.PLAN
            authorization = TaskLifecycleAuthorization.NOT_READY
        elif not plan_ready or blocked_reasons:
            stage = TaskLifecycleStage.PLAN
            authorization = TaskLifecycleAuthorization.BLOCKED
        elif risk is TaskLifecycleRiskLevel.LOW:
            stage = TaskLifecycleStage.AUTHORIZE
            authorization = TaskLifecycleAuthorization.AUTO_AUTHORIZED
        elif approval_state == "APPROVED":
            stage = TaskLifecycleStage.AUTHORIZE
            authorization = TaskLifecycleAuthorization.AUTHORIZED
        elif approval_state == "REJECTED":
            stage = TaskLifecycleStage.AUTHORIZE
            authorization = TaskLifecycleAuthorization.REJECTED
        else:
            stage = TaskLifecycleStage.AUTHORIZE
            authorization = TaskLifecycleAuthorization.APPROVAL_REQUIRED
    elif status is TaskStatus.PREFLIGHT:
        stage = TaskLifecycleStage.REVIEW
        authorization = TaskLifecycleAuthorization.REVIEW_REQUIRED
    elif status in {
        TaskStatus.PENDING,
        TaskStatus.PAUSED,
        TaskStatus.RETRY,
        TaskStatus.ANALYZING,
        TaskStatus.SEARCHING,
        TaskStatus.MATCHING,
        TaskStatus.VERIFYING,
    }:
        stage = TaskLifecycleStage.ANALYZE
        authorization = TaskLifecycleAuthorization.NOT_READY
    else:
        stage = TaskLifecycleStage.DISCOVER
        authorization = TaskLifecycleAuthorization.NOT_READY

    return TaskLifecycleProjection(
        stage=stage,
        risk_level=risk,
        authorization=authorization,
        execution_plan_id=execution_plan_id,
        plan_ready=plan_ready,
        side_effects_started=side_effects_started,
        blocked_reasons=blocked_reasons,
    )
