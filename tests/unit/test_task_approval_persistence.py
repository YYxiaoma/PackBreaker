from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.errors import ApplicationError
from backend.app.application.task_actions import TaskActionActor
from backend.app.application.task_definition_executions import TaskDefinitionExecutionService
from backend.app.application.tasks import ExecutionPlanView
from backend.app.domain.task_approval import (
    TaskApprovalDecisionSource,
    TaskApprovalState,
)
from backend.app.domain.task_state import TaskStatus
from backend.app.infrastructure.persistence.base import Base
from backend.app.infrastructure.persistence.models import (
    TaskApprovalRecord,
    TaskDefinition,
    TaskExecutionPolicy,
    TaskRiskSummaryRecord,
)


def _service(tmp_path: Path) -> tuple[TaskDefinitionExecutionService, sessionmaker[Session]]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory: sessionmaker[Session] = sessionmaker(
        bind=engine,
        expire_on_commit=False,
    )
    service = TaskDefinitionExecutionService(
        factory,
        cast(Any, None),
        cast(Any, None),
        cast(Any, None),
        data_root=tmp_path,
    )
    return service, factory


def _policy(
    *,
    definition_id: str,
    preauthorized: bool,
    allowlist: list[str],
) -> TaskExecutionPolicy:
    return TaskExecutionPolicy(
        id=f"policy-{definition_id}",
        task_definition_id=definition_id,
        stability_detection_enabled=True,
        stability_wait_seconds=60,
        only_completed_downloads=True,
        initial_scope="NEW_ONLY",
        debounce_seconds=30,
        overlap_policy="SKIP",
        auto_retry_enabled=True,
        max_auto_retries=3,
        retry_intervals_seconds=[60, 300, 900],
        high_risk_preauthorization_enabled=preauthorized,
        high_risk_allowed_action_kinds=allowlist,
    )


def _high_risk_plan(plan_id: str, action_kind: str = "DELETE_SOURCE") -> ExecutionPlanView:
    now = datetime.now(UTC)
    return ExecutionPlanView(
        id=plan_id,
        plan_digest=(plan_id.encode().hex() + "0" * 64)[:64],
        ready=True,
        current=True,
        current_reasons=(),
        target_root="/target",
        target_device=1,
        target_downloader_id="downloader-1",
        target_downloader_version=1,
        target_remote_save_path="/downloads",
        verification_level="FULL_VERIFIED",
        client_check_required=False,
        hardlink_count=0,
        client_fetch_count=0,
        create_directory_count=0,
        estimated_download_bytes_upper_bound=0,
        blocked_reasons=(),
        actions=({"kind": action_kind},),
        execution_allowed=True,
        side_effects_started=False,
        created_at=now,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("plan", "expected_event"),
    [
        (
            replace(
                _high_risk_plan("plan-stale"),
                current=False,
                current_reasons=("TARGET_STATE_CHANGED",),
            ),
            "TASK_LIFECYCLE_PLAN_BLOCKED",
        ),
        (
            _high_risk_plan("plan-awaiting-approval"),
            "TASK_LIFECYCLE_APPROVAL_REQUIRED",
        ),
    ],
)
async def test_lifecycle_never_calls_executor_without_current_authorized_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    plan: ExecutionPlanView,
    expected_event: str,
) -> None:
    service, factory = _service(tmp_path)
    definition_id = "definition-safety-gate"
    with factory() as session:
        session.add(
            TaskDefinition(
                id=definition_id,
                name="manual",
                kind="MANUAL",
                status="ENABLED",
                site_id=None,
                version=1,
            )
        )
        session.add(
            _policy(
                definition_id=definition_id,
                preauthorized=False,
                allowlist=[],
            )
        )
        session.commit()

    monkeypatch.setattr(
        service,
        "_execution_item_unit",
        lambda **_kwargs: ("unit-safety-gate", "task-safety-gate"),
    )
    monkeypatch.setattr(
        service,
        "_load_unpack_task",
        lambda _task_id: SimpleNamespace(
            id="task-safety-gate",
            status=TaskStatus.AWAITING_CONFIRMATION.value,
        ),
    )
    monkeypatch.setattr(
        service,
        "_task_analysis_service",
        SimpleNamespace(
            refresh_execution_gate=lambda _unit_id: SimpleNamespace(
                current=True,
                eligible=True,
                blocked_reasons=(),
            )
        ),
    )

    async def fake_create_plan(*_args: object, **_kwargs: object) -> ExecutionPlanView:
        return plan

    monkeypatch.setattr(service, "create_execution_plan", fake_create_plan)
    events: list[str] = []
    monkeypatch.setattr(
        service,
        "_append_lifecycle_event",
        lambda **kwargs: events.append(cast(str, kwargs["event_code"])),
    )

    class _ExecutorMustNotRun:
        calls = 0

        async def execute(self, *_args: object, **_kwargs: object) -> object:
            self.calls += 1
            raise AssertionError("未获得当前有效授权前不得调用副作用执行器")

    executor = _ExecutorMustNotRun()
    monkeypatch.setattr(service, "_task_action_service", executor)

    await service._advance_execution_item(  # noqa: SLF001
        definition_id=definition_id,
        execution_id="execution-safety-gate",
        item_id="item-safety-gate",
        actor=TaskActionActor("ADMIN", "stage-h"),
        idempotency_key="stage-h-safety-gate",
    )

    assert executor.calls == 0
    assert expected_event in events
    assert plan.side_effects_started is False
    with factory() as session:
        approval = session.query(TaskApprovalRecord).one()
        assert approval.state == TaskApprovalState.PENDING.value
        assert approval.plan_digest == plan.plan_digest


def test_manual_high_risk_plan_creates_one_pending_approval(tmp_path: Path) -> None:
    service, factory = _service(tmp_path)
    definition_id = "definition-manual"
    with factory() as session:
        session.add(
            TaskDefinition(
                id=definition_id,
                name="manual",
                kind="MANUAL",
                status="ENABLED",
                site_id=None,
                version=1,
            )
        )
        session.add(
            _policy(
                definition_id=definition_id,
                preauthorized=False,
                allowlist=[],
            )
        )
        session.commit()

    plan = _high_risk_plan("plan-manual")
    first_risk, first_approval = service._ensure_risk_and_approval(  # noqa: SLF001
        definition_id=definition_id,
        unit_id="unit-1",
        task_id="task-1",
        plan=plan,
    )
    second_risk, second_approval = service._ensure_risk_and_approval(  # noqa: SLF001
        definition_id=definition_id,
        unit_id="unit-1",
        task_id="task-1",
        plan=plan,
    )

    assert first_risk.risk_level == "HIGH"
    assert first_approval is not None
    assert first_approval.state == TaskApprovalState.PENDING.value
    assert first_approval.decision_source is None
    assert second_risk.id == first_risk.id
    assert second_approval is not None
    assert second_approval.id == first_approval.id
    with factory() as session:
        assert session.query(TaskRiskSummaryRecord).count() == 1
        assert session.query(TaskApprovalRecord).count() == 1


def test_monitor_high_risk_plan_only_preauthorizes_when_allowlist_covers_action(
    tmp_path: Path,
) -> None:
    service, factory = _service(tmp_path)
    definition_id = "definition-monitor"
    with factory() as session:
        session.add(
            TaskDefinition(
                id=definition_id,
                name="monitor",
                kind="MONITOR",
                status="ENABLED",
                site_id=None,
                version=1,
            )
        )
        session.add(
            _policy(
                definition_id=definition_id,
                preauthorized=True,
                allowlist=["DELETE_SOURCE"],
            )
        )
        session.commit()

    _risk, approved = service._ensure_risk_and_approval(  # noqa: SLF001
        definition_id=definition_id,
        unit_id="unit-covered",
        task_id="task-covered",
        plan=_high_risk_plan("plan-covered"),
    )
    _risk, pending = service._ensure_risk_and_approval(  # noqa: SLF001
        definition_id=definition_id,
        unit_id="unit-uncovered",
        task_id="task-uncovered",
        plan=_high_risk_plan("plan-uncovered", "REPLACE_EXISTING"),
    )

    assert approved is not None
    assert approved.state == TaskApprovalState.APPROVED.value
    assert approved.decision_source == TaskApprovalDecisionSource.PREAUTHORIZED.value
    assert pending is not None
    assert pending.state == TaskApprovalState.PENDING.value
    assert pending.decision_source is None


def test_new_plan_expires_older_pending_approval_for_same_run(tmp_path: Path) -> None:
    service, factory = _service(tmp_path)
    definition_id = "definition-expiration"
    with factory() as session:
        session.add(
            TaskDefinition(
                id=definition_id,
                name="manual",
                kind="MANUAL",
                status="ENABLED",
                site_id=None,
                version=1,
            )
        )
        session.add(
            _policy(
                definition_id=definition_id,
                preauthorized=False,
                allowlist=[],
            )
        )
        session.commit()

    _risk, first = service._ensure_risk_and_approval(  # noqa: SLF001
        definition_id=definition_id,
        unit_id="unit-expiration",
        task_id="task-expiration",
        plan=_high_risk_plan("plan-old"),
    )
    _risk, second = service._ensure_risk_and_approval(  # noqa: SLF001
        definition_id=definition_id,
        unit_id="unit-expiration",
        task_id="task-expiration",
        plan=_high_risk_plan("plan-new"),
    )

    assert first is not None and second is not None
    with factory() as session:
        old = session.get(TaskApprovalRecord, first.id)
        new = session.get(TaskApprovalRecord, second.id)
        assert old is not None and new is not None
        assert old.state == TaskApprovalState.EXPIRED.value
        assert old.actor_kind == "SYSTEM"
        assert old.actor_id == "execution-plan-superseded"
        assert old.decided_at is not None
        assert new.state == TaskApprovalState.PENDING.value


@pytest.mark.asyncio
async def test_web_decision_is_replay_safe_and_final_for_one_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, factory = _service(tmp_path)
    definition_id = "definition-decision"
    with factory() as session:
        session.add(
            TaskDefinition(
                id=definition_id,
                name="manual",
                kind="MANUAL",
                status="ENABLED",
                site_id=None,
                version=1,
            )
        )
        session.add(
            _policy(
                definition_id=definition_id,
                preauthorized=False,
                allowlist=[],
            )
        )
        session.commit()

    plan = _high_risk_plan("plan-decision")
    service._ensure_risk_and_approval(  # noqa: SLF001
        definition_id=definition_id,
        unit_id="unit-decision",
        task_id="task-decision",
        plan=plan,
    )
    monkeypatch.setattr(
        service,
        "_execution_item_unit",
        lambda **_kwargs: ("unit-decision", "task-decision"),
    )
    monkeypatch.setattr(
        service,
        "_load_unpack_task",
        lambda _task_id: SimpleNamespace(status=TaskStatus.AWAITING_CONFIRMATION.value),
    )

    async def fake_create_plan(*_args: object, **_kwargs: object) -> ExecutionPlanView:
        return plan

    advanced_keys: list[str] = []

    async def fake_advance_item(*_args: object, **kwargs: object) -> None:
        advanced_keys.append(cast(str, kwargs["idempotency_key"]))

    monkeypatch.setattr(service, "create_execution_plan", fake_create_plan)
    monkeypatch.setattr(service, "_advance_execution_item", fake_advance_item)
    monkeypatch.setattr(service, "_append_lifecycle_event", lambda **_kwargs: None)
    result_marker = object()
    monkeypatch.setattr(service, "get_execution", lambda _execution_id: cast(Any, result_marker))
    actor = TaskActionActor("ADMIN", "admin-1")

    first = await service.decide_execution_approval(
        definition_id,
        "execution-1",
        "item-1",
        execution_plan_id=plan.id,
        approve=True,
        note="reviewed",
        actor=actor,
        idempotency_key="approval-replay-key",
    )
    replay = await service.decide_execution_approval(
        definition_id,
        "execution-1",
        "item-1",
        execution_plan_id=plan.id,
        approve=True,
        note="reviewed",
        actor=actor,
        idempotency_key="approval-replay-key",
    )

    assert first is result_marker
    assert replay is result_marker
    assert advanced_keys == ["approval-replay-key", "approval-replay-key"]
    with pytest.raises(ApplicationError) as conflict:
        await service.decide_execution_approval(
            definition_id,
            "execution-1",
            "item-1",
            execution_plan_id=plan.id,
            approve=False,
            note=None,
            actor=actor,
            idempotency_key="different-key",
        )
    assert conflict.value.code == "TASK_APPROVAL_ALREADY_DECIDED"
    with factory() as session:
        approval = session.query(TaskApprovalRecord).one()
        assert approval.state == TaskApprovalState.APPROVED.value
        assert approval.decision_source == TaskApprovalDecisionSource.WEB.value
        assert approval.decision_note == "reviewed"
