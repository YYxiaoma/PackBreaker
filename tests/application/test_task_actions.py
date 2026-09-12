from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.errors import ApplicationError
from backend.app.application.task_actions import (
    CancelTaskAction,
    ExecuteTaskAction,
    TaskActionActor,
    TaskActionService,
)
from backend.app.application.task_cancellation import (
    TaskCancellationRequest,
    TaskCancellationResult,
)
from backend.app.application.task_linking import TaskLinkingResult
from backend.app.domain.task_state import TaskStatus
from backend.app.infrastructure.persistence.base import Base
from backend.app.infrastructure.persistence.database import (
    create_session_factory,
    create_sqlite_engine,
)
from backend.app.infrastructure.persistence.models import (
    PreflightSnapshotRecord,
    TaskActionReceipt,
    TaskCandidateRecord,
    TaskEvent,
    TaskExecutionGateRecord,
    TaskExecutionPlanRecord,
    TaskReviewRevisionRecord,
    TaskUnitRecord,
    UnpackTask,
    new_uuid,
)
from backend.app.infrastructure.persistence.repositories import (
    OperationIntent,
    OperationJournalRepository,
    TaskCreate,
    TaskRepository,
)

ActionFixture = tuple[sessionmaker[Session], str]


class SimulatedCrash(RuntimeError):
    pass


@dataclass
class _Cancellation:
    calls: int = 0
    error: Exception | None = None
    delay: float = 0.0

    async def execute(self, request: TaskCancellationRequest) -> TaskCancellationResult:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            error = self.error
            self.error = None
            raise error
        return TaskCancellationResult(
            task_id=request.task_id,
            task_version=9,
            status=TaskStatus.CANCELLED,
            execution_plan_id="plan-1",
            remove_journal_id=None,
            rolled_back_hardlink_journal_ids=(),
            rolled_back_directory_journal_ids=(),
            replayed=self.calls > 1,
        )


class _LinkingMustNotRun:
    def execute(self, unit_id: str, *, execution_plan_id: str) -> TaskLinkingResult:
        raise AssertionError("cancel 单测不应调用 linking")


@dataclass
class _Linking:
    task_id: str
    calls: int = 0
    error: Exception | None = None

    def execute(self, unit_id: str, *, execution_plan_id: str) -> TaskLinkingResult:
        self.calls += 1
        if self.error is not None:
            error = self.error
            self.error = None
            raise error
        return TaskLinkingResult(
            task_id=self.task_id,
            task_version=7,
            execution_plan_id=execution_plan_id,
            execution_plan_digest="5" * 64,
            linked_file_count=1,
            client_fetch_count=0,
            hardlink_journal_ids=("hardlink-1",),
            directory_journal_ids=("directory-1",),
            replayed=self.calls > 1,
        )


@pytest.fixture
def action_fixture(tmp_path: Path) -> Iterator[ActionFixture]:
    engine = create_sqlite_engine(tmp_path / "actions.db")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    with factory() as session:
        task, _ = TaskRepository(session).create_or_get(
            TaskCreate(
                task_type="PACKAGE_UNPACK",
                source_downloader_id="source",
                source_hash="hash",
                normalized_unit_key="unit",
                trace_id=str(uuid4()),
            )
        )
        session.commit()
        task_id = task.id
    try:
        yield factory, task_id
    finally:
        engine.dispose()


def _add_execution_plan(factory: sessionmaker[Session], task_id: str) -> tuple[str, str]:
    unit_id = new_uuid()
    snapshot_id = new_uuid()
    candidate_id = new_uuid()
    review_id = new_uuid()
    gate_id = new_uuid()
    plan_id = new_uuid()
    with factory() as session:
        session.add(
            TaskUnitRecord(
                id=unit_id,
                task_id=task_id,
                normalized_unit_key="unit",
                source_root="source",
                source_inventory_digest="1" * 64,
                kind="MOVIE",
                source_relative_path="Movie.mkv",
                length=16,
                descriptor={},
            )
        )
        session.flush()
        session.add(
            PreflightSnapshotRecord(
                id=snapshot_id,
                task_id=task_id,
                task_version=1,
                normalized_unit_key="unit",
                source_inventory_digest="1" * 64,
                snapshot_digest="2" * 64,
                payload={},
            )
        )
        session.flush()
        session.add(
            TaskCandidateRecord(
                id=candidate_id,
                preflight_snapshot_id=snapshot_id,
                task_id=task_id,
                normalized_unit_key="unit",
                site_id="site",
                torrent_id="torrent",
                display_name="Movie",
                score=100.0,
                rejected=False,
                selected_for_verification=True,
                verification_level="FULL_VERIFIED",
                metainfo_digest="3" * 64,
                error_code=None,
                evidence={},
            )
        )
        session.flush()
        session.add(
            TaskReviewRevisionRecord(
                id=review_id,
                task_id=task_id,
                task_unit_id=unit_id,
                preflight_snapshot_id=snapshot_id,
                approved_candidate_id=candidate_id,
                rejected_candidate_ids=[],
                manual_mappings=[],
                note=None,
                requires_reverification=False,
                actor_kind="admin_session",
                actor_id="actor",
                version=1,
            )
        )
        session.flush()
        session.add(
            TaskExecutionGateRecord(
                id=gate_id,
                task_id=task_id,
                task_unit_id=unit_id,
                preflight_snapshot_id=snapshot_id,
                review_revision_id=review_id,
                candidate_id=candidate_id,
                review_verification_id=None,
                task_version=1,
                eligible=True,
                client_check_required=False,
                verification_level="FULL_VERIFIED",
                metainfo_digest="3" * 64,
                blocked_reasons=[],
                gate_digest="4" * 64,
                payload={},
            )
        )
        session.flush()
        session.add(
            TaskExecutionPlanRecord(
                id=plan_id,
                task_id=task_id,
                task_unit_id=unit_id,
                execution_gate_id=gate_id,
                candidate_id=candidate_id,
                task_version=1,
                target_root=".",
                target_device=1,
                verification_level="FULL_VERIFIED",
                client_check_required=False,
                ready=True,
                blocked_reasons=[],
                estimated_download_bytes_upper_bound=0,
                plan_digest="5" * 64,
                payload={},
            )
        )
        session.commit()
    return plan_id, unit_id


def _set_task_status(
    factory: sessionmaker[Session],
    task_id: str,
    status: TaskStatus,
) -> None:
    with factory() as session:
        task = session.get(UnpackTask, task_id)
        assert task is not None
        task.status = status.value
        task.version += 1
        task.checkpoint = {}
        session.commit()


@pytest.mark.asyncio
async def test_cancel_action_replays_success_without_second_side_effect(
    action_fixture: ActionFixture,
) -> None:
    factory, task_id = action_fixture
    _set_task_status(factory, task_id, TaskStatus.LINKING)
    cancellation = _Cancellation()
    service = TaskActionService(factory, _LinkingMustNotRun(), cancellation)
    request = CancelTaskAction(task_id, True, True)
    actor = TaskActionActor("api_token", "actor-1")

    first = await service.cancel(request, actor=actor, idempotency_key="cancel-1")
    second = await service.cancel(request, actor=actor, idempotency_key="cancel-1")

    assert cancellation.calls == 1
    assert first.idempotency_replayed is False
    assert second.idempotency_replayed is True
    assert first.receipt_id == second.receipt_id
    assert second.status is TaskStatus.CANCELLED
    with factory() as session:
        receipt = session.query(TaskActionReceipt).one()
        assert receipt.idempotency_key_digest != "cancel-1"
        assert len(receipt.idempotency_key_digest) == 64


@pytest.mark.asyncio
async def test_execute_action_replays_receipt_without_second_linking(
    action_fixture: ActionFixture,
) -> None:
    factory, task_id = action_fixture
    plan_id, _ = _add_execution_plan(factory, task_id)
    linking = _Linking(task_id)
    service = TaskActionService(factory, linking, _Cancellation())
    actor = TaskActionActor("admin_session", "session-execute")
    request = ExecuteTaskAction(task_id, plan_id)

    first = await service.execute(request, actor=actor, idempotency_key="execute-1")
    second = await service.execute(request, actor=actor, idempotency_key="execute-1")

    assert linking.calls == 1
    assert first.status is TaskStatus.ADDING
    assert first.idempotency_replayed is False
    assert second.idempotency_replayed is True
    assert second.receipt_id == first.receipt_id


@pytest.mark.asyncio
async def test_execute_pending_receipt_recovers_after_task_advanced_without_second_linking(
    action_fixture: ActionFixture,
) -> None:
    factory, task_id = action_fixture
    plan_id, _ = _add_execution_plan(factory, task_id)
    linking = _Linking(task_id, error=RuntimeError("synthetic post-side-effect crash"))
    service = TaskActionService(factory, linking, _Cancellation())
    actor = TaskActionActor("api_token", "actor-execute-recovery")
    request = ExecuteTaskAction(task_id, plan_id)

    with pytest.raises(RuntimeError, match="synthetic post-side-effect crash"):
        await service.execute(request, actor=actor, idempotency_key="execute-recover")
    with factory() as session:
        receipt = session.query(TaskActionReceipt).one()
        assert receipt.state == "PENDING"
        task = session.get(UnpackTask, task_id)
        assert task is not None
        task.status = TaskStatus.CLIENT_VERIFYING.value
        task.version = 9
        task.checkpoint = {
            "execution_plan_id": plan_id,
            "execution_plan_digest": "5" * 64,
        }
        session.commit()

    recovered = await service.execute(
        request,
        actor=actor,
        idempotency_key="execute-recover",
    )

    assert linking.calls == 1
    assert recovered.status is TaskStatus.CLIENT_VERIFYING
    assert recovered.task_version == 9
    assert recovered.operation_replayed is True
    assert recovered.idempotency_replayed is True


@pytest.mark.asyncio
async def test_cancel_action_same_key_changed_request_conflicts(
    action_fixture: ActionFixture,
) -> None:
    factory, task_id = action_fixture
    _set_task_status(factory, task_id, TaskStatus.LINKING)
    cancellation = _Cancellation()
    service = TaskActionService(factory, _LinkingMustNotRun(), cancellation)
    actor = TaskActionActor("admin_session", "session-1")

    await service.cancel(
        CancelTaskAction(task_id, True, True),
        actor=actor,
        idempotency_key="same-key",
    )
    with pytest.raises(ApplicationError) as exc_info:
        await service.cancel(
            CancelTaskAction(task_id, True, False),
            actor=actor,
            idempotency_key="same-key",
        )

    assert exc_info.value.code == "IDEMPOTENCY_CONFLICT"
    assert cancellation.calls == 1


@pytest.mark.asyncio
async def test_cancel_action_safe_failure_is_persisted_and_replayed(
    action_fixture: ActionFixture,
) -> None:
    factory, task_id = action_fixture
    _set_task_status(factory, task_id, TaskStatus.LINKING)
    cancellation = _Cancellation(
        error=ApplicationError(
            code="CANCELLATION_BLOCKED",
            status=409,
            title="取消被阻断",
            detail="合成安全阻断",
        )
    )
    service = TaskActionService(factory, _LinkingMustNotRun(), cancellation)
    actor = TaskActionActor("api_token", "actor-2")
    request = CancelTaskAction(task_id, True, True)

    for _ in range(2):
        with pytest.raises(ApplicationError) as exc_info:
            await service.cancel(request, actor=actor, idempotency_key="blocked")
        assert exc_info.value.code == "CANCELLATION_BLOCKED"

    assert cancellation.calls == 1
    with factory() as session:
        receipt = session.query(TaskActionReceipt).one()
        assert receipt.state == "FAILED"
        assert receipt.error_payload is not None
        assert receipt.error_payload["code"] == "CANCELLATION_BLOCKED"


@pytest.mark.asyncio
async def test_cancel_action_unknown_failure_leaves_pending_for_safe_retry(
    action_fixture: ActionFixture,
) -> None:
    factory, task_id = action_fixture
    _set_task_status(factory, task_id, TaskStatus.LINKING)
    cancellation = _Cancellation(error=RuntimeError("synthetic unknown result"))
    service = TaskActionService(factory, _LinkingMustNotRun(), cancellation)
    actor = TaskActionActor("api_token", "actor-3")
    request = CancelTaskAction(task_id, True, True)

    with pytest.raises(RuntimeError, match="synthetic unknown result"):
        await service.cancel(request, actor=actor, idempotency_key="unknown")
    with factory() as session:
        assert session.query(TaskActionReceipt).one().state == "PENDING"

    recovered = await service.cancel(request, actor=actor, idempotency_key="unknown")
    assert recovered.idempotency_replayed is True
    assert cancellation.calls == 2


@pytest.mark.asyncio
async def test_cancel_action_ten_concurrent_replays_call_side_effect_once(
    action_fixture: ActionFixture,
) -> None:
    factory, task_id = action_fixture
    _set_task_status(factory, task_id, TaskStatus.LINKING)
    cancellation = _Cancellation(delay=0.01)
    service = TaskActionService(factory, _LinkingMustNotRun(), cancellation)
    actor = TaskActionActor("api_token", "actor-4")
    request = CancelTaskAction(task_id, True, True)

    results = await asyncio.gather(
        *(service.cancel(request, actor=actor, idempotency_key="concurrent") for _ in range(10))
    )

    assert cancellation.calls == 1
    assert len({result.receipt_id for result in results}) == 1
    assert sum(not result.idempotency_replayed for result in results) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "initial_status",
    [
        TaskStatus.PENDING,
        TaskStatus.PREFLIGHT,
        TaskStatus.AWAITING_CONFIRMATION,
        TaskStatus.PAUSED,
        TaskStatus.RETRY,
    ],
)
async def test_pre_side_effect_cancel_finishes_without_resource_coordinator(
    action_fixture: ActionFixture,
    initial_status: TaskStatus,
) -> None:
    factory, task_id = action_fixture
    if initial_status is not TaskStatus.PENDING:
        _set_task_status(factory, task_id, initial_status)
    cancellation = _Cancellation()
    service = TaskActionService(factory, _LinkingMustNotRun(), cancellation)
    actor = TaskActionActor("admin_session", f"pre-cancel-{initial_status.value}")
    request = CancelTaskAction(task_id, False, False)

    first = await service.cancel(request, actor=actor, idempotency_key="pre-cancel")
    second = await service.cancel(request, actor=actor, idempotency_key="pre-cancel")

    assert cancellation.calls == 0
    assert first.status is TaskStatus.CANCELLED
    assert first.execution_plan_id is None
    assert first.operation_replayed is False
    assert second.status is TaskStatus.CANCELLED
    assert second.idempotency_replayed is True
    with factory() as session:
        task = session.get(UnpackTask, task_id)
        assert task is not None
        assert task.status == TaskStatus.CANCELLED.value
        assert task.checkpoint == {
            "schema_version": "packbreaker-pre-side-effect-cancellation-v1",
            "stage": "CANCELLED",
            "mode": "NO_SIDE_EFFECTS",
            "requested_from_status": initial_status.value,
            "remove_downloader_task": False,
            "rollback_created_resources": False,
        }
        event_types = [
            item.event_type
            for item in session.query(TaskEvent)
            .filter(TaskEvent.task_id == task_id)
            .order_by(TaskEvent.created_at, TaskEvent.id)
            .all()
        ]
        assert event_types[-3:] == [
            "TASK_CANCEL_REQUESTED",
            "CANCELLATION_STARTED",
            "CANCELLATION_COMPLETED",
        ]


@pytest.mark.asyncio
async def test_pre_side_effect_cancel_recovers_pending_receipt_after_response_loss(
    action_fixture: ActionFixture,
) -> None:
    factory, task_id = action_fixture
    cancellation = _Cancellation()
    service = TaskActionService(factory, _LinkingMustNotRun(), cancellation)
    actor = TaskActionActor("api_token", "pre-cancel-response-loss")
    request = CancelTaskAction(task_id, False, False)

    def crash(checkpoint: str) -> None:
        if checkpoint == "after_cancellation_applied":
            raise SimulatedCrash(checkpoint)

    with pytest.raises(SimulatedCrash):
        await service.cancel(
            request,
            actor=actor,
            idempotency_key="pre-cancel-response-loss",
            fault_hook=crash,
        )

    with factory() as session:
        task = session.get(UnpackTask, task_id)
        receipt = session.query(TaskActionReceipt).one()
        assert task is not None and task.status == TaskStatus.CANCELLED.value
        assert receipt.state == "PENDING"

    recovered = await service.cancel(
        request,
        actor=actor,
        idempotency_key="pre-cancel-response-loss",
    )
    assert recovered.status is TaskStatus.CANCELLED
    assert recovered.operation_replayed is True
    assert recovered.idempotency_replayed is True
    assert cancellation.calls == 0
    with factory() as session:
        assert session.query(TaskActionReceipt).one().state == "SUCCEEDED"


@pytest.mark.asyncio
async def test_pre_side_effect_cancel_rejects_resource_options_and_existing_journal(
    action_fixture: ActionFixture,
) -> None:
    factory, task_id = action_fixture
    cancellation = _Cancellation()
    service = TaskActionService(factory, _LinkingMustNotRun(), cancellation)

    with pytest.raises(ApplicationError) as options:
        await service.cancel(
            CancelTaskAction(task_id, True, False),
            actor=TaskActionActor("admin_session", "pre-cancel-options"),
            idempotency_key="pre-cancel-options",
        )
    assert options.value.code == "CANCELLATION_OPTIONS_NOT_APPLICABLE"

    with factory() as session:
        OperationJournalRepository(session).record_intent(
            OperationIntent(
                task_id=task_id,
                idempotency_key="operation-before-cancel",
                operation_type="CREATE_HARDLINK",
                target={"resource": "synthetic"},
                intent={"synthetic": True},
            )
        )
        session.commit()

    with pytest.raises(ApplicationError) as evidence:
        await service.cancel(
            CancelTaskAction(task_id, False, False),
            actor=TaskActionActor("admin_session", "pre-cancel-journal"),
            idempotency_key="pre-cancel-journal",
        )
    assert evidence.value.code == "CANCELLATION_EVIDENCE_CONFLICT"
    assert cancellation.calls == 0
    with factory() as session:
        task = session.get(UnpackTask, task_id)
        assert task is not None and task.status == TaskStatus.PENDING.value


@pytest.mark.asyncio
async def test_retry_with_operation_journal_delegates_to_resource_cancellation(
    action_fixture: ActionFixture,
) -> None:
    factory, task_id = action_fixture
    _set_task_status(factory, task_id, TaskStatus.RETRY)
    with factory() as session:
        OperationJournalRepository(session).record_intent(
            OperationIntent(
                task_id=task_id,
                idempotency_key="retry-side-effect-cancel",
                operation_type="ISOLATE_REPAIR_TARGET",
                target={"resource": "synthetic"},
                intent={"synthetic": True},
            )
        )
        session.commit()

    cancellation = _Cancellation()
    service = TaskActionService(factory, _LinkingMustNotRun(), cancellation)
    result = await service.cancel(
        CancelTaskAction(task_id, True, False),
        actor=TaskActionActor("admin_session", "retry-side-effect-cancel"),
        idempotency_key="retry-side-effect-cancel",
    )

    assert cancellation.calls == 1
    assert result.status is TaskStatus.CANCELLED
    assert result.execution_plan_id == "plan-1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "initial_status",
    [
        TaskStatus.ANALYZING,
        TaskStatus.SEARCHING,
        TaskStatus.MATCHING,
        TaskStatus.VERIFYING,
    ],
)
async def test_active_analysis_cancel_records_cooperative_request_without_resource_coordinator(
    action_fixture: ActionFixture,
    initial_status: TaskStatus,
) -> None:
    factory, task_id = action_fixture
    _set_task_status(factory, task_id, initial_status)
    cancellation = _Cancellation()
    service = TaskActionService(factory, _LinkingMustNotRun(), cancellation)
    actor = TaskActionActor("admin_session", f"active-{initial_status.value}")
    request = CancelTaskAction(task_id, False, False)
    with factory() as session:
        task = session.get(UnpackTask, task_id)
        assert task is not None
        analysis_version = task.version

    first = await service.cancel(
        request,
        actor=actor,
        idempotency_key=f"active-{initial_status.value}",
    )
    replay = await service.cancel(
        request,
        actor=actor,
        idempotency_key=f"active-{initial_status.value}",
    )

    assert first.status is TaskStatus.CANCELLING
    assert first.execution_plan_id is None
    assert first.operation_replayed is False
    assert replay.status is TaskStatus.CANCELLING
    assert replay.idempotency_replayed is True
    assert cancellation.calls == 0
    with factory() as session:
        task = session.get(UnpackTask, task_id)
        assert task is not None
        assert task.status == TaskStatus.CANCELLING.value
        assert task.checkpoint == {
            "schema_version": "packbreaker-pre-side-effect-cancellation-v1",
            "stage": "CANCELLING",
            "mode": "COOPERATIVE_ANALYSIS",
            "requested_from_status": initial_status.value,
            "remove_downloader_task": False,
            "rollback_created_resources": False,
            "analysis_version": analysis_version,
        }


@pytest.mark.asyncio
async def test_active_analysis_cancel_recovers_pending_receipt_without_resource_coordinator(
    action_fixture: ActionFixture,
) -> None:
    factory, task_id = action_fixture
    _set_task_status(factory, task_id, TaskStatus.SEARCHING)
    cancellation = _Cancellation()
    service = TaskActionService(factory, _LinkingMustNotRun(), cancellation)
    request = CancelTaskAction(task_id, False, False)
    actor = TaskActionActor("api_token", "active-analysis-response-loss")

    def crash(checkpoint: str) -> None:
        if checkpoint == "after_cancellation_applied":
            raise SimulatedCrash(checkpoint)

    with pytest.raises(SimulatedCrash):
        await service.cancel(
            request,
            actor=actor,
            idempotency_key="active-analysis-response-loss",
            fault_hook=crash,
        )
    with factory() as session:
        task = session.get(UnpackTask, task_id)
        receipt = session.query(TaskActionReceipt).one()
        assert task is not None and task.status == TaskStatus.CANCELLING.value
        assert receipt.state == "PENDING"

    recovered = await service.cancel(
        request,
        actor=actor,
        idempotency_key="active-analysis-response-loss",
    )
    assert recovered.status is TaskStatus.CANCELLING
    assert recovered.operation_replayed is True
    assert recovered.idempotency_replayed is True
    assert cancellation.calls == 0
    with factory() as session:
        assert session.query(TaskActionReceipt).one().state == "SUCCEEDED"


@pytest.mark.asyncio
async def test_active_analysis_cancel_rejects_resource_options_without_changing_stage(
    action_fixture: ActionFixture,
) -> None:
    factory, task_id = action_fixture
    _set_task_status(factory, task_id, TaskStatus.SEARCHING)
    cancellation = _Cancellation()
    service = TaskActionService(factory, _LinkingMustNotRun(), cancellation)

    with pytest.raises(ApplicationError) as failure:
        await service.cancel(
            CancelTaskAction(task_id, True, False),
            actor=TaskActionActor("admin_session", "active-analysis-options"),
            idempotency_key="active-analysis-options",
        )
    assert failure.value.code == "CANCELLATION_OPTIONS_NOT_APPLICABLE"
    assert cancellation.calls == 0
    with factory() as session:
        task = session.get(UnpackTask, task_id)
        assert task is not None
        assert task.status == TaskStatus.SEARCHING.value
        assert task.checkpoint == {}


@pytest.mark.asyncio
async def test_resource_cancelled_task_is_delegated_to_resource_coordinator(
    action_fixture: ActionFixture,
) -> None:
    factory, task_id = action_fixture
    _set_task_status(factory, task_id, TaskStatus.CANCELLED)
    cancellation = _Cancellation()
    service = TaskActionService(factory, _LinkingMustNotRun(), cancellation)

    result = await service.cancel(
        CancelTaskAction(task_id, True, True),
        actor=TaskActionActor("admin_session", "resource-cancelled-replay"),
        idempotency_key="resource-cancelled-replay",
    )

    assert cancellation.calls == 1
    assert result.status is TaskStatus.CANCELLED
    assert result.execution_plan_id == "plan-1"


@pytest.mark.asyncio
async def test_cooperative_cancelled_replay_rejects_malformed_analysis_binding(
    action_fixture: ActionFixture,
) -> None:
    factory, task_id = action_fixture
    _set_task_status(factory, task_id, TaskStatus.CANCELLED)
    with factory() as session:
        task = session.get(UnpackTask, task_id)
        assert task is not None
        task.checkpoint = {
            "schema_version": "packbreaker-pre-side-effect-cancellation-v1",
            "stage": "CANCELLED",
            "mode": "COOPERATIVE_ANALYSIS",
            "requested_from_status": "SEARCHING",
            "analysis_version": "invalid",
            "remove_downloader_task": False,
            "rollback_created_resources": False,
        }
        session.commit()
    cancellation = _Cancellation()
    service = TaskActionService(factory, _LinkingMustNotRun(), cancellation)

    with pytest.raises(ApplicationError) as failure:
        await service.cancel(
            CancelTaskAction(task_id, False, False),
            actor=TaskActionActor("admin_session", "malformed-cooperative-cancelled"),
            idempotency_key="malformed-cooperative-cancelled",
        )
    assert failure.value.code == "CANCELLATION_EVIDENCE_INVALID"
    assert cancellation.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_status", [TaskStatus.DONE, TaskStatus.FAILED])
async def test_pre_side_effect_cancel_rejects_terminal_tasks(
    action_fixture: ActionFixture,
    initial_status: TaskStatus,
) -> None:
    factory, task_id = action_fixture
    _set_task_status(factory, task_id, initial_status)
    cancellation = _Cancellation()
    service = TaskActionService(factory, _LinkingMustNotRun(), cancellation)

    with pytest.raises(ApplicationError) as failure:
        await service.cancel(
            CancelTaskAction(task_id, False, False),
            actor=TaskActionActor("admin_session", f"blocked-{initial_status.value}"),
            idempotency_key=f"blocked-{initial_status.value}",
        )
    assert failure.value.code == "CANCELLATION_STATE_INVALID"
    assert cancellation.calls == 0


@pytest.mark.asyncio
async def test_task_action_requires_valid_idempotency_key(
    action_fixture: ActionFixture,
) -> None:
    factory, task_id = action_fixture
    service = TaskActionService(factory, _LinkingMustNotRun(), _Cancellation())
    actor = TaskActionActor("api_token", "actor-5")
    request = CancelTaskAction(task_id, True, True)

    with pytest.raises(ApplicationError) as missing:
        await service.cancel(request, actor=actor, idempotency_key=None)
    assert missing.value.code == "IDEMPOTENCY_KEY_REQUIRED"

    with pytest.raises(ApplicationError) as invalid:
        await service.cancel(request, actor=actor, idempotency_key=" has-space ")
    assert invalid.value.code == "IDEMPOTENCY_KEY_INVALID"
