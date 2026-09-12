from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.downloader_operations import (
    QbittorrentJournalReconcileService,
    QbittorrentWriteBindingPort,
)
from backend.app.application.errors import ApplicationError
from backend.app.application.filesystem_operations import (
    FilesystemOperationService,
    HardlinkExecutionRequest,
)
from backend.app.application.task_actions import TaskActionActor
from backend.app.application.task_operations import TaskOperationService
from backend.app.application.transmission_operations import (
    TransmissionJournalReconcileService,
    TransmissionWriteBindingPort,
)
from backend.app.domain.operation import OperationKind, OperationStatus
from backend.app.domain.verification import FileSnapshot
from backend.app.infrastructure.adapters.downloaders import (
    QbittorrentAddRequest,
    QbittorrentAddResult,
    QbittorrentTorrentState,
    QbittorrentWriteAdapter,
    TransmissionAddRequest,
    TransmissionAddResult,
    TransmissionTorrentState,
    TransmissionWriteAdapter,
)
from backend.app.infrastructure.persistence.base import Base
from backend.app.infrastructure.persistence.database import (
    create_session_factory,
    create_sqlite_engine,
)
from backend.app.infrastructure.persistence.models import (
    OperationJournal,
    TaskActionReceipt,
    TaskEvent,
)
from backend.app.infrastructure.persistence.repositories import (
    OperationIntent,
    OperationJournalRepository,
    TaskCreate,
    TaskRepository,
)
from backend.app.infrastructure.safe_filesystem import SafeFilesystemGateway


class SimulatedCrash(RuntimeError):
    pass


class _NoQbBindings:
    def qbittorrent_write_binding(self, downloader_id: str) -> QbittorrentWriteBindingPort:
        raise AssertionError(f"unexpected qB binding request: {downloader_id}")

    def transmission_write_binding(self, downloader_id: str) -> TransmissionWriteBindingPort:
        raise AssertionError(f"unexpected Transmission binding request: {downloader_id}")


class _ReadOnlyQbittorrent:
    def __init__(self, state: QbittorrentTorrentState) -> None:
        self.state = state
        self.write_calls = 0

    async def get_torrents(
        self, torrent_hashes: tuple[str, ...]
    ) -> tuple[QbittorrentTorrentState, ...]:
        return (self.state,) if self.state.torrent_hash in torrent_hashes else ()

    async def add_torrent(self, request: QbittorrentAddRequest) -> QbittorrentAddResult:
        self.write_calls += 1
        raise AssertionError("reconcile must not add torrent")

    async def stop_torrent(self, torrent_hash: str) -> None:
        self.write_calls += 1
        raise AssertionError("reconcile must not stop torrent")

    async def start_torrent(self, torrent_hash: str) -> None:
        self.write_calls += 1
        raise AssertionError("reconcile must not start torrent")

    async def recheck_torrent(self, torrent_hash: str) -> None:
        self.write_calls += 1
        raise AssertionError("reconcile must not recheck torrent")

    async def remove_torrent_keep_files(self, torrent_hash: str) -> None:
        self.write_calls += 1
        raise AssertionError("reconcile must not remove torrent")


class _ReadOnlyTransmission:
    def __init__(self, state: TransmissionTorrentState | None) -> None:
        self.state = state
        self.write_calls = 0

    async def get_torrents(
        self, torrent_hashes: tuple[str, ...]
    ) -> tuple[TransmissionTorrentState, ...]:
        if self.state is None or self.state.torrent_hash not in torrent_hashes:
            return ()
        return (self.state,)

    async def add_torrent(self, request: TransmissionAddRequest) -> TransmissionAddResult:
        self.write_calls += 1
        raise AssertionError("reconcile must not add Transmission torrent")

    async def stop_torrent(self, torrent_hash: str) -> None:
        self.write_calls += 1
        raise AssertionError("reconcile must not stop Transmission torrent")

    async def start_torrent(self, torrent_hash: str) -> None:
        self.write_calls += 1
        raise AssertionError("reconcile must not start Transmission torrent")

    async def verify_torrent(self, torrent_hash: str) -> None:
        self.write_calls += 1
        raise AssertionError("reconcile must not verify Transmission torrent")

    async def remove_torrent_keep_files(self, torrent_hash: str) -> None:
        self.write_calls += 1
        raise AssertionError("reconcile must not remove Transmission torrent")


@dataclass(frozen=True, slots=True)
class _ReadOnlyBinding:
    adapter: QbittorrentWriteAdapter
    downloader_id: str = "qb-reconcile"
    downloader_version: int = 7
    capabilities: dict[str, object] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.capabilities is None:
            object.__setattr__(self, "capabilities", {})


@dataclass(frozen=True, slots=True)
class _ReadOnlyTransmissionBinding:
    adapter: TransmissionWriteAdapter
    downloader_id: str = "tr-reconcile"
    downloader_version: int = 4
    capabilities: dict[str, object] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.capabilities is None:
            object.__setattr__(self, "capabilities", {})


class _QbBindings:
    def __init__(self, binding: QbittorrentWriteBindingPort) -> None:
        self.binding = binding
        self.requested: list[str] = []

    def qbittorrent_write_binding(self, downloader_id: str) -> QbittorrentWriteBindingPort:
        self.requested.append(downloader_id)
        return self.binding

    def transmission_write_binding(self, downloader_id: str) -> TransmissionWriteBindingPort:
        raise AssertionError(f"unexpected Transmission binding request: {downloader_id}")


class _TransmissionBindings:
    def __init__(self, binding: TransmissionWriteBindingPort) -> None:
        self.binding = binding
        self.requested: list[str] = []

    def qbittorrent_write_binding(self, downloader_id: str) -> QbittorrentWriteBindingPort:
        raise AssertionError(f"unexpected qB binding request: {downloader_id}")

    def transmission_write_binding(self, downloader_id: str) -> TransmissionWriteBindingPort:
        self.requested.append(downloader_id)
        return self.binding


@dataclass(frozen=True, slots=True)
class _OperationFixture:
    service: TaskOperationService
    filesystem: FilesystemOperationService
    factory: sessionmaker[Session]
    data_root: Path
    source: Path
    target: Path
    task_id: str
    journal_id: str


@pytest.fixture
def operation_fixture(tmp_path: Path) -> Iterator[_OperationFixture]:
    data_root = tmp_path / "data"
    source = data_root / "source" / "movie.mkv"
    target_root = data_root / "target"
    source.parent.mkdir(parents=True)
    target_root.mkdir(parents=True)
    source.write_bytes(b"synthetic-media-content")

    engine = create_sqlite_engine(tmp_path / "packbreaker-operation.db")
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

    stat_result = source.stat(follow_symlinks=False)
    filesystem = FilesystemOperationService(factory, SafeFilesystemGateway(data_root))
    created = filesystem.execute_hardlink(
        HardlinkExecutionRequest(
            task_id=task_id,
            candidate_key="c" * 64,
            source_relative_path="source/movie.mkv",
            target_root_relative_path="target",
            target_relative_path="movie.mkv",
            expected_source_snapshot=FileSnapshot(
                device=stat_result.st_dev,
                inode=stat_result.st_ino,
                size=stat_result.st_size,
                mtime_ns=stat_result.st_mtime_ns,
            ),
        )
    )
    with factory() as session:
        OperationJournalRepository(session).transition_status(
            journal_id=created.hardlink_journal_id,
            expected_status=OperationStatus.APPLIED,
            to_status=OperationStatus.RECONCILE_REQUIRED,
        )
        session.commit()

    fixture = _OperationFixture(
        service=TaskOperationService(
            factory,
            filesystem,
            _NoQbBindings(),
            QbittorrentJournalReconcileService(factory),
            TransmissionJournalReconcileService(factory),
        ),
        filesystem=filesystem,
        factory=factory,
        data_root=data_root,
        source=source,
        target=target_root / "movie.mkv",
        task_id=task_id,
        journal_id=created.hardlink_journal_id,
    )
    try:
        yield fixture
    finally:
        engine.dispose()


def test_operation_list_is_redacted_and_marks_only_safe_reconcile(
    operation_fixture: _OperationFixture,
) -> None:
    items = operation_fixture.service.list_operations(operation_fixture.task_id)

    assert len(items) == 1
    item = items[0]
    assert item.id == operation_fixture.journal_id
    assert item.kind is OperationKind.FILESYSTEM_HARDLINK
    assert item.status is OperationStatus.RECONCILE_REQUIRED
    assert item.attention_required is True
    assert item.reconcile_supported is True
    assert set(asdict(item)) == {
        "id",
        "task_id",
        "kind",
        "status",
        "attention_required",
        "reconcile_supported",
        "created_at",
        "updated_at",
    }


@pytest.mark.asyncio
async def test_reconcile_reproves_snapshot_and_replays_same_receipt(
    operation_fixture: _OperationFixture,
) -> None:
    source_before = operation_fixture.source.stat(follow_symlinks=False)
    actor = TaskActionActor("admin_session", "synthetic-actor")

    first = await operation_fixture.service.reconcile(
        task_id=operation_fixture.task_id,
        journal_id=operation_fixture.journal_id,
        actor=actor,
        idempotency_key="reconcile-same-key",
    )
    second = await operation_fixture.service.reconcile(
        task_id=operation_fixture.task_id,
        journal_id=operation_fixture.journal_id,
        actor=actor,
        idempotency_key="reconcile-same-key",
    )

    assert first.status is OperationStatus.APPLIED
    assert first.operation_replayed is False
    assert first.idempotency_replayed is False
    assert second.receipt_id == first.receipt_id
    assert second.status is OperationStatus.APPLIED
    assert second.idempotency_replayed is True
    assert operation_fixture.target.exists()
    source_after = operation_fixture.source.stat(follow_symlinks=False)
    assert (
        source_before.st_ino,
        source_before.st_size,
        source_before.st_mtime_ns,
    ) == (
        source_after.st_ino,
        source_after.st_size,
        source_after.st_mtime_ns,
    )

    with operation_fixture.factory() as session:
        journal = session.get(OperationJournal, operation_fixture.journal_id)
        assert journal is not None and journal.status == OperationStatus.APPLIED.value
        assert session.scalar(select(func.count()).select_from(TaskActionReceipt)) == 1
        events = list(
            session.scalars(
                select(TaskEvent)
                .where(TaskEvent.task_id == operation_fixture.task_id)
                .order_by(TaskEvent.created_at, TaskEvent.id)
            )
        )
        event_types = [event.event_type for event in events]
        assert event_types.count("OPERATION_RECONCILE_REQUESTED") == 1
        assert event_types.count("OPERATION_RECONCILE_CONFIRMED") == 1


@pytest.mark.asyncio
async def test_reconcile_recovers_pending_receipt_after_response_loss(
    operation_fixture: _OperationFixture,
) -> None:
    actor = TaskActionActor("admin_session", "response-loss-actor")

    def crash(checkpoint: str) -> None:
        if checkpoint == "after_operation_reconciled":
            raise SimulatedCrash(checkpoint)

    with pytest.raises(SimulatedCrash):
        await operation_fixture.service.reconcile(
            task_id=operation_fixture.task_id,
            journal_id=operation_fixture.journal_id,
            actor=actor,
            idempotency_key="reconcile-response-loss",
            fault_hook=crash,
        )

    with operation_fixture.factory() as session:
        journal = session.get(OperationJournal, operation_fixture.journal_id)
        assert journal is not None and journal.status == OperationStatus.APPLIED.value
        receipt = session.scalar(select(TaskActionReceipt))
        assert receipt is not None and receipt.state == "PENDING"

    recovered = await operation_fixture.service.reconcile(
        task_id=operation_fixture.task_id,
        journal_id=operation_fixture.journal_id,
        actor=actor,
        idempotency_key="reconcile-response-loss",
    )

    assert recovered.status is OperationStatus.APPLIED
    assert recovered.operation_replayed is True
    assert recovered.idempotency_replayed is True
    with operation_fixture.factory() as session:
        receipt = session.scalar(select(TaskActionReceipt))
        assert receipt is not None and receipt.state == "SUCCEEDED"


@pytest.mark.asyncio
async def test_reconcile_fails_closed_if_target_no_longer_matches_snapshot(
    operation_fixture: _OperationFixture,
) -> None:
    source_before = operation_fixture.source.read_bytes()
    operation_fixture.target.unlink()
    operation_fixture.target.write_bytes(b"external-replacement")
    actor = TaskActionActor("admin_session", "blocked-actor")

    with pytest.raises(ApplicationError) as failure:
        await operation_fixture.service.reconcile(
            task_id=operation_fixture.task_id,
            journal_id=operation_fixture.journal_id,
            actor=actor,
            idempotency_key="reconcile-blocked",
        )

    assert failure.value.code == "OPERATION_RECONCILE_BLOCKED"
    assert operation_fixture.target.read_bytes() == b"external-replacement"
    assert operation_fixture.source.read_bytes() == source_before
    with operation_fixture.factory() as session:
        journal = session.get(OperationJournal, operation_fixture.journal_id)
        assert journal is not None
        assert journal.status == OperationStatus.RECONCILE_REQUIRED.value
        receipt = session.scalar(select(TaskActionReceipt))
        assert receipt is not None and receipt.state == "FAILED"

    with pytest.raises(ApplicationError) as replayed:
        await operation_fixture.service.reconcile(
            task_id=operation_fixture.task_id,
            journal_id=operation_fixture.journal_id,
            actor=actor,
            idempotency_key="reconcile-blocked",
        )
    assert replayed.value.code == "OPERATION_RECONCILE_BLOCKED"


@pytest.mark.asyncio
async def test_task_operation_service_dispatches_safe_qb_reconcile(
    operation_fixture: _OperationFixture,
) -> None:
    torrent_hash = "a" * 40
    ownership_tag = "packbreaker-reconcile"
    save_path = "/downloads/reconcile"
    state = QbittorrentTorrentState(
        torrent_hash=torrent_hash,
        save_path=save_path,
        content_path=None,
        state="stoppedUP",
        tags=(ownership_tag,),
        progress=1.0,
    )
    adapter = _ReadOnlyQbittorrent(state)
    binding = _ReadOnlyBinding(adapter)
    provider = _QbBindings(binding)
    with operation_fixture.factory() as session:
        repository = OperationJournalRepository(session)
        journal, _ = repository.record_intent(
            OperationIntent(
                task_id=operation_fixture.task_id,
                idempotency_key="e" * 64,
                operation_type="QBITTORRENT_ADD",
                target={"downloader_id": binding.downloader_id, "remote_save_path": save_path},
                intent={
                    "schema_version": "packbreaker-qbittorrent-operation-v1",
                    "downloader_version": binding.downloader_version,
                    "remote_save_path": save_path,
                    "ownership_tag": ownership_tag,
                },
                before_snapshot={"torrent_absent": True, "checked_hashes": [torrent_hash]},
            )
        )
        repository.transition_status(
            journal_id=journal.id,
            expected_status=OperationStatus.INTENT_RECORDED,
            to_status=OperationStatus.APPLIED,
            after_snapshot={
                "torrent_hash": torrent_hash,
                "save_path": save_path,
                "content_path": None,
                "state": "stoppedUP",
                "progress": 1.0,
                "ownership_tag": ownership_tag,
                "tags": [ownership_tag],
            },
        )
        repository.transition_status(
            journal_id=journal.id,
            expected_status=OperationStatus.APPLIED,
            to_status=OperationStatus.RECONCILE_REQUIRED,
        )
        session.commit()
        journal_id = journal.id

    service = TaskOperationService(
        operation_fixture.factory,
        operation_fixture.filesystem,
        provider,
        QbittorrentJournalReconcileService(operation_fixture.factory),
        TransmissionJournalReconcileService(operation_fixture.factory),
    )
    summary = next(
        item for item in service.list_operations(operation_fixture.task_id) if item.id == journal_id
    )
    assert summary.kind is OperationKind.QBITTORRENT_ADD
    assert summary.reconcile_supported is True

    actor = TaskActionActor("admin_session", "qb-safe-reconcile")

    def crash(checkpoint: str) -> None:
        if checkpoint == "after_operation_reconciled":
            raise SimulatedCrash(checkpoint)

    with pytest.raises(SimulatedCrash):
        await service.reconcile(
            task_id=operation_fixture.task_id,
            journal_id=journal_id,
            actor=actor,
            idempotency_key="qb-safe-reconcile-key",
            fault_hook=crash,
        )

    with operation_fixture.factory() as session:
        restored = session.get(OperationJournal, journal_id)
        assert restored is not None and restored.status == OperationStatus.APPLIED.value
        receipt = session.scalar(
            select(TaskActionReceipt).where(TaskActionReceipt.task_id == operation_fixture.task_id)
        )
        assert receipt is not None and receipt.state == "PENDING"

    result = await service.reconcile(
        task_id=operation_fixture.task_id,
        journal_id=journal_id,
        actor=actor,
        idempotency_key="qb-safe-reconcile-key",
    )

    assert result.status is OperationStatus.APPLIED
    assert result.kind is OperationKind.QBITTORRENT_ADD
    assert result.operation_replayed is True
    assert result.idempotency_replayed is True
    assert provider.requested == [binding.downloader_id, binding.downloader_id]
    assert adapter.write_calls == 0
    with operation_fixture.factory() as session:
        receipt = session.scalar(
            select(TaskActionReceipt).where(TaskActionReceipt.task_id == operation_fixture.task_id)
        )
        assert receipt is not None and receipt.state == "SUCCEEDED"


@pytest.mark.asyncio
async def test_qb_unknown_result_without_after_snapshot_stays_manual_only(
    operation_fixture: _OperationFixture,
) -> None:
    with operation_fixture.factory() as session:
        repository = OperationJournalRepository(session)
        journal, _ = repository.record_intent(
            OperationIntent(
                task_id=operation_fixture.task_id,
                idempotency_key="d" * 64,
                operation_type="QBITTORRENT_ADD",
                target={"downloader_id": "private-downloader"},
                intent={"ownership_tag": "private-tag"},
            )
        )
        repository.transition_status(
            journal_id=journal.id,
            expected_status=OperationStatus.INTENT_RECORDED,
            to_status=OperationStatus.RECONCILE_REQUIRED,
        )
        session.commit()
        qbit_journal_id = journal.id

    summary = next(
        item
        for item in operation_fixture.service.list_operations(operation_fixture.task_id)
        if item.id == qbit_journal_id
    )
    assert summary.kind is OperationKind.QBITTORRENT_ADD
    assert summary.attention_required is True
    assert summary.reconcile_supported is False

    with pytest.raises(ApplicationError) as failure:
        await operation_fixture.service.reconcile(
            task_id=operation_fixture.task_id,
            journal_id=qbit_journal_id,
            actor=TaskActionActor("admin_session", "qb-actor"),
            idempotency_key="reconcile-qb-unsupported",
        )
    assert failure.value.code == "OPERATION_RECONCILE_UNPROVABLE"


@pytest.mark.asyncio
async def test_task_operation_service_dispatches_safe_transmission_reconcile(
    operation_fixture: _OperationFixture,
) -> None:
    torrent_hash = "b" * 40
    ownership_tag = "packbreaker-tr-reconcile"
    save_path = "/downloads/reconcile"
    state = TransmissionTorrentState(
        torrent_hash=torrent_hash,
        download_dir=save_path,
        status=0,
        labels=(ownership_tag,),
        percent_done=1.0,
        recheck_progress=0.0,
    )
    adapter = _ReadOnlyTransmission(state)
    binding = _ReadOnlyTransmissionBinding(adapter)
    provider = _TransmissionBindings(binding)
    with operation_fixture.factory() as session:
        repository = OperationJournalRepository(session)
        journal, _ = repository.record_intent(
            OperationIntent(
                task_id=operation_fixture.task_id,
                idempotency_key="f" * 64,
                operation_type="TRANSMISSION_ADD",
                target={"downloader_id": binding.downloader_id, "remote_save_path": save_path},
                intent={
                    "schema_version": "packbreaker-transmission-add-v1",
                    "downloader_version": binding.downloader_version,
                    "execution_plan_id": "plan-tr-reconcile",
                    "execution_plan_digest": "1" * 64,
                    "expected_metainfo_digest": "2" * 64,
                    "torrent_payload_digest": "3" * 64,
                    "expected_hashes": [torrent_hash],
                    "remote_save_path": save_path,
                    "paused": True,
                    "skip_checking": False,
                    "ownership_tag": ownership_tag,
                },
                before_snapshot={"torrent_absent": True, "checked_hashes": [torrent_hash]},
            )
        )
        repository.transition_status(
            journal_id=journal.id,
            expected_status=OperationStatus.INTENT_RECORDED,
            to_status=OperationStatus.APPLIED,
            after_snapshot={
                "torrent_hash": torrent_hash,
                "save_path": save_path,
                "state": "0",
                "status": 0,
                "progress": 1.0,
                "recheck_progress": 0.0,
                "checking": False,
                "ownership_tag": ownership_tag,
                "labels": [ownership_tag],
            },
        )
        repository.transition_status(
            journal_id=journal.id,
            expected_status=OperationStatus.APPLIED,
            to_status=OperationStatus.RECONCILE_REQUIRED,
        )
        session.commit()
        journal_id = journal.id

    service = TaskOperationService(
        operation_fixture.factory,
        operation_fixture.filesystem,
        provider,
        QbittorrentJournalReconcileService(operation_fixture.factory),
        TransmissionJournalReconcileService(operation_fixture.factory),
    )
    summary = next(
        item for item in service.list_operations(operation_fixture.task_id) if item.id == journal_id
    )
    assert summary.kind is OperationKind.TRANSMISSION_ADD
    assert summary.reconcile_supported is True

    result = await service.reconcile(
        task_id=operation_fixture.task_id,
        journal_id=journal_id,
        actor=TaskActionActor("admin_session", "tr-safe-reconcile"),
        idempotency_key="tr-safe-reconcile-key",
    )

    assert result.status is OperationStatus.APPLIED
    assert result.kind is OperationKind.TRANSMISSION_ADD
    assert result.operation_replayed is False
    assert provider.requested == [binding.downloader_id]
    assert adapter.write_calls == 0


def test_maintenance_report_is_redacted_and_separates_repair_from_retention_candidates(
    operation_fixture: _OperationFixture,
) -> None:
    secret_marker = "/data/private/do-not-leak-marker"
    with operation_fixture.factory() as session:
        repository = OperationJournalRepository(session)

        manual, _ = repository.record_intent(
            OperationIntent(
                task_id=operation_fixture.task_id,
                idempotency_key="1" * 64,
                operation_type="UNKNOWN_SIDE_EFFECT",
                target={"path": secret_marker},
                intent={"ownership_tag": "private-owner", "secret": secret_marker},
                before_snapshot={"absolute_path": secret_marker},
            )
        )
        repository.transition_status(
            journal_id=manual.id,
            expected_status=OperationStatus.INTENT_RECORDED,
            to_status=OperationStatus.RECONCILE_REQUIRED,
        )

        blocked, _ = repository.record_intent(
            OperationIntent(
                task_id=operation_fixture.task_id,
                idempotency_key="2" * 64,
                operation_type="CREATE_DIRECTORY",
                target={"target_relative_path": secret_marker},
                intent={"private": secret_marker},
                before_snapshot={"path": secret_marker},
            )
        )
        repository.transition_status(
            journal_id=blocked.id,
            expected_status=OperationStatus.INTENT_RECORDED,
            to_status=OperationStatus.APPLIED,
            after_snapshot={"path": secret_marker},
        )
        repository.transition_status(
            journal_id=blocked.id,
            expected_status=OperationStatus.APPLIED,
            to_status=OperationStatus.ROLLBACK_PENDING,
        )
        repository.transition_status(
            journal_id=blocked.id,
            expected_status=OperationStatus.ROLLBACK_PENDING,
            to_status=OperationStatus.ROLLBACK_BLOCKED,
        )

        noop, _ = repository.record_intent(
            OperationIntent(
                task_id=operation_fixture.task_id,
                idempotency_key="3" * 64,
                operation_type="CREATE_DIRECTORY",
                target={"path": secret_marker},
                intent={"private": secret_marker},
            )
        )
        repository.transition_status(
            journal_id=noop.id,
            expected_status=OperationStatus.INTENT_RECORDED,
            to_status=OperationStatus.NOOP,
        )

        rolled_back, _ = repository.record_intent(
            OperationIntent(
                task_id=operation_fixture.task_id,
                idempotency_key="4" * 64,
                operation_type="CREATE_HARDLINK",
                target={"path": secret_marker},
                intent={"private": secret_marker},
            )
        )
        repository.transition_status(
            journal_id=rolled_back.id,
            expected_status=OperationStatus.INTENT_RECORDED,
            to_status=OperationStatus.APPLIED,
            after_snapshot={"path": secret_marker},
        )
        repository.transition_status(
            journal_id=rolled_back.id,
            expected_status=OperationStatus.APPLIED,
            to_status=OperationStatus.ROLLBACK_PENDING,
        )
        repository.transition_status(
            journal_id=rolled_back.id,
            expected_status=OperationStatus.ROLLBACK_PENDING,
            to_status=OperationStatus.ROLLED_BACK,
        )
        session.commit()

    report = operation_fixture.service.maintenance_report(limit=10)

    assert report.summary.total_journals == 5
    assert report.summary.attention_required == 3
    assert report.summary.reconcile_supported == 1
    assert report.summary.manual_only == 2
    assert report.summary.retention_candidates == 2
    assert report.summary.truncated is False

    repairs = {item.journal_id: item for item in report.repair_items}
    assert repairs[operation_fixture.journal_id].action == "RECONCILE"
    assert repairs[operation_fixture.journal_id].manual_required is False
    assert repairs[manual.id].reason_code == "MANUAL_RECONCILE_REQUIRED"
    assert repairs[manual.id].manual_required is True
    assert repairs[blocked.id].reason_code == "ROLLBACK_BLOCKED"
    assert repairs[blocked.id].action == "MANUAL_INSPECTION"

    cleanup = {item.journal_id: item for item in report.cleanup_candidates}
    assert cleanup[noop.id].reason_code == "NO_SIDE_EFFECT"
    assert cleanup[rolled_back.id].reason_code == "ROLLBACK_CONFIRMED"
    assert all(
        "当前 API 不删除 operation journal" in item.recommendation for item in cleanup.values()
    )

    encoded = json.dumps(asdict(report), ensure_ascii=False, default=str)
    assert secret_marker not in encoded
    assert "private-owner" not in encoded

    limited = operation_fixture.service.maintenance_report(limit=1)
    assert limited.summary.attention_required == 3
    assert limited.summary.retention_candidates == 2
    assert limited.summary.truncated is True
    assert len(limited.repair_items) == 1
    assert len(limited.cleanup_candidates) == 1
