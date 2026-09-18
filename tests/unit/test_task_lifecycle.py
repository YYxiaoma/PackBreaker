from backend.app.domain.task_lifecycle import (
    TaskLifecycleAuthorization,
    TaskLifecycleRiskLevel,
    TaskLifecycleStage,
    classify_execution_plan_risk,
    project_task_lifecycle,
)
from backend.app.domain.task_state import TaskStatus


def test_lifecycle_projection_preserves_review_boundary_before_plan() -> None:
    pending = project_task_lifecycle(status=TaskStatus.PENDING)
    assert pending.stage is TaskLifecycleStage.ANALYZE
    assert pending.authorization is TaskLifecycleAuthorization.NOT_READY
    assert pending.side_effects_started is False

    preflight = project_task_lifecycle(status=TaskStatus.PREFLIGHT)
    assert preflight.stage is TaskLifecycleStage.REVIEW
    assert preflight.authorization is TaskLifecycleAuthorization.REVIEW_REQUIRED
    assert preflight.side_effects_started is False


def test_current_safe_plan_can_be_auto_authorized_without_expanding_action_scope() -> None:
    projection = project_task_lifecycle(
        status=TaskStatus.AWAITING_CONFIRMATION,
        execution_plan_id="plan-1",
        plan_ready=True,
        plan_action_kinds=("HARDLINK", "CLIENT_FETCH"),
    )

    assert projection.stage is TaskLifecycleStage.AUTHORIZE
    assert projection.risk_level is TaskLifecycleRiskLevel.LOW
    assert projection.authorization is TaskLifecycleAuthorization.AUTO_AUTHORIZED
    assert projection.side_effects_started is False


def test_unknown_future_plan_action_fails_closed_into_explicit_approval() -> None:
    assert (
        classify_execution_plan_risk(
            action_kinds=("DELETE_SOURCE",),
            blocked_reasons=(),
        )
        is TaskLifecycleRiskLevel.HIGH
    )
    projection = project_task_lifecycle(
        status=TaskStatus.AWAITING_CONFIRMATION,
        execution_plan_id="plan-risky",
        plan_ready=True,
        plan_action_kinds=("DELETE_SOURCE",),
    )
    assert projection.stage is TaskLifecycleStage.AUTHORIZE
    assert projection.authorization is TaskLifecycleAuthorization.APPROVAL_REQUIRED
    assert projection.side_effects_started is False


def test_high_risk_plan_reflects_persisted_approval_decision() -> None:
    approved = project_task_lifecycle(
        status=TaskStatus.AWAITING_CONFIRMATION,
        execution_plan_id="plan-risky",
        plan_ready=True,
        plan_action_kinds=("DELETE_SOURCE",),
        approval_state="APPROVED",
    )
    rejected = project_task_lifecycle(
        status=TaskStatus.AWAITING_CONFIRMATION,
        execution_plan_id="plan-risky",
        plan_ready=True,
        plan_action_kinds=("DELETE_SOURCE",),
        approval_state="REJECTED",
    )

    assert approved.authorization is TaskLifecycleAuthorization.AUTHORIZED
    assert rejected.authorization is TaskLifecycleAuthorization.REJECTED


def test_blocked_plan_never_becomes_auto_authorized() -> None:
    projection = project_task_lifecycle(
        status=TaskStatus.AWAITING_CONFIRMATION,
        execution_plan_id="plan-blocked",
        plan_ready=False,
        plan_action_kinds=("HARDLINK",),
        blocked_reasons=("TARGET_EXISTS",),
    )

    assert projection.risk_level is TaskLifecycleRiskLevel.UNKNOWN
    assert projection.authorization is TaskLifecycleAuthorization.BLOCKED
    assert projection.side_effects_started is False


def test_execution_and_verification_stages_report_side_effect_boundary() -> None:
    linking = project_task_lifecycle(status=TaskStatus.LINKING)
    assert linking.stage is TaskLifecycleStage.EXECUTE
    assert linking.authorization is TaskLifecycleAuthorization.AUTHORIZED
    assert linking.side_effects_started is True

    verifying = project_task_lifecycle(status=TaskStatus.CLIENT_VERIFYING)
    assert verifying.stage is TaskLifecycleStage.VERIFY
    assert verifying.authorization is TaskLifecycleAuthorization.AUTHORIZED
    assert verifying.side_effects_started is True
