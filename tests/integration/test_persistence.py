from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.domain.errors import DomainViolation, ErrorCode
from backend.app.domain.operation import OperationStatus
from backend.app.domain.task_state import TaskStatus
from backend.app.domain.verification import DownloaderKind, VerificationLevel
from backend.app.infrastructure.persistence.base import Base
from backend.app.infrastructure.persistence.database import (
    create_session_factory,
    create_sqlite_engine,
)
from backend.app.infrastructure.persistence.models import OperationJournal, TaskEvent, UnpackTask
from backend.app.infrastructure.persistence.repositories import (
    OperationIntent,
    OperationJournalRepository,
    TaskCreate,
    TaskRepository,
)


@pytest.fixture
def db_session(tmp_path: Path) -> Iterator[Session]:
    engine = create_sqlite_engine(tmp_path / "packbreaker-test.db")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    with factory() as session:
        yield session
    engine.dispose()


def _task_request(unit_key: str = "synthetic-movie-2026") -> TaskCreate:
    return TaskCreate(
        task_type="PACKAGE_UNPACK",
        source_downloader_id="source-downloader",
        source_hash="synthetic-source-hash",
        normalized_unit_key=unit_key,
        trace_id=str(uuid4()),
    )


def _advance_to_adding(repository: TaskRepository, task: UnpackTask) -> UnpackTask:
    current = task
    for to_status in (
        TaskStatus.ANALYZING,
        TaskStatus.SEARCHING,
        TaskStatus.MATCHING,
        TaskStatus.VERIFYING,
        TaskStatus.PREFLIGHT,
        TaskStatus.LINKING,
        TaskStatus.ADDING,
    ):
        current = repository.transition(
            task_id=current.id,
            expected_version=current.version,
            to_status=to_status,
            event_type=f"ENTER_{to_status.value}",
            reason="合成主链路推进",
        )
    return current


def test_sqlite_connections_enable_wal_and_foreign_keys(tmp_path: Path) -> None:
    engine = create_sqlite_engine(tmp_path / "pragma-test.db")
    with engine.connect() as connection:
        journal_mode = connection.exec_driver_sql("PRAGMA journal_mode").scalar_one()
        foreign_keys = connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one()

    assert str(journal_mode).lower() == "wal"
    assert foreign_keys == 1
    engine.dispose()


def test_task_create_replayed_ten_times_is_unique(db_session: Session) -> None:
    repository = TaskRepository(db_session)
    results = [repository.create_or_get(_task_request()) for _ in range(10)]
    db_session.commit()

    task_count = db_session.scalar(select(func.count()).select_from(UnpackTask))
    event_count = db_session.scalar(select(func.count()).select_from(TaskEvent))
    task_ids = {task.id for task, _ in results}
    create_count = sum(1 for _, created in results if created)

    assert create_count == 1
    assert len(task_ids) == 1
    assert task_count == 1
    assert event_count == 1
    first = results[0][0]
    db_session.expire(first)
    db_session.refresh(first)
    assert first.created_at.utcoffset() == timedelta(0)


def test_task_transition_updates_version_and_appends_event(db_session: Session) -> None:
    repository = TaskRepository(db_session)
    task, _ = repository.create_or_get(_task_request())
    db_session.commit()

    updated = repository.transition(
        task_id=task.id,
        expected_version=1,
        to_status=TaskStatus.ANALYZING,
        event_type="ANALYSIS_STARTED",
        reason="开始分析",
    )
    db_session.commit()

    events = list(
        db_session.scalars(
            select(TaskEvent).where(TaskEvent.task_id == task.id).order_by(TaskEvent.created_at)
        )
    )
    assert updated.status == TaskStatus.ANALYZING.value
    assert updated.version == 2
    assert [event.event_type for event in events] == ["TASK_CREATED", "ANALYSIS_STARTED"]
    assert events[-1].from_status == TaskStatus.PENDING.value
    assert events[-1].to_status == TaskStatus.ANALYZING.value


def test_stale_task_version_cannot_append_event(db_session: Session) -> None:
    repository = TaskRepository(db_session)
    task, _ = repository.create_or_get(_task_request())
    db_session.commit()
    repository.transition(
        task_id=task.id,
        expected_version=1,
        to_status=TaskStatus.ANALYZING,
        event_type="ANALYSIS_STARTED",
        reason="开始分析",
    )
    db_session.commit()

    with pytest.raises(DomainViolation) as exc_info:
        repository.transition(
            task_id=task.id,
            expected_version=1,
            to_status=TaskStatus.SEARCHING,
            event_type="SEARCH_STARTED",
            reason="过期执行器尝试继续",
        )

    db_session.rollback()
    event_count = db_session.scalar(select(func.count()).select_from(TaskEvent))
    assert exc_info.value.code is ErrorCode.TASK_VERSION_CONFLICT
    assert event_count == 2


def test_repository_enforces_qb_and_transmission_post_add_gate(db_session: Session) -> None:
    repository = TaskRepository(db_session)
    qb_task, _ = repository.create_or_get(_task_request("qb-unit"))
    tr_task, _ = repository.create_or_get(_task_request("tr-unit"))
    qb_task = _advance_to_adding(repository, qb_task)
    tr_task = _advance_to_adding(repository, tr_task)

    qb_task = repository.transition_after_add(
        task_id=qb_task.id,
        expected_version=qb_task.version,
        downloader=DownloaderKind.QBITTORRENT,
        verification_level=VerificationLevel.FULL_VERIFIED,
        skip_checking_enabled=True,
        preflight_current=True,
        event_type="DOWNLOADER_ADDED",
        reason="FULL_VERIFIED 且显式允许跳过校验",
    )
    tr_task = repository.transition_after_add(
        task_id=tr_task.id,
        expected_version=tr_task.version,
        downloader=DownloaderKind.TRANSMISSION,
        verification_level=VerificationLevel.FULL_VERIFIED,
        skip_checking_enabled=True,
        preflight_current=True,
        event_type="DOWNLOADER_ADDED",
        reason="Transmission 必须完整校验",
    )
    db_session.commit()

    assert qb_task.status == TaskStatus.SEEDING.value
    assert tr_task.status == TaskStatus.CLIENT_VERIFYING.value


def test_operation_intent_replay_is_noop_but_changed_payload_conflicts(
    db_session: Session,
) -> None:
    task, _ = TaskRepository(db_session).create_or_get(_task_request())
    db_session.commit()
    repository = OperationJournalRepository(db_session)
    intent = OperationIntent(
        task_id=task.id,
        idempotency_key="a" * 64,
        operation_type="CREATE_HARDLINK",
        target={"relative_path": "Movie/title.mkv"},
        intent={"source_snapshot": "synthetic"},
        before_snapshot=None,
    )

    results = [repository.record_intent(intent) for _ in range(10)]
    db_session.commit()

    first = results[0][0]
    assert sum(1 for _, created in results if created) == 1
    assert len({journal.id for journal, _ in results}) == 1
    assert first.status == OperationStatus.INTENT_RECORDED.value
    assert db_session.scalar(select(func.count()).select_from(OperationJournal)) == 1

    changed = OperationIntent(
        task_id=task.id,
        idempotency_key=intent.idempotency_key,
        operation_type=intent.operation_type,
        target={"relative_path": "Movie/other.mkv"},
        intent=intent.intent,
    )
    with pytest.raises(DomainViolation) as exc_info:
        repository.record_intent(changed)

    assert exc_info.value.code is ErrorCode.IDEMPOTENCY_CONFLICT
