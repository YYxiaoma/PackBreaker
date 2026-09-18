from backend.app.domain.task_approval import (
    build_task_risk_summary,
    preauthorization_covers,
)
from backend.app.domain.task_lifecycle import TaskLifecycleRiskLevel


def test_risk_summary_is_deterministic_for_low_risk_plan() -> None:
    first = build_task_risk_summary(
        execution_plan_id="plan-1",
        plan_digest="a" * 64,
        action_kinds=("CLIENT_FETCH", "HARDLINK"),
        blocked_reasons=(),
        hardlink_count=1,
        client_fetch_count=1,
        create_directory_count=1,
        estimated_download_bytes_upper_bound=123,
    )
    second = build_task_risk_summary(
        execution_plan_id="plan-1",
        plan_digest="a" * 64,
        action_kinds=("HARDLINK", "CLIENT_FETCH"),
        blocked_reasons=(),
        hardlink_count=1,
        client_fetch_count=1,
        create_directory_count=1,
        estimated_download_bytes_upper_bound=123,
    )
    assert first.risk_level is TaskLifecycleRiskLevel.LOW
    assert first.reason_codes == ("LOW_RISK_ACTION_SET",)
    assert first.risk_digest == second.risk_digest


def test_unknown_action_is_high_risk_and_named_in_evidence() -> None:
    summary = build_task_risk_summary(
        execution_plan_id="plan-risky",
        plan_digest="b" * 64,
        action_kinds=("HARDLINK", "DELETE_SOURCE"),
        blocked_reasons=(),
        hardlink_count=1,
        client_fetch_count=0,
        create_directory_count=0,
        estimated_download_bytes_upper_bound=0,
    )
    assert summary.risk_level is TaskLifecycleRiskLevel.HIGH
    assert summary.reason_codes == ("HIGH_RISK_ACTION:DELETE_SOURCE",)


def test_blocked_plan_is_unknown_and_preserves_block_reason() -> None:
    summary = build_task_risk_summary(
        execution_plan_id="plan-blocked",
        plan_digest="c" * 64,
        action_kinds=("HARDLINK",),
        blocked_reasons=("TARGET_EXISTS",),
        hardlink_count=1,
        client_fetch_count=0,
        create_directory_count=0,
        estimated_download_bytes_upper_bound=0,
    )
    assert summary.risk_level is TaskLifecycleRiskLevel.UNKNOWN
    assert summary.reason_codes == ("PLAN_BLOCKED:TARGET_EXISTS",)


def test_monitor_preauthorization_requires_full_high_risk_allowlist_coverage() -> None:
    actions = ("HARDLINK", "DELETE_SOURCE", "REPLACE_EXISTING")
    assert not preauthorization_covers(
        enabled=False,
        allowed_action_kinds=("DELETE_SOURCE", "REPLACE_EXISTING"),
        action_kinds=actions,
    )
    assert not preauthorization_covers(
        enabled=True,
        allowed_action_kinds=("DELETE_SOURCE",),
        action_kinds=actions,
    )
    assert preauthorization_covers(
        enabled=True,
        allowed_action_kinds=("DELETE_SOURCE", "REPLACE_EXISTING", "FUTURE_EXTRA"),
        action_kinds=actions,
    )
    assert not preauthorization_covers(
        enabled=True,
        allowed_action_kinds=("DELETE_SOURCE",),
        action_kinds=("HARDLINK",),
    )
