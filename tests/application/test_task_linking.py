from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.errors import ApplicationError
from backend.app.application.filesystem_operations import FilesystemOperationService
from backend.app.application.task_linking import TaskLinkingCoordinator
from backend.app.application.tasks import ExecutionPlanView
from backend.app.domain.execution_plan import (
    ExecutionPlanAction,
    ExecutionPlanActionKind,
    ExecutionPlanSnapshot,
)
from backend.app.domain.task_state import TaskStatus
from backend.app.domain.verification import VerificationLevel
from backend.app.infrastructure.persistence.base import Base
from backend.app.infrastructure.persistence.database import (
    create_session_factory,
    create_sqlite_engine,
)
from backend.app.infrastructure.persistence.models import (
    OperationJournal,
    PreflightSnapshotRecord,
    TaskCandidateRecord,
    TaskEvent,
    TaskExecutionGateRecord,
    TaskReviewRevisionRecord,
    TaskUnitRecord,
    UnpackTask,
    new_uuid,
    utc_now,
)
from backend.app.infrastructure.persistence.task_analysis_repositories import (
    TaskExecutionPlanRepository,
)
from backend.app.infrastructure.safe_filesystem import SafeFilesystemGateway
from backend.app.infrastructure.source_inventory import (
    scan_source_inventory,
    source_inventory_digest,
)


@dataclass
class _FakePlanProvider:
    view: ExecutionPlanView
    on_get: Callable[[], None] | None = None
    calls: int = 0

    def get_execution_plan(self, unit_id: str) -> ExecutionPlanView:
        assert unit_id
        self.calls += 1
        if self.on_get is not None:
            self.on_get()
        return self.view


@dataclass(frozen=True, slots=True)
class _LinkingFixture:
    coordinator: TaskLinkingCoordinator
    provider: _FakePlanProvider
    factory: sessionmaker[Session]
    data_root: Path
    source_file: Path
    target_file: Path
    target_downloader_id: str
    task_id: str
    unit_id: str
    plan_id: str
    candidate_id: str
    preflight_id: str


@pytest.fixture
def linking_fixture(tmp_path: Path) -> Iterator[_LinkingFixture]:
    data_root = tmp_path / "data"
    source_root = data_root / "source"
    target_root = data_root / "target"
    source_root.mkdir(parents=True)
    target_root.mkdir()
    source_file = source_root / "movie.mkv"
    source_file.write_bytes(b"synthetic-linking-content")
    inventory = scan_source_inventory(source_root)
    assert len(inventory) == 1
    source = inventory[0]
    inventory_digest = source_inventory_digest(inventory)

    engine = create_sqlite_engine(tmp_path / "task-linking.db")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)

    task_id = new_uuid()
    unit_id = new_uuid()
    preflight_id = new_uuid()
    candidate_id = new_uuid()
    review_id = new_uuid()
    gate_id = new_uuid()
    now = utc_now()
    task_version = 7
    metainfo_digest = "c" * 64
    gate_digest = "d" * 64
    target_downloader_id = "target-downloader"

    with factory() as session:
        session.add(
            UnpackTask(
                id=task_id,
                type="PACKAGE_UNPACK",
                source_downloader_id="source-downloader",
                source_hash="synthetic-source-hash",
                normalized_unit_key="synthetic-movie",
                idempotency_key="a" * 64,
                status=TaskStatus.AWAITING_CONFIRMATION.value,
                trace_id=new_uuid(),
                checkpoint={},
                error_code=None,
                version=task_version,
                created_at=now,
                updated_at=now,
            )
        )
        session.flush()
        session.add(
            PreflightSnapshotRecord(
                id=preflight_id,
                task_id=task_id,
                task_version=task_version,
                normalized_unit_key="synthetic-movie",
                source_inventory_digest=inventory_digest,
                snapshot_digest="b" * 64,
                payload={},
                created_at=now,
            )
        )
        session.flush()
        session.add(
            TaskUnitRecord(
                id=unit_id,
                task_id=task_id,
                normalized_unit_key="synthetic-movie",
                source_root="source",
                source_inventory_digest=inventory_digest,
                kind="MOVIE",
                source_relative_path="movie.mkv",
                length=source.length,
                descriptor={},
                discovered_at=now,
            )
        )
        session.flush()
        session.add(
            TaskCandidateRecord(
                id=candidate_id,
                preflight_snapshot_id=preflight_id,
                task_id=task_id,
                normalized_unit_key="synthetic-movie",
                site_id="synthetic-site",
                torrent_id="torrent-1",
                display_name="Synthetic Movie",
                score=100.0,
                rejected=False,
                selected_for_verification=True,
                verification_level=VerificationLevel.FULL_VERIFIED.value,
                metainfo_digest=metainfo_digest,
                error_code=None,
                evidence={},
                created_at=now,
            )
        )
        session.flush()
        session.add(
            TaskReviewRevisionRecord(
                id=review_id,
                task_id=task_id,
                task_unit_id=unit_id,
                preflight_snapshot_id=preflight_id,
                approved_candidate_id=candidate_id,
                rejected_candidate_ids=[],
                manual_mappings=[],
                note=None,
                requires_reverification=False,
                actor_kind="ADMIN",
                actor_id="test-admin",
                version=1,
                created_at=now,
            )
        )
        session.flush()
        session.add(
            TaskExecutionGateRecord(
                id=gate_id,
                task_id=task_id,
                task_unit_id=unit_id,
                preflight_snapshot_id=preflight_id,
                review_revision_id=review_id,
                candidate_id=candidate_id,
                review_verification_id=None,
                task_version=task_version,
                eligible=True,
                client_check_required=False,
                verification_level=VerificationLevel.FULL_VERIFIED.value,
                metainfo_digest=metainfo_digest,
                blocked_reasons=[],
                gate_digest=gate_digest,
                payload={"review_version": 1},
                created_at=now,
            )
        )
        session.flush()
        snapshot = ExecutionPlanSnapshot(
            task_id=task_id,
            task_version=task_version,
            task_unit_id=unit_id,
            execution_gate_id=gate_id,
            execution_gate_digest=gate_digest,
            preflight_snapshot_id=preflight_id,
            review_revision_id=review_id,
            candidate_id=candidate_id,
            source_inventory_digest=inventory_digest,
            metainfo_digest=metainfo_digest,
            verification_level=VerificationLevel.FULL_VERIFIED,
            client_check_required=False,
            source_root="source",
            target_root="target",
            target_device=target_root.stat().st_dev,
            target_downloader_id=target_downloader_id,
            target_downloader_version=1,
            target_downloader_binding_digest="e" * 64,
            target_remote_save_path="/downloads/target",
            actions=(
                ExecutionPlanAction(
                    torrent_path="Pack/movie.mkv",
                    kind=ExecutionPlanActionKind.HARDLINK,
                    length=source.length,
                    source_relative_path="movie.mkv",
                    source_snapshot=source.snapshot,
                ),
            ),
            create_directories=("Pack",),
            estimated_download_bytes_upper_bound=0,
            blocked_reasons=(),
            created_at=now,
        )
        plan, _ = TaskExecutionPlanRepository(session).create_or_get(snapshot)
        session.commit()

    view = ExecutionPlanView(
        id=plan.id,
        plan_digest=plan.plan_digest,
        ready=True,
        current=True,
        current_reasons=(),
        target_root="target",
        target_device=target_root.stat().st_dev,
        target_downloader_id=target_downloader_id,
        target_downloader_version=1,
        target_remote_save_path="/downloads/target",
        verification_level=VerificationLevel.FULL_VERIFIED.value,
        client_check_required=False,
        hardlink_count=1,
        client_fetch_count=0,
        create_directory_count=1,
        estimated_download_bytes_upper_bound=0,
        blocked_reasons=(),
        actions=(
            {
                "torrent_path": "Pack/movie.mkv",
                "kind": ExecutionPlanActionKind.HARDLINK.value,
                "length": source.length,
                "source_relative_path": "movie.mkv",
            },
        ),
        execution_allowed=False,
        side_effects_started=False,
        created_at=now,
    )
    provider = _FakePlanProvider(view)
    filesystem_operations = FilesystemOperationService(
        factory,
        SafeFilesystemGateway(data_root),
    )
    coordinator = TaskLinkingCoordinator(
        factory,
        provider,
        filesystem_operations,
        data_root=data_root,
    )
    yield _LinkingFixture(
        coordinator=coordinator,
        provider=provider,
        factory=factory,
        data_root=data_root,
        source_file=source_file,
        target_file=target_root / "Pack" / "movie.mkv",
        target_downloader_id=target_downloader_id,
        task_id=task_id,
        unit_id=unit_id,
        plan_id=plan.id,
        candidate_id=candidate_id,
        preflight_id=preflight_id,
    )
    engine.dispose()


def test_linking_reserves_checkpoint_and_replays_without_new_side_effects(
    linking_fixture: _LinkingFixture,
) -> None:
    first = linking_fixture.coordinator.execute(
        linking_fixture.unit_id,
        execution_plan_id=linking_fixture.plan_id,
    )
    source_stat = linking_fixture.source_file.stat(follow_symlinks=False)
    target_stat = linking_fixture.target_file.stat(follow_symlinks=False)
    assert first.replayed is False
    assert first.linked_file_count == 1
    assert source_stat.st_ino == target_stat.st_ino

    with linking_fixture.factory() as session:
        task = session.get(UnpackTask, linking_fixture.task_id)
        assert task is not None
        assert task.status == TaskStatus.ADDING.value
        assert task.version == 9
        assert task.checkpoint["execution_plan_id"] == linking_fixture.plan_id
        assert task.checkpoint["target_downloader_id"] == linking_fixture.target_downloader_id
        events = list(
            session.scalars(
                select(TaskEvent)
                .where(TaskEvent.task_id == linking_fixture.task_id)
                .order_by(TaskEvent.created_at, TaskEvent.id)
            )
        )
        journal_count = session.scalar(select(func.count()).select_from(OperationJournal))
        assert events[-1].event_type == "LINKING_COMPLETED"
        assert "1 个 hardlink" in events[-1].reason
        assert "1 个目录" in events[-1].reason
        assert journal_count == 2

    repeated = linking_fixture.coordinator.execute(
        linking_fixture.unit_id,
        execution_plan_id=linking_fixture.plan_id,
    )
    assert repeated.replayed is True
    assert repeated.hardlink_journal_ids == first.hardlink_journal_ids
    assert linking_fixture.provider.calls == 1
    with linking_fixture.factory() as session:
        assert session.scalar(select(func.count()).select_from(OperationJournal)) == 2


def test_stale_plan_is_rejected_before_task_transition_or_file_write(
    linking_fixture: _LinkingFixture,
) -> None:
    linking_fixture.provider.view = replace(
        linking_fixture.provider.view,
        current=False,
        current_reasons=("TARGET_STATE_CHANGED",),
    )

    with pytest.raises(ApplicationError) as exc_info:
        linking_fixture.coordinator.execute(
            linking_fixture.unit_id,
            execution_plan_id=linking_fixture.plan_id,
        )

    assert exc_info.value.code == "LINKING_PLAN_NOT_CURRENT"
    assert not linking_fixture.target_file.exists()
    with linking_fixture.factory() as session:
        task = session.get(UnpackTask, linking_fixture.task_id)
        assert task is not None
        assert task.status == TaskStatus.AWAITING_CONFIRMATION.value
        assert session.scalar(select(func.count()).select_from(OperationJournal)) == 0


def test_new_review_revision_invalidates_plan_during_final_database_recheck(
    linking_fixture: _LinkingFixture,
) -> None:
    with linking_fixture.factory() as session:
        session.add(
            TaskReviewRevisionRecord(
                id=new_uuid(),
                task_id=linking_fixture.task_id,
                task_unit_id=linking_fixture.unit_id,
                preflight_snapshot_id=linking_fixture.preflight_id,
                approved_candidate_id=linking_fixture.candidate_id,
                rejected_candidate_ids=[],
                manual_mappings=[],
                note="new revision after plan",
                requires_reverification=False,
                actor_kind="ADMIN",
                actor_id="test-admin",
                version=2,
                created_at=utc_now(),
            )
        )
        session.commit()

    with pytest.raises(ApplicationError) as exc_info:
        linking_fixture.coordinator.execute(
            linking_fixture.unit_id,
            execution_plan_id=linking_fixture.plan_id,
        )

    assert exc_info.value.code == "LINKING_PLAN_NOT_CURRENT"
    assert not linking_fixture.target_file.exists()
    with linking_fixture.factory() as session:
        task = session.get(UnpackTask, linking_fixture.task_id)
        assert task is not None
        assert task.status == TaskStatus.AWAITING_CONFIRMATION.value


def test_source_inventory_change_after_authorization_blocks_before_file_side_effect(
    linking_fixture: _LinkingFixture,
) -> None:
    def mutate_source_inventory() -> None:
        (linking_fixture.source_file.parent / "new-sidecar.nfo").write_text("changed")

    linking_fixture.provider.on_get = mutate_source_inventory
    with pytest.raises(ApplicationError) as exc_info:
        linking_fixture.coordinator.execute(
            linking_fixture.unit_id,
            execution_plan_id=linking_fixture.plan_id,
        )

    assert exc_info.value.code == "LINKING_SOURCE_CHANGED"
    assert not linking_fixture.target_file.exists()
    with linking_fixture.factory() as session:
        task = session.get(UnpackTask, linking_fixture.task_id)
        assert task is not None
        assert task.status == TaskStatus.LINKING.value
        assert task.checkpoint["execution_plan_id"] == linking_fixture.plan_id
        assert session.scalar(select(func.count()).select_from(OperationJournal)) == 0


def test_linking_resume_requires_matching_authorization_checkpoint(
    linking_fixture: _LinkingFixture,
) -> None:
    with linking_fixture.factory() as session:
        task = session.get(UnpackTask, linking_fixture.task_id)
        assert task is not None
        task.status = TaskStatus.LINKING.value
        task.version = 8
        task.checkpoint = {"stage": "LINKING", "execution_plan_id": "different-plan"}
        session.commit()

    with pytest.raises(ApplicationError) as exc_info:
        linking_fixture.coordinator.execute(
            linking_fixture.unit_id,
            execution_plan_id=linking_fixture.plan_id,
        )

    assert exc_info.value.code == "LINKING_CHECKPOINT_MISMATCH"
    assert not linking_fixture.target_file.exists()
