from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.filesystem_operations import (
    CREATE_DIRECTORY_OPERATION,
    CREATE_HARDLINK_OPERATION,
    FilesystemOperationService,
    HardlinkExecutionRequest,
)
from backend.app.domain.errors import DomainViolation, ErrorCode
from backend.app.domain.operation import OperationStatus
from backend.app.domain.verification import FileSnapshot
from backend.app.infrastructure.persistence.base import Base
from backend.app.infrastructure.persistence.database import (
    create_session_factory,
    create_sqlite_engine,
)
from backend.app.infrastructure.persistence.models import OperationJournal
from backend.app.infrastructure.persistence.repositories import TaskCreate, TaskRepository
from backend.app.infrastructure.safe_filesystem import SafeFilesystemGateway


class SimulatedCrash(RuntimeError):
    pass


@pytest.fixture
def filesystem_service(
    tmp_path: Path,
) -> Iterator[tuple[FilesystemOperationService, sessionmaker[Session], Path, str]]:
    data_root = tmp_path / "data"
    data_root.mkdir()
    engine = create_sqlite_engine(tmp_path / "packbreaker-filesystem.db")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    with factory() as session:
        task, _ = TaskRepository(session).create_or_get(
            TaskCreate(
                task_type="PACKAGE_UNPACK",
                source_downloader_id="source-downloader",
                source_hash="source-hash",
                normalized_unit_key=str(uuid4()),
                trace_id=str(uuid4()),
            )
        )
        session.commit()
        task_id = task.id
    yield (
        FilesystemOperationService(factory, SafeFilesystemGateway(data_root)),
        factory,
        data_root,
        task_id,
    )
    engine.dispose()


def _source_snapshot(path: Path) -> FileSnapshot:
    result = path.stat(follow_symlinks=False)
    return FileSnapshot(
        device=result.st_dev,
        inode=result.st_ino,
        size=result.st_size,
        mtime_ns=result.st_mtime_ns,
    )


def _request(
    data_root: Path, task_id: str, *, target: str = "Pack/Season 01/movie.mkv"
) -> HardlinkExecutionRequest:
    source = data_root / "source" / "movie.mkv"
    return HardlinkExecutionRequest(
        task_id=task_id,
        candidate_key="c" * 64,
        source_relative_path="source/movie.mkv",
        target_root_relative_path="target",
        target_relative_path=target,
        expected_source_snapshot=_source_snapshot(source),
    )


def _prepare(data_root: Path) -> Path:
    source = data_root / "source" / "movie.mkv"
    source.parent.mkdir()
    (data_root / "target").mkdir()
    source.write_bytes(b"synthetic-media-content")
    return source


def _journals(factory: sessionmaker[Session]) -> list[OperationJournal]:
    with factory() as session:
        return list(session.scalars(select(OperationJournal).order_by(OperationJournal.created_at)))


def test_execute_hardlink_journals_directories_and_is_idempotent(
    filesystem_service: tuple[FilesystemOperationService, sessionmaker[Session], Path, str],
) -> None:
    service, factory, data_root, task_id = filesystem_service
    source = _prepare(data_root)
    before = source.stat(follow_symlinks=False)

    first = service.execute_hardlink(_request(data_root, task_id))
    target = data_root / "target" / "Pack" / "Season 01" / "movie.mkv"
    after = source.stat(follow_symlinks=False)

    assert target.exists()
    assert target.stat().st_ino == source.stat().st_ino
    assert (before.st_ino, before.st_size, before.st_mtime_ns) == (
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    )
    assert after.st_nlink == before.st_nlink + 1
    assert len(first.directory_journal_ids) == 2
    assert first.replayed is False

    second = service.execute_hardlink(_request(data_root, task_id))
    assert second.hardlink_journal_id == first.hardlink_journal_id
    assert second.directory_journal_ids == first.directory_journal_ids
    assert second.replayed is True

    journals = _journals(factory)
    assert [item.operation_type for item in journals].count(CREATE_DIRECTORY_OPERATION) == 2
    assert [item.operation_type for item in journals].count(CREATE_HARDLINK_OPERATION) == 1
    assert {item.status for item in journals} == {OperationStatus.APPLIED.value}


def test_crash_after_temporary_hardlink_can_resume_safely(
    filesystem_service: tuple[FilesystemOperationService, sessionmaker[Session], Path, str],
) -> None:
    service, factory, data_root, task_id = filesystem_service
    _prepare(data_root)
    (data_root / "target" / "Pack").mkdir()
    request = _request(data_root, task_id, target="Pack/movie.mkv")

    def crash(checkpoint: str) -> None:
        if checkpoint == "after_temporary_hardlink":
            raise SimulatedCrash(checkpoint)

    with pytest.raises(SimulatedCrash):
        service.execute_hardlink(request, fault_hook=crash)

    journals = _journals(factory)
    assert len(journals) == 1
    assert journals[0].status == OperationStatus.INTENT_RECORDED.value
    temporary = (
        data_root / "target" / "Pack" / f".packbreaker-link-{journals[0].idempotency_key}.tmp"
    )
    assert temporary.exists()
    assert not (data_root / "target" / "Pack" / "movie.mkv").exists()

    resumed = service.execute_hardlink(request)
    assert resumed.replayed is True
    assert not temporary.exists()
    assert (data_root / "target" / "Pack" / "movie.mkv").exists()
    assert _journals(factory)[0].status == OperationStatus.APPLIED.value


def test_crash_after_final_hardlink_requires_reconciliation(
    filesystem_service: tuple[FilesystemOperationService, sessionmaker[Session], Path, str],
) -> None:
    service, factory, data_root, task_id = filesystem_service
    _prepare(data_root)
    (data_root / "target" / "Pack").mkdir()
    request = _request(data_root, task_id, target="Pack/movie.mkv")

    def crash(checkpoint: str) -> None:
        if checkpoint == "after_final_hardlink":
            raise SimulatedCrash(checkpoint)

    with pytest.raises(SimulatedCrash):
        service.execute_hardlink(request, fault_hook=crash)

    target = data_root / "target" / "Pack" / "movie.mkv"
    assert target.exists()
    assert _journals(factory)[0].status == OperationStatus.INTENT_RECORDED.value

    with pytest.raises(DomainViolation) as failure:
        service.execute_hardlink(request)

    assert failure.value.code is ErrorCode.TARGET_CONFLICT
    assert target.exists()
    assert _journals(factory)[0].status == OperationStatus.RECONCILE_REQUIRED.value


def test_crash_after_directory_creation_does_not_claim_existing_directory(
    filesystem_service: tuple[FilesystemOperationService, sessionmaker[Session], Path, str],
) -> None:
    service, factory, data_root, task_id = filesystem_service
    _prepare(data_root)
    request = _request(data_root, task_id)

    def crash(checkpoint: str) -> None:
        if checkpoint == "after_directory_created:Pack":
            raise SimulatedCrash(checkpoint)

    with pytest.raises(SimulatedCrash):
        service.execute_hardlink(request, fault_hook=crash)

    assert (data_root / "target" / "Pack").is_dir()
    assert _journals(factory)[0].status == OperationStatus.INTENT_RECORDED.value

    with pytest.raises(DomainViolation) as failure:
        service.execute_hardlink(request)

    assert failure.value.code is ErrorCode.TARGET_CONFLICT
    assert _journals(factory)[0].status == OperationStatus.RECONCILE_REQUIRED.value


def test_rollback_removes_only_journal_owned_resources_in_reverse_order(
    filesystem_service: tuple[FilesystemOperationService, sessionmaker[Session], Path, str],
) -> None:
    service, factory, data_root, task_id = filesystem_service
    source = _prepare(data_root)
    before = source.stat(follow_symlinks=False)
    result = service.execute_hardlink(_request(data_root, task_id))

    hardlink_rollback = service.rollback_journal(result.hardlink_journal_id)
    directory_rollbacks = [
        service.rollback_journal(journal_id)
        for journal_id in reversed(result.directory_journal_ids)
    ]

    assert hardlink_rollback.status is OperationStatus.ROLLED_BACK
    assert all(item.status is OperationStatus.ROLLED_BACK for item in directory_rollbacks)
    assert not (data_root / "target" / "Pack").exists()
    after = source.stat(follow_symlinks=False)
    assert (before.st_ino, before.st_size, before.st_mtime_ns, before.st_nlink) == (
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_nlink,
    )
    assert {item.status for item in _journals(factory)} == {OperationStatus.ROLLED_BACK.value}


def test_rollback_blocks_if_hardlink_target_was_replaced(
    filesystem_service: tuple[FilesystemOperationService, sessionmaker[Session], Path, str],
) -> None:
    service, factory, data_root, task_id = filesystem_service
    source = _prepare(data_root)
    result = service.execute_hardlink(_request(data_root, task_id, target="movie.mkv"))
    target = data_root / "target" / "movie.mkv"
    target.unlink()
    target.write_bytes(b"external-replacement")

    with pytest.raises(DomainViolation) as failure:
        service.rollback_journal(result.hardlink_journal_id)

    assert failure.value.code is ErrorCode.ROLLBACK_BLOCKED
    assert target.read_bytes() == b"external-replacement"
    assert source.read_bytes() == b"synthetic-media-content"
    journals = _journals(factory)
    hardlink = next(item for item in journals if item.id == result.hardlink_journal_id)
    assert hardlink.status == OperationStatus.ROLLBACK_BLOCKED.value


def test_applied_replay_rejects_replaced_source_path(
    filesystem_service: tuple[FilesystemOperationService, sessionmaker[Session], Path, str],
) -> None:
    service, _, data_root, task_id = filesystem_service
    source = _prepare(data_root)
    request = _request(data_root, task_id, target="movie.mkv")
    service.execute_hardlink(request)
    target = data_root / "target" / "movie.mkv"
    original_target_inode = target.stat().st_ino

    source.unlink()
    source.write_bytes(b"replacement-source")

    with pytest.raises(DomainViolation) as failure:
        service.execute_hardlink(request)

    assert failure.value.code is ErrorCode.SOURCE_CHANGED
    assert target.stat().st_ino == original_target_inode
    assert target.read_bytes() == b"synthetic-media-content"
