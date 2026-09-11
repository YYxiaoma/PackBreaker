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
    TaskExecutionGateRecord,
    TaskExecutionPlanRecord,
    TaskReviewRevisionRecord,
    TaskUnitRecord,
    UnpackTask,
    new_uuid,
)
from backend.app.infrastructure.persistence.repositories import TaskCreate, TaskRepository

ActionFixture = tuple[sessionmaker[Session], str]


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
            qbit_remove_journal_id=None,
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


@pytest.mark.asyncio
async def test_cancel_action_replays_success_without_second_side_effect(
    action_fixture: ActionFixture,
) -> None:
    factory, task_id = action_fixture
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
