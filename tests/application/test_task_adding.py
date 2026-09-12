from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.downloader_operations import (
    QbittorrentAddOperationService,
    QbittorrentRecheckOperationService,
    QbittorrentRemoveOperationService,
    QbittorrentStartOperationService,
)
from backend.app.application.downloaders import QbittorrentWriteBinding, TransmissionWriteBinding
from backend.app.application.errors import ApplicationError
from backend.app.application.filesystem_operations import (
    FilesystemOperationService,
    HardlinkExecutionRequest,
)
from backend.app.application.sites import EnabledSiteAdapter
from backend.app.application.task_adding import TaskAddingCoordinator
from backend.app.application.task_cancellation import (
    TaskCancellationCoordinator,
    TaskCancellationRequest,
)
from backend.app.application.task_client_verification import TaskClientVerificationCoordinator
from backend.app.application.task_driver import ActiveTaskDriver
from backend.app.application.task_recovery import RecoveryOutcome, TaskRecoveryCoordinator
from backend.app.application.task_seeding import TaskSeedingCoordinator
from backend.app.application.transmission_operations import (
    TRANSMISSION_ADD_OPERATION,
    TRANSMISSION_REMOVE_OPERATION,
    TRANSMISSION_START_OPERATION,
    TRANSMISSION_VERIFY_OPERATION,
    TransmissionAddOperationService,
    TransmissionRemoveOperationService,
    TransmissionStartOperationService,
    TransmissionVerifyOperationService,
)
from backend.app.domain.downloader import (
    PathMappingRule,
    ProbeStatus,
    downloader_execution_binding_digest,
)
from backend.app.domain.errors import DomainViolation
from backend.app.domain.execution_plan import (
    ExecutionPlanAction,
    ExecutionPlanActionKind,
    ExecutionPlanSnapshot,
)
from backend.app.domain.operation import OperationStatus
from backend.app.domain.site_adapter import SiteConnectionResult, TorrentDetails, TorrentPayload
from backend.app.domain.site_search import (
    SearchPage,
    SearchQuery,
    SiteSearchCapabilities,
    normalize_candidate_meta,
)
from backend.app.domain.task_state import TaskStatus
from backend.app.domain.verification import DownloaderKind, FileSnapshot, VerificationLevel
from backend.app.infrastructure.adapters.downloaders import (
    DownloaderAdapterError,
    QbittorrentAddRequest,
    QbittorrentAddResult,
    QbittorrentTorrentState,
    TransmissionAddRequest,
    TransmissionAddResult,
    TransmissionTorrentState,
)
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
from backend.app.infrastructure.torrent_parser import parse_torrent


class SimulatedCrash(RuntimeError):
    pass


class _FakeSiteAdapter:
    def __init__(self, torrent_content: bytes) -> None:
        self.torrent_content = torrent_content

    async def capabilities(self) -> SiteSearchCapabilities:
        return SiteSearchCapabilities()

    async def test_connection(self) -> SiteConnectionResult:
        return SiteConnectionResult("fake")

    async def search(self, query: SearchQuery) -> SearchPage:
        return SearchPage("fake", query.page, (), False, 0)

    async def fetch_details(self, torrent_id: str) -> TorrentDetails:
        return TorrentDetails(
            normalize_candidate_meta(
                site_id="fake",
                torrent_id=torrent_id,
                display_name="Synthetic Movie",
                total_size=16,
            )
        )

    async def fetch_torrent(self, torrent_id: str) -> TorrentPayload:
        return TorrentPayload("fake", torrent_id, self.torrent_content, datetime.now(UTC))


class _FakeSiteProvider:
    def __init__(self, adapter: _FakeSiteAdapter) -> None:
        self.adapter = adapter

    def enabled_adapters(self) -> tuple[EnabledSiteAdapter, ...]:
        return (EnabledSiteAdapter("site-config", 1, "fake", self.adapter),)

    def enabled_site_versions(self) -> tuple[tuple[str, int], ...]:
        return (("site-config", 1),)


class _FakeQbittorrent:
    def __init__(self) -> None:
        self.states: dict[str, QbittorrentTorrentState] = {}
        self.add_calls = 0
        self.recheck_calls = 0
        self.raise_after_apply_once = False
        self.raise_after_recheck_apply_once = False
        self.apply_on_recheck = True
        self.start_calls = 0
        self.raise_after_start_apply_once = False
        self.apply_on_start = True
        self.remove_calls = 0
        self.raise_after_remove_apply_once = False

    async def add_torrent(self, request: QbittorrentAddRequest) -> QbittorrentAddResult:
        self.add_calls += 1
        meta = parse_torrent(request.torrent_content)
        torrent_hash = meta.v1_info_hash or meta.v2_info_hash
        assert torrent_hash is not None
        self.states[torrent_hash] = QbittorrentTorrentState(
            torrent_hash=torrent_hash,
            save_path=request.save_path,
            content_path=None,
            state="stoppedUP",
            tags=request.tags,
            progress=1.0,
        )
        if self.raise_after_apply_once:
            self.raise_after_apply_once = False
            raise DownloaderAdapterError("DOWNLOADER_UNAVAILABLE", "synthetic response lost")
        return QbittorrentAddResult(1, 0, 0, (torrent_hash,), "2.15.1")

    async def get_torrents(
        self, torrent_hashes: tuple[str, ...]
    ) -> tuple[QbittorrentTorrentState, ...]:
        return tuple(self.states[item] for item in torrent_hashes if item in self.states)

    async def stop_torrent(self, torrent_hash: str) -> None:
        state = self.states[torrent_hash]
        self.states[torrent_hash] = replace(state, state="stoppedDL")

    async def start_torrent(self, torrent_hash: str) -> None:
        self.start_calls += 1
        if self.apply_on_start:
            self.states[torrent_hash] = replace(
                self.states[torrent_hash],
                state="stalledUP",
            )
        if self.raise_after_start_apply_once:
            self.raise_after_start_apply_once = False
            raise DownloaderAdapterError("DOWNLOADER_UNAVAILABLE", "synthetic start response lost")

    async def recheck_torrent(self, torrent_hash: str) -> None:
        self.recheck_calls += 1
        if self.apply_on_recheck:
            self.states[torrent_hash] = replace(
                self.states[torrent_hash],
                state="checkingUP",
                progress=0.0,
            )
        if self.raise_after_recheck_apply_once:
            self.raise_after_recheck_apply_once = False
            raise DownloaderAdapterError(
                "DOWNLOADER_UNAVAILABLE", "synthetic recheck response lost"
            )

    async def remove_torrent_keep_files(self, torrent_hash: str) -> None:
        self.remove_calls += 1
        self.states.pop(torrent_hash, None)
        if self.raise_after_remove_apply_once:
            self.raise_after_remove_apply_once = False
            raise DownloaderAdapterError("DOWNLOADER_UNAVAILABLE", "synthetic remove response lost")


class _FakeTransmission:
    def __init__(self) -> None:
        self.states: dict[str, TransmissionTorrentState] = {}
        self.add_calls = 0
        self.verify_calls = 0
        self.start_calls = 0
        self.remove_calls = 0
        self.raise_after_start_apply_once = False

    async def add_torrent(self, request: TransmissionAddRequest) -> TransmissionAddResult:
        self.add_calls += 1
        meta = parse_torrent(request.torrent_content)
        torrent_hash = meta.v1_info_hash or meta.v2_info_hash
        assert torrent_hash is not None
        if torrent_hash in self.states:
            return TransmissionAddResult(torrent_hash=torrent_hash, duplicate=True)
        self.states[torrent_hash] = TransmissionTorrentState(
            torrent_hash=torrent_hash,
            download_dir=request.save_path,
            status=0,
            labels=request.labels,
            percent_done=1.0,
            recheck_progress=0.0,
        )
        return TransmissionAddResult(torrent_hash=torrent_hash, duplicate=False)

    async def get_torrents(
        self, torrent_hashes: tuple[str, ...]
    ) -> tuple[TransmissionTorrentState, ...]:
        return tuple(self.states[item] for item in torrent_hashes if item in self.states)

    async def stop_torrent(self, torrent_hash: str) -> None:
        self.states[torrent_hash] = replace(self.states[torrent_hash], status=0)

    async def start_torrent(self, torrent_hash: str) -> None:
        self.start_calls += 1
        self.states[torrent_hash] = replace(self.states[torrent_hash], status=5)
        if self.raise_after_start_apply_once:
            self.raise_after_start_apply_once = False
            raise DownloaderAdapterError("DOWNLOADER_UNAVAILABLE", "synthetic start response lost")

    async def verify_torrent(self, torrent_hash: str) -> None:
        self.verify_calls += 1
        self.states[torrent_hash] = replace(
            self.states[torrent_hash],
            status=2,
            recheck_progress=0.1,
        )

    async def remove_torrent_keep_files(self, torrent_hash: str) -> None:
        self.remove_calls += 1
        self.states.pop(torrent_hash, None)


@dataclass
class _FakeDownloaderProvider:
    binding: QbittorrentWriteBinding | TransmissionWriteBinding

    def write_binding(
        self, downloader_id: str
    ) -> QbittorrentWriteBinding | TransmissionWriteBinding:
        assert downloader_id == self.binding.downloader_id
        return self.binding

    def qbittorrent_write_binding(self, downloader_id: str) -> QbittorrentWriteBinding:
        assert downloader_id == self.binding.downloader_id
        assert isinstance(self.binding, QbittorrentWriteBinding)
        return self.binding


@dataclass(frozen=True, slots=True)
class _AddingFixture:
    coordinator: TaskAddingCoordinator
    verifier: TaskClientVerificationCoordinator
    seeder: TaskSeedingCoordinator
    factory: sessionmaker[Session]
    data_root: Path
    source_file: Path
    task_id: str
    unit_id: str
    plan_id: str
    qbit: _FakeQbittorrent
    site_adapter: _FakeSiteAdapter
    downloader_provider: _FakeDownloaderProvider
    binding_digest: str
    torrent_content: bytes


@pytest.fixture
def adding_fixture(tmp_path: Path) -> _AddingFixture:
    data_root = tmp_path / "data"
    source_root = data_root / "source"
    target_root = data_root / "target"
    source_root.mkdir(parents=True)
    target_root.mkdir()
    content = b"0123456789abcdef"
    source_file = source_root / "Movie.2026.mkv"
    source_file.write_bytes(content)
    os.link(source_file, target_root / "Movie.2026.mkv")
    inventory = scan_source_inventory(source_root)
    inventory_digest = source_inventory_digest(inventory)
    source = inventory[0]
    torrent_content = _v1_torrent(source_file.name.encode(), content, piece_length=4)
    meta = parse_torrent(torrent_content)

    downloader_id = "target-qb"
    mappings = (PathMappingRule("/downloads", str(data_root)),)
    capabilities = {
        "client": "qBittorrent",
        "version": "v5.2.3",
        "api_version": "2.15.1",
        "supports_skip_checking": True,
        "supports_force_recheck": True,
        "supports_verify_progress": True,
    }
    binding_digest = downloader_execution_binding_digest(
        downloader_id=downloader_id,
        version=3,
        kind=DownloaderKind.QBITTORRENT,
        enabled=True,
        connection_status=ProbeStatus.OK,
        path_mapping_status=ProbeStatus.OK,
        path_mappings=mappings,
        capabilities=capabilities,
    )
    qbit = _FakeQbittorrent()
    binding = QbittorrentWriteBinding(
        downloader_id=downloader_id,
        downloader_version=3,
        binding_digest=binding_digest,
        path_mappings=mappings,
        capabilities=capabilities,
        adapter=qbit,
        data_root=data_root,
    )
    downloader_provider = _FakeDownloaderProvider(binding)
    site_adapter = _FakeSiteAdapter(torrent_content)

    engine = create_sqlite_engine(tmp_path / "adding.db")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    task_id = new_uuid()
    unit_id = new_uuid()
    preflight_id = new_uuid()
    review_id = new_uuid()
    candidate_id = new_uuid()
    gate_id = new_uuid()
    now = utc_now()
    plan_task_version = 7
    with factory() as session:
        session.add(
            UnpackTask(
                id=task_id,
                type="PACKAGE_UNPACK",
                source_downloader_id="source-downloader",
                source_hash="source-hash",
                normalized_unit_key="synthetic-movie",
                idempotency_key="a" * 64,
                status=TaskStatus.ADDING.value,
                trace_id=new_uuid(),
                checkpoint={},
                error_code=None,
                version=plan_task_version + 2,
                created_at=now,
                updated_at=now,
            )
        )
        session.flush()
        session.add(
            PreflightSnapshotRecord(
                id=preflight_id,
                task_id=task_id,
                task_version=plan_task_version,
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
                source_relative_path=source_file.name,
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
                site_id="fake",
                torrent_id="42",
                display_name="Synthetic Movie",
                score=100.0,
                rejected=False,
                selected_for_verification=True,
                verification_level=VerificationLevel.FULL_VERIFIED.value,
                metainfo_digest=meta.metainfo_digest,
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
                task_version=plan_task_version,
                eligible=True,
                client_check_required=False,
                verification_level=VerificationLevel.FULL_VERIFIED.value,
                metainfo_digest=meta.metainfo_digest,
                blocked_reasons=[],
                gate_digest="c" * 64,
                payload={"review_version": 1},
                created_at=now,
            )
        )
        session.flush()
        snapshot = ExecutionPlanSnapshot(
            task_id=task_id,
            task_version=plan_task_version,
            task_unit_id=unit_id,
            execution_gate_id=gate_id,
            execution_gate_digest="c" * 64,
            preflight_snapshot_id=preflight_id,
            review_revision_id=review_id,
            candidate_id=candidate_id,
            source_inventory_digest=inventory_digest,
            metainfo_digest=meta.metainfo_digest,
            verification_level=VerificationLevel.FULL_VERIFIED,
            client_check_required=False,
            source_root="source",
            target_root="target",
            target_device=target_root.stat().st_dev,
            target_downloader_id=downloader_id,
            target_downloader_version=3,
            target_downloader_binding_digest=binding_digest,
            target_remote_save_path="/downloads/target",
            actions=(
                ExecutionPlanAction(
                    torrent_path=source_file.name,
                    kind=ExecutionPlanActionKind.HARDLINK,
                    length=source.length,
                    source_relative_path=source_file.name,
                    source_snapshot=source.snapshot,
                ),
            ),
            create_directories=(),
            estimated_download_bytes_upper_bound=0,
            blocked_reasons=(),
            created_at=now,
        )
        plan, _ = TaskExecutionPlanRepository(session).create_or_get(snapshot)
        task = session.get(UnpackTask, task_id)
        assert task is not None
        task.checkpoint = _adding_checkpoint(
            plan.id,
            plan.plan_digest,
            gate_id,
            downloader_id,
            binding_digest,
        )
        session.commit()
        plan_id = plan.id

    coordinator = TaskAddingCoordinator(
        factory,
        _FakeSiteProvider(site_adapter),
        downloader_provider,
        QbittorrentAddOperationService(factory),
        data_root=data_root,
    )
    verifier = TaskClientVerificationCoordinator(
        factory,
        downloader_provider,
        QbittorrentRecheckOperationService(factory),
        data_root=data_root,
    )
    seeder = TaskSeedingCoordinator(
        factory,
        downloader_provider,
        QbittorrentStartOperationService(factory),
        data_root=data_root,
    )
    return _AddingFixture(
        coordinator=coordinator,
        verifier=verifier,
        seeder=seeder,
        factory=factory,
        data_root=data_root,
        source_file=source_file,
        task_id=task_id,
        unit_id=unit_id,
        plan_id=plan_id,
        qbit=qbit,
        site_adapter=site_adapter,
        downloader_provider=downloader_provider,
        binding_digest=binding_digest,
        torrent_content=torrent_content,
    )


@pytest.mark.asyncio
async def test_full_verified_add_transitions_to_seeding_and_replays_once(
    adding_fixture: _AddingFixture,
) -> None:
    first = await adding_fixture.coordinator.execute(
        adding_fixture.unit_id,
        execution_plan_id=adding_fixture.plan_id,
    )

    assert first.status is TaskStatus.SEEDING
    assert first.skip_checking is True
    assert adding_fixture.qbit.add_calls == 1
    repeated = await adding_fixture.coordinator.execute(
        adding_fixture.unit_id,
        execution_plan_id=adding_fixture.plan_id,
    )
    assert repeated.replayed is True
    assert repeated.qbit_journal_id == first.qbit_journal_id
    assert adding_fixture.qbit.add_calls == 1
    with adding_fixture.factory() as session:
        task = session.get(UnpackTask, adding_fixture.task_id)
        assert task is not None
        assert task.status == TaskStatus.SEEDING.value
        assert task.version == 10
        assert session.scalar(select(func.count()).select_from(OperationJournal)) == 1


@pytest.mark.asyncio
async def test_client_check_required_transitions_to_client_verifying_without_skip(
    adding_fixture: _AddingFixture,
) -> None:
    _set_verification_level(
        adding_fixture,
        VerificationLevel.CLIENT_CHECK_REQUIRED,
        client_check_required=True,
    )

    result = await adding_fixture.coordinator.execute(
        adding_fixture.unit_id,
        execution_plan_id=adding_fixture.plan_id,
    )

    assert result.status is TaskStatus.CLIENT_VERIFYING
    assert result.skip_checking is False


@pytest.mark.asyncio
async def test_transmission_full_verified_verifies_then_recovery_starts_seeding_to_done(
    adding_fixture: _AddingFixture,
) -> None:
    current_binding = adding_fixture.downloader_provider.binding
    assert isinstance(current_binding, QbittorrentWriteBinding)
    capabilities = {
        "client": "Transmission",
        "version": "4.1.3",
        "api_version": "6.0.0",
        "supports_skip_checking": False,
        "supports_force_recheck": True,
        "supports_verify_progress": True,
    }
    binding_digest = downloader_execution_binding_digest(
        downloader_id=current_binding.downloader_id,
        version=current_binding.downloader_version,
        kind=DownloaderKind.TRANSMISSION,
        enabled=True,
        connection_status=ProbeStatus.OK,
        path_mapping_status=ProbeStatus.OK,
        path_mappings=current_binding.path_mappings,
        capabilities=capabilities,
    )
    transmission = _FakeTransmission()
    adding_fixture.downloader_provider.binding = TransmissionWriteBinding(
        downloader_id=current_binding.downloader_id,
        downloader_version=current_binding.downloader_version,
        binding_digest=binding_digest,
        path_mappings=current_binding.path_mappings,
        capabilities=capabilities,
        adapter=transmission,
        data_root=adding_fixture.data_root,
    )
    with adding_fixture.factory() as session:
        plan = TaskExecutionPlanRepository(session).get(adding_fixture.plan_id)
        task = session.get(UnpackTask, adding_fixture.task_id)
        assert plan is not None and task is not None
        plan_payload = dict(plan.payload)
        plan_payload["target_downloader_binding_digest"] = binding_digest
        plan.payload = plan_payload
        checkpoint = dict(task.checkpoint)
        checkpoint["target_downloader_binding_digest"] = binding_digest
        task.checkpoint = checkpoint
        session.commit()

    adding = TaskAddingCoordinator(
        adding_fixture.factory,
        _FakeSiteProvider(adding_fixture.site_adapter),
        adding_fixture.downloader_provider,
        QbittorrentAddOperationService(adding_fixture.factory),
        TransmissionAddOperationService(adding_fixture.factory),
        data_root=adding_fixture.data_root,
    )
    verifier = TaskClientVerificationCoordinator(
        adding_fixture.factory,
        adding_fixture.downloader_provider,
        QbittorrentRecheckOperationService(adding_fixture.factory),
        TransmissionVerifyOperationService(adding_fixture.factory),
        data_root=adding_fixture.data_root,
    )

    added = await adding.execute(
        adding_fixture.unit_id,
        execution_plan_id=adding_fixture.plan_id,
    )

    assert added.status is TaskStatus.CLIENT_VERIFYING
    assert added.skip_checking is False
    assert transmission.add_calls == 1
    with adding_fixture.factory() as session:
        task = session.get(UnpackTask, adding_fixture.task_id)
        assert task is not None
        assert task.checkpoint["downloader_kind"] == DownloaderKind.TRANSMISSION.value
        add_journal = session.get(OperationJournal, added.qbit_journal_id)
        assert add_journal is not None
        assert add_journal.operation_type == TRANSMISSION_ADD_OPERATION

    checking = await verifier.execute(
        adding_fixture.unit_id,
        execution_plan_id=adding_fixture.plan_id,
    )
    assert checking.status is TaskStatus.CLIENT_VERIFYING
    assert checking.verification_outcome == "CHECKING"
    assert transmission.verify_calls == 1

    transmission.states[added.torrent_hash] = replace(
        transmission.states[added.torrent_hash],
        status=0,
        percent_done=1.0,
        recheck_progress=1.0,
    )
    verified = await verifier.execute(
        adding_fixture.unit_id,
        execution_plan_id=adding_fixture.plan_id,
    )
    assert verified.status is TaskStatus.SEEDING
    assert verified.verification_outcome == "VERIFIED"
    assert transmission.verify_calls == 1
    with adding_fixture.factory() as session:
        verify_journal = session.get(OperationJournal, verified.recheck_journal_id)
        assert verify_journal is not None
        assert verify_journal.operation_type == TRANSMISSION_VERIFY_OPERATION

    seeder = TaskSeedingCoordinator(
        adding_fixture.factory,
        adding_fixture.downloader_provider,
        QbittorrentStartOperationService(adding_fixture.factory),
        TransmissionStartOperationService(adding_fixture.factory),
        data_root=adding_fixture.data_root,
    )

    class _NeverLinking:
        def execute(self, unit_id: str, *, execution_plan_id: str) -> object:
            raise AssertionError(f"unexpected linking recovery: {unit_id}/{execution_plan_id}")

    class _NeverAsync:
        def __init__(self) -> None:
            self.calls = 0

        async def execute(self, unit_id: str, *, execution_plan_id: str) -> object:
            self.calls += 1
            raise AssertionError(f"unexpected async recovery: {unit_id}/{execution_plan_id}")

    recovery = TaskRecoveryCoordinator(
        adding_fixture.factory,
        _NeverLinking(),
        _NeverAsync(),
        _NeverAsync(),
        seeder,
    )
    recovery_result = await recovery.reconcile_task(adding_fixture.task_id)

    assert recovery_result.outcome is RecoveryOutcome.COMPLETED
    assert recovery_result.final_status is TaskStatus.DONE
    assert transmission.start_calls == 1
    assert transmission.states[added.torrent_hash].status == 5
    replayed = await seeder.execute(
        adding_fixture.unit_id,
        execution_plan_id=adding_fixture.plan_id,
    )
    assert replayed.status is TaskStatus.DONE
    assert replayed.replayed is True
    assert transmission.start_calls == 1
    with adding_fixture.factory() as session:
        journals = list(
            session.scalars(select(OperationJournal).order_by(OperationJournal.created_at))
        )
        assert [item.operation_type for item in journals] == [
            TRANSMISSION_ADD_OPERATION,
            TRANSMISSION_VERIFY_OPERATION,
            TRANSMISSION_START_OPERATION,
        ]
        assert all(item.status == OperationStatus.APPLIED.value for item in journals)


@pytest.mark.asyncio
async def test_downloader_binding_change_blocks_before_qb_write(
    adding_fixture: _AddingFixture,
) -> None:
    adding_fixture.downloader_provider.binding = replace(
        adding_fixture.downloader_provider.binding,
        binding_digest="f" * 64,
    )

    with pytest.raises(ApplicationError) as failure:
        await adding_fixture.coordinator.execute(
            adding_fixture.unit_id,
            execution_plan_id=adding_fixture.plan_id,
        )

    assert failure.value.code == "ADDING_DOWNLOADER_CHANGED"
    assert adding_fixture.qbit.add_calls == 0


@pytest.mark.asyncio
async def test_source_inventory_change_blocks_before_qb_write(
    adding_fixture: _AddingFixture,
) -> None:
    (adding_fixture.source_file.parent / "new.nfo").write_text("changed")

    with pytest.raises(ApplicationError) as failure:
        await adding_fixture.coordinator.execute(
            adding_fixture.unit_id,
            execution_plan_id=adding_fixture.plan_id,
        )

    assert failure.value.code == "ADDING_SOURCE_CHANGED"
    assert adding_fixture.qbit.add_calls == 0


@pytest.mark.asyncio
async def test_target_root_symlink_replacement_blocks_before_qb_write(
    adding_fixture: _AddingFixture,
) -> None:
    target_root = adding_fixture.data_root / "target"
    (target_root / "Movie.2026.mkv").unlink()
    target_root.rmdir()
    outside = adding_fixture.data_root.parent / "outside-target"
    outside.mkdir()
    target_root.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ApplicationError) as failure:
        await adding_fixture.coordinator.execute(
            adding_fixture.unit_id,
            execution_plan_id=adding_fixture.plan_id,
        )

    assert failure.value.code == "ADDING_TARGET_CHANGED"
    assert adding_fixture.qbit.add_calls == 0


@pytest.mark.asyncio
async def test_torrent_change_blocks_before_qb_write(adding_fixture: _AddingFixture) -> None:
    adding_fixture.site_adapter.torrent_content = _v1_torrent(
        b"Movie.2026.mkv",
        b"fedcba9876543210",
        piece_length=4,
    )

    with pytest.raises(ApplicationError) as failure:
        await adding_fixture.coordinator.execute(
            adding_fixture.unit_id,
            execution_plan_id=adding_fixture.plan_id,
        )

    assert failure.value.code == "ADDING_TORRENT_CHANGED"
    assert adding_fixture.qbit.add_calls == 0


@pytest.mark.asyncio
async def test_qb_response_lost_recovers_without_second_add(adding_fixture: _AddingFixture) -> None:
    adding_fixture.qbit.raise_after_apply_once = True
    with pytest.raises(ApplicationError) as failure:
        await adding_fixture.coordinator.execute(
            adding_fixture.unit_id,
            execution_plan_id=adding_fixture.plan_id,
        )
    assert failure.value.code == "DOWNLOADER_UNAVAILABLE"
    assert adding_fixture.qbit.add_calls == 1

    recovered = await adding_fixture.coordinator.execute(
        adding_fixture.unit_id,
        execution_plan_id=adding_fixture.plan_id,
    )
    assert recovered.status is TaskStatus.SEEDING
    assert recovered.recovered_after_unknown_result is True
    assert adding_fixture.qbit.add_calls == 1


@pytest.mark.asyncio
async def test_crash_after_qb_applied_only_finishes_task_transition_on_replay(
    adding_fixture: _AddingFixture,
) -> None:
    def crash(checkpoint: str) -> None:
        if checkpoint == "after_qb_applied":
            raise SimulatedCrash(checkpoint)

    with pytest.raises(SimulatedCrash):
        await adding_fixture.coordinator.execute(
            adding_fixture.unit_id,
            execution_plan_id=adding_fixture.plan_id,
            fault_hook=crash,
        )
    assert adding_fixture.qbit.add_calls == 1
    with adding_fixture.factory() as session:
        task = session.get(UnpackTask, adding_fixture.task_id)
        assert task is not None and task.status == TaskStatus.ADDING.value
        journal = session.scalar(select(OperationJournal))
        assert journal is not None and journal.status == "APPLIED"

    recovered = await adding_fixture.coordinator.execute(
        adding_fixture.unit_id,
        execution_plan_id=adding_fixture.plan_id,
    )
    assert recovered.status is TaskStatus.SEEDING
    assert recovered.replayed is True
    assert adding_fixture.qbit.add_calls == 1


@pytest.mark.asyncio
async def test_client_verifying_tracks_check_progress_then_enters_seeding(
    adding_fixture: _AddingFixture,
) -> None:
    _set_verification_level(
        adding_fixture,
        VerificationLevel.CLIENT_CHECK_REQUIRED,
        client_check_required=True,
    )
    added = await adding_fixture.coordinator.execute(
        adding_fixture.unit_id,
        execution_plan_id=adding_fixture.plan_id,
    )
    assert added.status is TaskStatus.CLIENT_VERIFYING

    first = await adding_fixture.verifier.execute(
        adding_fixture.unit_id,
        execution_plan_id=adding_fixture.plan_id,
    )
    assert first.status is TaskStatus.CLIENT_VERIFYING
    assert first.verification_outcome == "CHECKING"
    assert first.progress == 0.0
    assert first.checking_observed is True
    assert adding_fixture.qbit.recheck_calls == 1

    adding_fixture.qbit.states[first.torrent_hash] = replace(
        adding_fixture.qbit.states[first.torrent_hash],
        state="checkingUP",
        progress=0.42,
    )
    progress = await adding_fixture.verifier.execute(
        adding_fixture.unit_id,
        execution_plan_id=adding_fixture.plan_id,
    )
    assert progress.status is TaskStatus.CLIENT_VERIFYING
    assert progress.progress == 0.42
    assert adding_fixture.qbit.recheck_calls == 1

    adding_fixture.qbit.states[first.torrent_hash] = replace(
        adding_fixture.qbit.states[first.torrent_hash],
        state="stoppedUP",
        progress=1.0,
    )
    verified = await adding_fixture.verifier.execute(
        adding_fixture.unit_id,
        execution_plan_id=adding_fixture.plan_id,
    )
    assert verified.status is TaskStatus.SEEDING
    assert verified.verification_outcome == "VERIFIED"
    assert verified.progress == 1.0
    assert adding_fixture.qbit.recheck_calls == 1

    add_replay = await adding_fixture.coordinator.execute(
        adding_fixture.unit_id,
        execution_plan_id=adding_fixture.plan_id,
    )
    assert add_replay.status is TaskStatus.SEEDING
    assert add_replay.replayed is True
    assert adding_fixture.qbit.add_calls == 1

    replayed = await adding_fixture.verifier.execute(
        adding_fixture.unit_id,
        execution_plan_id=adding_fixture.plan_id,
    )
    assert replayed.status is TaskStatus.SEEDING
    assert replayed.replayed is True
    assert adding_fixture.qbit.recheck_calls == 1


@pytest.mark.asyncio
async def test_client_verifying_never_accepts_unchanged_precheck_complete_state(
    adding_fixture: _AddingFixture,
) -> None:
    _set_verification_level(
        adding_fixture,
        VerificationLevel.CLIENT_CHECK_REQUIRED,
        client_check_required=True,
    )
    await adding_fixture.coordinator.execute(
        adding_fixture.unit_id,
        execution_plan_id=adding_fixture.plan_id,
    )
    adding_fixture.qbit.apply_on_recheck = False

    first = await adding_fixture.verifier.execute(
        adding_fixture.unit_id,
        execution_plan_id=adding_fixture.plan_id,
    )
    repeated = await adding_fixture.verifier.execute(
        adding_fixture.unit_id,
        execution_plan_id=adding_fixture.plan_id,
    )

    assert first.status is TaskStatus.CLIENT_VERIFYING
    assert repeated.status is TaskStatus.CLIENT_VERIFYING
    assert repeated.verification_outcome == "AWAITING_CHECK_EVIDENCE"
    assert repeated.checking_observed is False
    assert adding_fixture.qbit.recheck_calls == 1


@pytest.mark.asyncio
async def test_client_verifying_recheck_response_loss_recovers_without_second_recheck(
    adding_fixture: _AddingFixture,
) -> None:
    _set_verification_level(
        adding_fixture,
        VerificationLevel.CLIENT_CHECK_REQUIRED,
        client_check_required=True,
    )
    await adding_fixture.coordinator.execute(
        adding_fixture.unit_id,
        execution_plan_id=adding_fixture.plan_id,
    )
    adding_fixture.qbit.raise_after_recheck_apply_once = True

    with pytest.raises(ApplicationError) as lost:
        await adding_fixture.verifier.execute(
            adding_fixture.unit_id,
            execution_plan_id=adding_fixture.plan_id,
        )
    assert lost.value.code == "DOWNLOADER_UNAVAILABLE"
    assert adding_fixture.qbit.recheck_calls == 1

    recovered = await adding_fixture.verifier.execute(
        adding_fixture.unit_id,
        execution_plan_id=adding_fixture.plan_id,
    )
    assert recovered.status is TaskStatus.CLIENT_VERIFYING
    assert recovered.recovered_after_unknown_result is True
    assert recovered.checking_observed is True
    assert adding_fixture.qbit.recheck_calls == 1


@pytest.mark.asyncio
async def test_client_verifying_crash_after_recheck_applied_recovers_without_second_recheck(
    adding_fixture: _AddingFixture,
) -> None:
    _set_verification_level(
        adding_fixture,
        VerificationLevel.CLIENT_CHECK_REQUIRED,
        client_check_required=True,
    )
    await adding_fixture.coordinator.execute(
        adding_fixture.unit_id,
        execution_plan_id=adding_fixture.plan_id,
    )

    def crash(checkpoint: str) -> None:
        if checkpoint == "after_recheck_applied":
            raise SimulatedCrash(checkpoint)

    with pytest.raises(SimulatedCrash):
        await adding_fixture.verifier.execute(
            adding_fixture.unit_id,
            execution_plan_id=adding_fixture.plan_id,
            fault_hook=crash,
        )
    assert adding_fixture.qbit.recheck_calls == 1
    with adding_fixture.factory() as session:
        task = session.get(UnpackTask, adding_fixture.task_id)
        assert task is not None
        assert task.status == TaskStatus.CLIENT_VERIFYING.value
        assert task.checkpoint["schema_version"] == "packbreaker-post-add-checkpoint-v1"
        journals = list(
            session.scalars(select(OperationJournal).order_by(OperationJournal.created_at))
        )
        assert len(journals) == 2
        assert journals[1].operation_type == "QBITTORRENT_RECHECK"
        assert journals[1].status == "APPLIED"

    recovered = await adding_fixture.verifier.execute(
        adding_fixture.unit_id,
        execution_plan_id=adding_fixture.plan_id,
    )
    assert recovered.status is TaskStatus.CLIENT_VERIFYING
    assert recovered.replayed is True
    assert adding_fixture.qbit.recheck_calls == 1


@pytest.mark.asyncio
async def test_client_verifying_incomplete_check_transitions_to_retry(
    adding_fixture: _AddingFixture,
) -> None:
    _set_verification_level(
        adding_fixture,
        VerificationLevel.CLIENT_CHECK_REQUIRED,
        client_check_required=True,
    )
    await adding_fixture.coordinator.execute(
        adding_fixture.unit_id,
        execution_plan_id=adding_fixture.plan_id,
    )
    checking = await adding_fixture.verifier.execute(
        adding_fixture.unit_id,
        execution_plan_id=adding_fixture.plan_id,
    )
    adding_fixture.qbit.states[checking.torrent_hash] = replace(
        adding_fixture.qbit.states[checking.torrent_hash],
        state="stoppedDL",
        progress=0.75,
    )

    failed = await adding_fixture.verifier.execute(
        adding_fixture.unit_id,
        execution_plan_id=adding_fixture.plan_id,
    )
    assert failed.status is TaskStatus.RETRY
    assert failed.verification_outcome == "INCOMPLETE"
    assert failed.progress == 0.75
    assert adding_fixture.qbit.recheck_calls == 1

    add_replay = await adding_fixture.coordinator.execute(
        adding_fixture.unit_id,
        execution_plan_id=adding_fixture.plan_id,
    )
    assert add_replay.status is TaskStatus.RETRY
    assert add_replay.replayed is True
    assert adding_fixture.qbit.add_calls == 1

    replayed = await adding_fixture.verifier.execute(
        adding_fixture.unit_id,
        execution_plan_id=adding_fixture.plan_id,
    )
    assert replayed.status is TaskStatus.RETRY
    assert replayed.replayed is True


@pytest.mark.asyncio
async def test_client_verifying_binding_change_blocks_before_recheck(
    adding_fixture: _AddingFixture,
) -> None:
    _set_verification_level(
        adding_fixture,
        VerificationLevel.CLIENT_CHECK_REQUIRED,
        client_check_required=True,
    )
    await adding_fixture.coordinator.execute(
        adding_fixture.unit_id,
        execution_plan_id=adding_fixture.plan_id,
    )
    adding_fixture.downloader_provider.binding = replace(
        adding_fixture.downloader_provider.binding,
        binding_digest="f" * 64,
    )

    with pytest.raises(ApplicationError) as failure:
        await adding_fixture.verifier.execute(
            adding_fixture.unit_id,
            execution_plan_id=adding_fixture.plan_id,
        )

    assert failure.value.code == "CLIENT_VERIFYING_BINDING_CHANGED"
    assert adding_fixture.qbit.recheck_calls == 0


@pytest.mark.asyncio
async def test_client_verifying_external_torrent_delete_marks_recheck_for_reconciliation(
    adding_fixture: _AddingFixture,
) -> None:
    _set_verification_level(
        adding_fixture,
        VerificationLevel.CLIENT_CHECK_REQUIRED,
        client_check_required=True,
    )
    await adding_fixture.coordinator.execute(
        adding_fixture.unit_id,
        execution_plan_id=adding_fixture.plan_id,
    )
    checking = await adding_fixture.verifier.execute(
        adding_fixture.unit_id,
        execution_plan_id=adding_fixture.plan_id,
    )
    del adding_fixture.qbit.states[checking.torrent_hash]

    with pytest.raises(ApplicationError) as failure:
        await adding_fixture.verifier.execute(
            adding_fixture.unit_id,
            execution_plan_id=adding_fixture.plan_id,
        )

    assert failure.value.code == "DOWNLOADER_STATE_MISMATCH"
    assert adding_fixture.qbit.recheck_calls == 1
    with adding_fixture.factory() as session:
        journals = list(
            session.scalars(select(OperationJournal).order_by(OperationJournal.created_at))
        )
        assert journals[1].status == "RECONCILE_REQUIRED"
        task = session.get(UnpackTask, adding_fixture.task_id)
        assert task is not None and task.status == TaskStatus.CLIENT_VERIFYING.value


@pytest.mark.asyncio
async def test_full_verified_seeding_start_transitions_to_done_and_replays_once(
    adding_fixture: _AddingFixture,
) -> None:
    added = await adding_fixture.coordinator.execute(
        adding_fixture.unit_id,
        execution_plan_id=adding_fixture.plan_id,
    )
    assert added.status is TaskStatus.SEEDING

    done = await adding_fixture.seeder.execute(
        adding_fixture.unit_id,
        execution_plan_id=adding_fixture.plan_id,
    )

    assert done.status is TaskStatus.DONE
    assert done.client_state == "stalledUP"
    assert done.progress == 1.0
    assert adding_fixture.qbit.start_calls == 1
    replayed = await adding_fixture.seeder.execute(
        adding_fixture.unit_id,
        execution_plan_id=adding_fixture.plan_id,
    )
    assert replayed.status is TaskStatus.DONE
    assert replayed.replayed is True
    assert replayed.start_journal_id == done.start_journal_id
    assert adding_fixture.qbit.start_calls == 1

    add_replay = await adding_fixture.coordinator.execute(
        adding_fixture.unit_id,
        execution_plan_id=adding_fixture.plan_id,
    )
    assert add_replay.status is TaskStatus.DONE
    assert add_replay.replayed is True
    assert adding_fixture.qbit.add_calls == 1
    with adding_fixture.factory() as session:
        task = session.get(UnpackTask, adding_fixture.task_id)
        assert task is not None and task.status == TaskStatus.DONE.value
        journals = list(
            session.scalars(select(OperationJournal).order_by(OperationJournal.created_at))
        )
        assert [item.operation_type for item in journals] == [
            "QBITTORRENT_ADD",
            "QBITTORRENT_START",
        ]
        assert all(item.status == "APPLIED" for item in journals)


@pytest.mark.asyncio
async def test_client_verified_seeding_start_preserves_recheck_chain_until_done(
    adding_fixture: _AddingFixture,
) -> None:
    _set_verification_level(
        adding_fixture,
        VerificationLevel.CLIENT_CHECK_REQUIRED,
        client_check_required=True,
    )
    added = await adding_fixture.coordinator.execute(
        adding_fixture.unit_id,
        execution_plan_id=adding_fixture.plan_id,
    )
    assert added.status is TaskStatus.CLIENT_VERIFYING
    checking = await adding_fixture.verifier.execute(
        adding_fixture.unit_id,
        execution_plan_id=adding_fixture.plan_id,
    )
    adding_fixture.qbit.states[checking.torrent_hash] = replace(
        adding_fixture.qbit.states[checking.torrent_hash],
        state="stoppedUP",
        progress=1.0,
    )
    verified = await adding_fixture.verifier.execute(
        adding_fixture.unit_id,
        execution_plan_id=adding_fixture.plan_id,
    )
    assert verified.status is TaskStatus.SEEDING

    done = await adding_fixture.seeder.execute(
        adding_fixture.unit_id,
        execution_plan_id=adding_fixture.plan_id,
    )

    assert done.status is TaskStatus.DONE
    assert adding_fixture.qbit.recheck_calls == 1
    assert adding_fixture.qbit.start_calls == 1
    verifier_replay = await adding_fixture.verifier.execute(
        adding_fixture.unit_id,
        execution_plan_id=adding_fixture.plan_id,
    )
    assert verifier_replay.status is TaskStatus.DONE
    assert verifier_replay.replayed is True
    assert adding_fixture.qbit.recheck_calls == 1
    with adding_fixture.factory() as session:
        journals = list(
            session.scalars(select(OperationJournal).order_by(OperationJournal.created_at))
        )
        assert [item.operation_type for item in journals] == [
            "QBITTORRENT_ADD",
            "QBITTORRENT_RECHECK",
            "QBITTORRENT_START",
        ]
        assert all(item.status == "APPLIED" for item in journals)


@pytest.mark.asyncio
async def test_seeding_start_response_loss_recovers_without_second_start(
    adding_fixture: _AddingFixture,
) -> None:
    await adding_fixture.coordinator.execute(
        adding_fixture.unit_id,
        execution_plan_id=adding_fixture.plan_id,
    )
    adding_fixture.qbit.raise_after_start_apply_once = True

    with pytest.raises(ApplicationError) as lost:
        await adding_fixture.seeder.execute(
            adding_fixture.unit_id,
            execution_plan_id=adding_fixture.plan_id,
        )
    assert lost.value.code == "DOWNLOADER_UNAVAILABLE"
    assert adding_fixture.qbit.start_calls == 1
    with adding_fixture.factory() as session:
        task = session.get(UnpackTask, adding_fixture.task_id)
        assert task is not None and task.status == TaskStatus.SEEDING.value
        journals = list(
            session.scalars(select(OperationJournal).order_by(OperationJournal.created_at))
        )
        assert journals[-1].operation_type == "QBITTORRENT_START"
        assert journals[-1].status == "INTENT_RECORDED"

    recovered = await adding_fixture.seeder.execute(
        adding_fixture.unit_id,
        execution_plan_id=adding_fixture.plan_id,
    )
    assert recovered.status is TaskStatus.DONE
    assert recovered.recovered_after_unknown_result is True
    assert adding_fixture.qbit.start_calls == 1


@pytest.mark.asyncio
async def test_seeding_crash_after_start_applied_only_finishes_task_on_replay(
    adding_fixture: _AddingFixture,
) -> None:
    await adding_fixture.coordinator.execute(
        adding_fixture.unit_id,
        execution_plan_id=adding_fixture.plan_id,
    )

    def crash(checkpoint: str) -> None:
        if checkpoint == "after_start_applied":
            raise SimulatedCrash(checkpoint)

    with pytest.raises(SimulatedCrash):
        await adding_fixture.seeder.execute(
            adding_fixture.unit_id,
            execution_plan_id=adding_fixture.plan_id,
            fault_hook=crash,
        )
    assert adding_fixture.qbit.start_calls == 1
    with adding_fixture.factory() as session:
        task = session.get(UnpackTask, adding_fixture.task_id)
        assert task is not None and task.status == TaskStatus.SEEDING.value
        journals = list(
            session.scalars(select(OperationJournal).order_by(OperationJournal.created_at))
        )
        assert journals[-1].operation_type == "QBITTORRENT_START"
        assert journals[-1].status == "APPLIED"

    recovered = await adding_fixture.seeder.execute(
        adding_fixture.unit_id,
        execution_plan_id=adding_fixture.plan_id,
    )
    assert recovered.status is TaskStatus.DONE
    assert recovered.replayed is True
    assert adding_fixture.qbit.start_calls == 1


@pytest.mark.asyncio
async def test_seeding_rejects_incomplete_torrent_before_start(
    adding_fixture: _AddingFixture,
) -> None:
    added = await adding_fixture.coordinator.execute(
        adding_fixture.unit_id,
        execution_plan_id=adding_fixture.plan_id,
    )
    adding_fixture.qbit.states[added.torrent_hash] = replace(
        adding_fixture.qbit.states[added.torrent_hash],
        state="stoppedDL",
        progress=0.99,
    )

    with pytest.raises(ApplicationError) as failure:
        await adding_fixture.seeder.execute(
            adding_fixture.unit_id,
            execution_plan_id=adding_fixture.plan_id,
        )

    assert failure.value.code == "DOWNLOADER_START_STATE_INVALID"
    assert adding_fixture.qbit.start_calls == 0
    with adding_fixture.factory() as session:
        assert session.scalar(select(func.count()).select_from(OperationJournal)) == 1


@pytest.mark.asyncio
async def test_seeding_binding_change_blocks_before_start(
    adding_fixture: _AddingFixture,
) -> None:
    await adding_fixture.coordinator.execute(
        adding_fixture.unit_id,
        execution_plan_id=adding_fixture.plan_id,
    )
    adding_fixture.downloader_provider.binding = replace(
        adding_fixture.downloader_provider.binding,
        binding_digest="f" * 64,
    )

    with pytest.raises(ApplicationError) as failure:
        await adding_fixture.seeder.execute(
            adding_fixture.unit_id,
            execution_plan_id=adding_fixture.plan_id,
        )

    assert failure.value.code == "SEEDING_BINDING_CHANGED"
    assert adding_fixture.qbit.start_calls == 0


@pytest.mark.asyncio
async def test_seeding_source_change_blocks_before_start_intent(
    adding_fixture: _AddingFixture,
) -> None:
    await adding_fixture.coordinator.execute(
        adding_fixture.unit_id,
        execution_plan_id=adding_fixture.plan_id,
    )
    (adding_fixture.source_file.parent / "late-change.nfo").write_text("changed")

    with pytest.raises(ApplicationError) as failure:
        await adding_fixture.seeder.execute(
            adding_fixture.unit_id,
            execution_plan_id=adding_fixture.plan_id,
        )

    assert failure.value.code == "SEEDING_SOURCE_CHANGED"
    assert adding_fixture.qbit.start_calls == 0
    with adding_fixture.factory() as session:
        assert session.scalar(select(func.count()).select_from(OperationJournal)) == 1


def _recovery_coordinator(
    fixture: _AddingFixture,
    cancellation: TaskCancellationCoordinator | None = None,
) -> TaskRecoveryCoordinator:
    class _LinkingMustNotRun:
        def execute(self, unit_id: str, *, execution_plan_id: str) -> object:
            raise AssertionError("ADDING 起始夹具不应回退到 LINKING")

    return TaskRecoveryCoordinator(
        fixture.factory,
        _LinkingMustNotRun(),
        fixture.coordinator,
        fixture.verifier,
        fixture.seeder,
        cancellation,
    )


@pytest.mark.asyncio
async def test_startup_recovery_full_verified_converges_adding_to_done(
    adding_fixture: _AddingFixture,
) -> None:
    report = await _recovery_coordinator(adding_fixture).reconcile_once()

    assert report.scanned_count == 1
    assert report.completed_count == 1
    assert report.waiting_count == 0
    assert report.blocked_count == 0
    assert report.truncated is False
    item = report.items[0]
    assert item.initial_status is TaskStatus.ADDING
    assert item.final_status is TaskStatus.DONE
    assert item.outcome is RecoveryOutcome.COMPLETED
    assert item.steps == (TaskStatus.ADDING, TaskStatus.SEEDING)
    assert adding_fixture.qbit.add_calls == 1
    assert adding_fixture.qbit.start_calls == 1


@pytest.mark.asyncio
async def test_startup_recovery_client_check_waits_then_finishes_on_next_tick(
    adding_fixture: _AddingFixture,
) -> None:
    _set_verification_level(
        adding_fixture,
        VerificationLevel.CLIENT_CHECK_REQUIRED,
        client_check_required=True,
    )
    recovery = _recovery_coordinator(adding_fixture)

    first = await recovery.reconcile_once()
    assert first.items[0].initial_status is TaskStatus.ADDING
    assert first.items[0].final_status is TaskStatus.CLIENT_VERIFYING
    assert first.items[0].outcome is RecoveryOutcome.WAITING
    assert first.items[0].steps == (TaskStatus.ADDING, TaskStatus.CLIENT_VERIFYING)
    assert adding_fixture.qbit.add_calls == 1
    assert adding_fixture.qbit.recheck_calls == 1
    assert adding_fixture.qbit.start_calls == 0

    actual_hash = next(iter(adding_fixture.qbit.states))
    adding_fixture.qbit.states[actual_hash] = replace(
        adding_fixture.qbit.states[actual_hash],
        state="stoppedUP",
        progress=1.0,
    )

    second = await recovery.reconcile_once()
    assert second.items[0].initial_status is TaskStatus.CLIENT_VERIFYING
    assert second.items[0].final_status is TaskStatus.DONE
    assert second.items[0].outcome is RecoveryOutcome.COMPLETED
    assert second.items[0].steps == (TaskStatus.CLIENT_VERIFYING, TaskStatus.SEEDING)
    assert adding_fixture.qbit.recheck_calls == 1
    assert adding_fixture.qbit.start_calls == 1


@pytest.mark.asyncio
async def test_periodic_driver_finishes_client_verification_without_restart(
    adding_fixture: _AddingFixture,
) -> None:
    _set_verification_level(
        adding_fixture,
        VerificationLevel.CLIENT_CHECK_REQUIRED,
        client_check_required=True,
    )
    recovery = _recovery_coordinator(adding_fixture)
    first = await recovery.reconcile_once()
    assert first.items[0].final_status is TaskStatus.CLIENT_VERIFYING

    actual_hash = next(iter(adding_fixture.qbit.states))
    adding_fixture.qbit.states[actual_hash] = replace(
        adding_fixture.qbit.states[actual_hash],
        state="stoppedUP",
        progress=1.0,
    )
    driver = ActiveTaskDriver(
        recovery,
        interval_seconds=0.01,
        limit=10,
        max_steps_per_task=4,
    )
    driver.start()
    try:
        final_status = TaskStatus.CLIENT_VERIFYING
        for _ in range(100):
            with adding_fixture.factory() as session:
                task = session.get(UnpackTask, adding_fixture.task_id)
                assert task is not None
                final_status = TaskStatus(task.status)
            if final_status is TaskStatus.DONE:
                break
            await asyncio.sleep(0.005)
        assert final_status is TaskStatus.DONE
        assert driver.state.ticks_completed >= 1
        assert adding_fixture.qbit.recheck_calls == 1
        assert adding_fixture.qbit.start_calls == 1
    finally:
        await driver.stop()


@pytest.mark.asyncio
async def test_startup_recovery_bad_plan_digest_is_blocked_without_side_effect(
    adding_fixture: _AddingFixture,
) -> None:
    with adding_fixture.factory() as session:
        task = session.get(UnpackTask, adding_fixture.task_id)
        assert task is not None
        task.checkpoint = {**task.checkpoint, "execution_plan_digest": "f" * 64}
        session.commit()

    report = await _recovery_coordinator(adding_fixture).reconcile_once()

    assert report.blocked_count == 1
    assert report.items[0].outcome is RecoveryOutcome.BLOCKED
    assert report.items[0].error_code == "RECOVERY_PLAN_MISMATCH"
    assert report.items[0].final_status is TaskStatus.ADDING
    assert adding_fixture.qbit.add_calls == 0
    assert adding_fixture.qbit.recheck_calls == 0
    assert adding_fixture.qbit.start_calls == 0


@pytest.mark.asyncio
async def test_startup_recovery_step_limit_is_bounded(adding_fixture: _AddingFixture) -> None:
    item = await _recovery_coordinator(adding_fixture).reconcile_task(
        adding_fixture.task_id,
        max_steps=1,
    )

    assert item.initial_status is TaskStatus.ADDING
    assert item.final_status is TaskStatus.SEEDING
    assert item.outcome is RecoveryOutcome.WAITING
    assert item.steps == (TaskStatus.ADDING,)
    assert adding_fixture.qbit.add_calls == 1
    assert adding_fixture.qbit.start_calls == 0


@pytest.mark.asyncio
async def test_startup_recovery_blocked_task_does_not_prevent_later_task(
    adding_fixture: _AddingFixture,
) -> None:
    blocked_task_id = new_uuid()
    with adding_fixture.factory() as session:
        old = utc_now() - timedelta(days=1)
        session.add(
            UnpackTask(
                id=blocked_task_id,
                type="PACKAGE_UNPACK",
                source_downloader_id="source-downloader",
                source_hash="blocked-source",
                normalized_unit_key="blocked-unit",
                idempotency_key="f" * 64,
                status=TaskStatus.ADDING.value,
                trace_id=new_uuid(),
                checkpoint={
                    "execution_plan_id": "missing-plan",
                    "execution_plan_digest": "e" * 64,
                },
                error_code=None,
                version=1,
                created_at=old,
                updated_at=old,
            )
        )
        session.commit()

    report = await _recovery_coordinator(adding_fixture).reconcile_once()

    assert report.scanned_count == 2
    assert report.blocked_count == 1
    assert report.completed_count == 1
    assert report.items[0].task_id == blocked_task_id
    assert report.items[0].outcome is RecoveryOutcome.BLOCKED
    assert report.items[0].error_code == "RECOVERY_PLAN_MISMATCH"
    assert report.items[1].task_id == adding_fixture.task_id
    assert report.items[1].outcome is RecoveryOutcome.COMPLETED
    assert adding_fixture.qbit.add_calls == 1
    assert adding_fixture.qbit.start_calls == 1


@pytest.mark.asyncio
async def test_startup_recovery_limit_reports_truncation_without_touching_later_task(
    adding_fixture: _AddingFixture,
) -> None:
    blocked_task_id = new_uuid()
    with adding_fixture.factory() as session:
        old = utc_now() - timedelta(days=1)
        session.add(
            UnpackTask(
                id=blocked_task_id,
                type="PACKAGE_UNPACK",
                source_downloader_id="source-downloader",
                source_hash="blocked-source",
                normalized_unit_key="blocked-unit",
                idempotency_key="e" * 64,
                status=TaskStatus.ADDING.value,
                trace_id=new_uuid(),
                checkpoint={
                    "execution_plan_id": "missing-plan",
                    "execution_plan_digest": "d" * 64,
                },
                error_code=None,
                version=1,
                created_at=old,
                updated_at=old,
            )
        )
        session.commit()

    report = await _recovery_coordinator(adding_fixture).reconcile_once(limit=1)

    assert report.scanned_count == 1
    assert report.truncated is True
    assert report.items[0].task_id == blocked_task_id
    assert report.items[0].outcome is RecoveryOutcome.BLOCKED
    assert adding_fixture.qbit.add_calls == 0
    assert adding_fixture.qbit.start_calls == 0


async def _prepare_transmission_seeding_for_cancellation(
    adding_fixture: _AddingFixture,
) -> tuple[_FakeTransmission, str]:
    current_binding = adding_fixture.downloader_provider.binding
    assert isinstance(current_binding, QbittorrentWriteBinding)
    capabilities = {
        "client": "Transmission",
        "version": "4.1.3",
        "api_version": "6.0.0",
        "supports_skip_checking": False,
        "supports_force_recheck": True,
        "supports_verify_progress": True,
    }
    binding_digest = downloader_execution_binding_digest(
        downloader_id=current_binding.downloader_id,
        version=current_binding.downloader_version,
        kind=DownloaderKind.TRANSMISSION,
        enabled=True,
        connection_status=ProbeStatus.OK,
        path_mapping_status=ProbeStatus.OK,
        path_mappings=current_binding.path_mappings,
        capabilities=capabilities,
    )
    transmission = _FakeTransmission()
    adding_fixture.downloader_provider.binding = TransmissionWriteBinding(
        downloader_id=current_binding.downloader_id,
        downloader_version=current_binding.downloader_version,
        binding_digest=binding_digest,
        path_mappings=current_binding.path_mappings,
        capabilities=capabilities,
        adapter=transmission,
        data_root=adding_fixture.data_root,
    )
    with adding_fixture.factory() as session:
        plan = TaskExecutionPlanRepository(session).get(adding_fixture.plan_id)
        task = session.get(UnpackTask, adding_fixture.task_id)
        assert plan is not None and task is not None
        plan.payload = {
            **plan.payload,
            "target_downloader_binding_digest": binding_digest,
        }
        task.checkpoint = {
            **task.checkpoint,
            "target_downloader_binding_digest": binding_digest,
        }
        session.commit()

    adding = TaskAddingCoordinator(
        adding_fixture.factory,
        _FakeSiteProvider(adding_fixture.site_adapter),
        adding_fixture.downloader_provider,
        QbittorrentAddOperationService(adding_fixture.factory),
        TransmissionAddOperationService(adding_fixture.factory),
        data_root=adding_fixture.data_root,
    )
    verifier = TaskClientVerificationCoordinator(
        adding_fixture.factory,
        adding_fixture.downloader_provider,
        QbittorrentRecheckOperationService(adding_fixture.factory),
        TransmissionVerifyOperationService(adding_fixture.factory),
        data_root=adding_fixture.data_root,
    )
    added = await adding.execute(
        adding_fixture.unit_id,
        execution_plan_id=adding_fixture.plan_id,
    )
    checking = await verifier.execute(
        adding_fixture.unit_id,
        execution_plan_id=adding_fixture.plan_id,
    )
    assert checking.status is TaskStatus.CLIENT_VERIFYING
    transmission.states[added.torrent_hash] = replace(
        transmission.states[added.torrent_hash],
        status=0,
        percent_done=1.0,
        recheck_progress=1.0,
    )
    verified = await verifier.execute(
        adding_fixture.unit_id,
        execution_plan_id=adding_fixture.plan_id,
    )
    assert verified.status is TaskStatus.SEEDING
    return transmission, added.torrent_hash


@pytest.mark.asyncio
async def test_transmission_cancellation_removes_before_rollback_and_recovers_without_second_remove(
    adding_fixture: _AddingFixture,
) -> None:
    filesystem = FilesystemOperationService(
        adding_fixture.factory,
        SafeFilesystemGateway(adding_fixture.data_root),
    )
    source_stat = adding_fixture.source_file.stat(follow_symlinks=False)
    link_result = filesystem.execute_hardlink(
        HardlinkExecutionRequest(
            task_id=adding_fixture.task_id,
            candidate_key="2" * 64,
            source_relative_path=f"source/{adding_fixture.source_file.name}",
            target_root_relative_path="target",
            target_relative_path="owned/transmission-recovery.mkv",
            expected_source_snapshot=FileSnapshot(
                device=source_stat.st_dev,
                inode=source_stat.st_ino,
                size=source_stat.st_size,
                mtime_ns=source_stat.st_mtime_ns,
            ),
        )
    )
    transmission, torrent_hash = await _prepare_transmission_seeding_for_cancellation(
        adding_fixture
    )
    owned_target = adding_fixture.data_root / "target" / "owned" / "transmission-recovery.mkv"
    cancellation = TaskCancellationCoordinator(
        adding_fixture.factory,
        adding_fixture.downloader_provider,
        QbittorrentRemoveOperationService(adding_fixture.factory),
        filesystem,
        TransmissionRemoveOperationService(adding_fixture.factory),
    )
    request = TaskCancellationRequest(
        task_id=adding_fixture.task_id,
        remove_downloader_task=True,
        rollback_created_resources=True,
    )

    def crash(checkpoint: str) -> None:
        if checkpoint == "after_transmission_removed":
            raise SimulatedCrash(checkpoint)

    with pytest.raises(SimulatedCrash):
        await cancellation.execute(request, fault_hook=crash)

    assert transmission.remove_calls == 1
    assert torrent_hash not in transmission.states
    assert owned_target.exists()
    with adding_fixture.factory() as session:
        task = session.get(UnpackTask, adding_fixture.task_id)
        assert task is not None and task.status == TaskStatus.ROLLING_BACK.value
        assert task.checkpoint["downloader_kind"] == DownloaderKind.TRANSMISSION.value
        assert task.checkpoint["schema_version"] == "packbreaker-cancellation-checkpoint-v2"

    report = await _recovery_coordinator(adding_fixture, cancellation).reconcile_once()

    assert report.scanned_count == 1
    assert report.completed_count == 1
    assert report.items[0].initial_status is TaskStatus.ROLLING_BACK
    assert report.items[0].final_status is TaskStatus.CANCELLED
    assert report.items[0].steps == (TaskStatus.ROLLING_BACK,)
    assert transmission.remove_calls == 1
    assert not owned_target.exists()
    with adding_fixture.factory() as session:
        hardlink = session.get(OperationJournal, link_result.hardlink_journal_id)
        assert hardlink is not None and hardlink.status == OperationStatus.ROLLED_BACK.value
        remove_journal = session.scalar(
            select(OperationJournal).where(
                OperationJournal.operation_type == TRANSMISSION_REMOVE_OPERATION
            )
        )
        assert remove_journal is not None
        assert remove_journal.status == OperationStatus.APPLIED.value
        assert remove_journal.intent["delete_local_data"] is False
        task = session.get(UnpackTask, adding_fixture.task_id)
        assert task is not None
        assert task.checkpoint["remove_journal_id"] == remove_journal.id
        assert task.checkpoint["qbit_remove_journal_id"] is None


@pytest.mark.asyncio
async def test_cancellation_removes_qb_and_rolls_back_only_journal_owned_files(
    adding_fixture: _AddingFixture,
) -> None:
    filesystem = FilesystemOperationService(
        adding_fixture.factory,
        SafeFilesystemGateway(adding_fixture.data_root),
    )
    source_stat = adding_fixture.source_file.stat(follow_symlinks=False)
    link_result = filesystem.execute_hardlink(
        HardlinkExecutionRequest(
            task_id=adding_fixture.task_id,
            candidate_key="d" * 64,
            source_relative_path=f"source/{adding_fixture.source_file.name}",
            target_root_relative_path="target",
            target_relative_path="owned/copy.mkv",
            expected_source_snapshot=FileSnapshot(
                device=source_stat.st_dev,
                inode=source_stat.st_ino,
                size=source_stat.st_size,
                mtime_ns=source_stat.st_mtime_ns,
            ),
        )
    )
    added = await adding_fixture.coordinator.execute(
        adding_fixture.unit_id,
        execution_plan_id=adding_fixture.plan_id,
    )
    assert added.status is TaskStatus.SEEDING
    owned_target = adding_fixture.data_root / "target" / "owned" / "copy.mkv"
    unowned_target = adding_fixture.data_root / "target" / adding_fixture.source_file.name
    cancellation = TaskCancellationCoordinator(
        adding_fixture.factory,
        adding_fixture.downloader_provider,
        QbittorrentRemoveOperationService(adding_fixture.factory),
        filesystem,
    )
    request = TaskCancellationRequest(
        task_id=adding_fixture.task_id,
        remove_downloader_task=True,
        rollback_created_resources=True,
    )

    result = await cancellation.execute(request)
    replayed = await cancellation.execute(request)

    assert result.status is TaskStatus.CANCELLED
    assert replayed.status is TaskStatus.CANCELLED
    assert replayed.replayed is True
    assert adding_fixture.qbit.remove_calls == 1
    assert not owned_target.exists()
    assert unowned_target.exists()
    assert adding_fixture.source_file.read_bytes() == b"0123456789abcdef"
    with adding_fixture.factory() as session:
        hardlink = session.get(OperationJournal, link_result.hardlink_journal_id)
        assert hardlink is not None and hardlink.status == "ROLLED_BACK"
        task = session.get(UnpackTask, adding_fixture.task_id)
        assert task is not None and task.status == TaskStatus.CANCELLED.value
        rollback_event = session.scalar(
            select(TaskEvent)
            .where(
                TaskEvent.task_id == adding_fixture.task_id,
                TaskEvent.event_type == "ROLLBACK_COMPLETED",
            )
            .order_by(TaskEvent.created_at.desc(), TaskEvent.id.desc())
            .limit(1)
        )
        assert rollback_event is not None
        assert "1 个 hardlink" in rollback_event.reason
        assert "1 个目录" in rollback_event.reason
        assert "源媒体不在删除范围" in rollback_event.reason


@pytest.mark.asyncio
async def test_cancellation_requires_qb_removal_before_file_rollback(
    adding_fixture: _AddingFixture,
) -> None:
    await adding_fixture.coordinator.execute(
        adding_fixture.unit_id,
        execution_plan_id=adding_fixture.plan_id,
    )
    cancellation = TaskCancellationCoordinator(
        adding_fixture.factory,
        adding_fixture.downloader_provider,
        QbittorrentRemoveOperationService(adding_fixture.factory),
        FilesystemOperationService(
            adding_fixture.factory,
            SafeFilesystemGateway(adding_fixture.data_root),
        ),
    )

    with pytest.raises(ApplicationError) as failure:
        await cancellation.execute(
            TaskCancellationRequest(
                task_id=adding_fixture.task_id,
                remove_downloader_task=False,
                rollback_created_resources=True,
            )
        )

    assert failure.value.code == "CANCELLATION_DOWNLOADER_REQUIRED"
    assert adding_fixture.qbit.remove_calls == 0
    with adding_fixture.factory() as session:
        task = session.get(UnpackTask, adding_fixture.task_id)
        assert task is not None and task.status == TaskStatus.SEEDING.value


@pytest.mark.asyncio
async def test_cancellation_blocks_if_owned_hardlink_was_replaced(
    adding_fixture: _AddingFixture,
) -> None:
    filesystem = FilesystemOperationService(
        adding_fixture.factory,
        SafeFilesystemGateway(adding_fixture.data_root),
    )
    source_stat = adding_fixture.source_file.stat(follow_symlinks=False)
    filesystem.execute_hardlink(
        HardlinkExecutionRequest(
            task_id=adding_fixture.task_id,
            candidate_key="e" * 64,
            source_relative_path=f"source/{adding_fixture.source_file.name}",
            target_root_relative_path="target",
            target_relative_path="owned/copy.mkv",
            expected_source_snapshot=FileSnapshot(
                device=source_stat.st_dev,
                inode=source_stat.st_ino,
                size=source_stat.st_size,
                mtime_ns=source_stat.st_mtime_ns,
            ),
        )
    )
    await adding_fixture.coordinator.execute(
        adding_fixture.unit_id,
        execution_plan_id=adding_fixture.plan_id,
    )
    owned_target = adding_fixture.data_root / "target" / "owned" / "copy.mkv"
    owned_target.unlink()
    owned_target.write_bytes(b"external-replacement")
    cancellation = TaskCancellationCoordinator(
        adding_fixture.factory,
        adding_fixture.downloader_provider,
        QbittorrentRemoveOperationService(adding_fixture.factory),
        filesystem,
    )

    with pytest.raises(DomainViolation):
        await cancellation.execute(
            TaskCancellationRequest(
                task_id=adding_fixture.task_id,
                remove_downloader_task=True,
                rollback_created_resources=True,
            )
        )

    assert owned_target.read_bytes() == b"external-replacement"
    assert adding_fixture.qbit.remove_calls == 1
    with adding_fixture.factory() as session:
        task = session.get(UnpackTask, adding_fixture.task_id)
        assert task is not None and task.status == TaskStatus.ROLLING_BACK.value


@pytest.mark.asyncio
async def test_startup_recovery_resumes_rolling_back_without_second_qb_remove(
    adding_fixture: _AddingFixture,
) -> None:
    filesystem = FilesystemOperationService(
        adding_fixture.factory,
        SafeFilesystemGateway(adding_fixture.data_root),
    )
    source_stat = adding_fixture.source_file.stat(follow_symlinks=False)
    filesystem.execute_hardlink(
        HardlinkExecutionRequest(
            task_id=adding_fixture.task_id,
            candidate_key="f" * 64,
            source_relative_path=f"source/{adding_fixture.source_file.name}",
            target_root_relative_path="target",
            target_relative_path="owned/recovery.mkv",
            expected_source_snapshot=FileSnapshot(
                device=source_stat.st_dev,
                inode=source_stat.st_ino,
                size=source_stat.st_size,
                mtime_ns=source_stat.st_mtime_ns,
            ),
        )
    )
    await adding_fixture.coordinator.execute(
        adding_fixture.unit_id,
        execution_plan_id=adding_fixture.plan_id,
    )
    owned_target = adding_fixture.data_root / "target" / "owned" / "recovery.mkv"
    cancellation = TaskCancellationCoordinator(
        adding_fixture.factory,
        adding_fixture.downloader_provider,
        QbittorrentRemoveOperationService(adding_fixture.factory),
        filesystem,
    )
    request = TaskCancellationRequest(
        task_id=adding_fixture.task_id,
        remove_downloader_task=True,
        rollback_created_resources=True,
    )

    def crash(checkpoint: str) -> None:
        if checkpoint == "after_qb_removed":
            raise SimulatedCrash(checkpoint)

    with pytest.raises(SimulatedCrash):
        await cancellation.execute(request, fault_hook=crash)

    assert adding_fixture.qbit.remove_calls == 1
    assert owned_target.exists()
    with adding_fixture.factory() as session:
        task = session.get(UnpackTask, adding_fixture.task_id)
        assert task is not None and task.status == TaskStatus.ROLLING_BACK.value

        legacy_checkpoint = dict(task.checkpoint)
        legacy_checkpoint["schema_version"] = "packbreaker-cancellation-checkpoint-v1"
        legacy_checkpoint.pop("downloader_kind", None)
        legacy_checkpoint.pop("add_journal_id", None)
        legacy_checkpoint.pop("remove_journal_id", None)
        task.checkpoint = legacy_checkpoint
        session.commit()

    report = await _recovery_coordinator(adding_fixture, cancellation).reconcile_once()

    assert report.scanned_count == 1
    assert report.completed_count == 1
    assert report.items[0].initial_status is TaskStatus.ROLLING_BACK
    assert report.items[0].final_status is TaskStatus.CANCELLED
    assert report.items[0].steps == (TaskStatus.ROLLING_BACK,)
    assert adding_fixture.qbit.remove_calls == 1
    assert not owned_target.exists()


@pytest.mark.asyncio
async def test_cancellation_blocks_on_unresolved_filesystem_intent(
    adding_fixture: _AddingFixture,
) -> None:
    filesystem = FilesystemOperationService(
        adding_fixture.factory,
        SafeFilesystemGateway(adding_fixture.data_root),
    )
    source_stat = adding_fixture.source_file.stat(follow_symlinks=False)
    request = HardlinkExecutionRequest(
        task_id=adding_fixture.task_id,
        candidate_key="1" * 64,
        source_relative_path=f"source/{adding_fixture.source_file.name}",
        target_root_relative_path="target",
        target_relative_path="owned/unresolved.mkv",
        expected_source_snapshot=FileSnapshot(
            device=source_stat.st_dev,
            inode=source_stat.st_ino,
            size=source_stat.st_size,
            mtime_ns=source_stat.st_mtime_ns,
        ),
    )

    def crash(checkpoint: str) -> None:
        if checkpoint == "after_temporary_hardlink":
            raise SimulatedCrash(checkpoint)

    with pytest.raises(SimulatedCrash):
        filesystem.execute_hardlink(request, fault_hook=crash)

    cancellation = TaskCancellationCoordinator(
        adding_fixture.factory,
        adding_fixture.downloader_provider,
        QbittorrentRemoveOperationService(adding_fixture.factory),
        filesystem,
    )
    with pytest.raises(ApplicationError) as failure:
        await cancellation.execute(
            TaskCancellationRequest(
                task_id=adding_fixture.task_id,
                remove_downloader_task=False,
                rollback_created_resources=True,
            )
        )

    assert failure.value.code == "CANCELLATION_EVIDENCE_INVALID"
    assert adding_fixture.qbit.remove_calls == 0
    with adding_fixture.factory() as session:
        task = session.get(UnpackTask, adding_fixture.task_id)
        assert task is not None and task.status == TaskStatus.ADDING.value


def _set_verification_level(
    fixture: _AddingFixture,
    level: VerificationLevel,
    *,
    client_check_required: bool,
) -> None:
    with fixture.factory() as session:
        plan = TaskExecutionPlanRepository(session).get(fixture.plan_id)
        assert plan is not None
        plan.verification_level = level.value
        plan.client_check_required = client_check_required
        plan.payload["verification_level"] = level.value
        plan.payload["client_check_required"] = client_check_required
        gate = session.get(TaskExecutionGateRecord, plan.execution_gate_id)
        assert gate is not None
        gate.verification_level = level.value
        gate.client_check_required = client_check_required
        candidate = session.get(TaskCandidateRecord, plan.candidate_id)
        assert candidate is not None
        candidate.verification_level = level.value
        session.commit()


def _adding_checkpoint(
    plan_id: str,
    plan_digest: str,
    gate_id: str,
    downloader_id: str,
    binding_digest: str,
) -> dict[str, object]:
    return {
        "schema_version": "packbreaker-adding-checkpoint-v1",
        "stage": TaskStatus.ADDING.value,
        "execution_plan_id": plan_id,
        "execution_plan_digest": plan_digest,
        "execution_gate_id": gate_id,
        "execution_gate_digest": "c" * 64,
        "task_version_before_linking": 7,
        "target_downloader_id": downloader_id,
        "target_downloader_version": 3,
        "target_downloader_binding_digest": binding_digest,
        "target_remote_save_path": "/downloads/target",
        "hardlink_journal_ids": [],
        "directory_journal_ids": [],
        "client_fetch_count": 0,
    }


def _v1_torrent(name: bytes, content: bytes, *, piece_length: int) -> bytes:
    pieces = b"".join(
        hashlib.sha1(content[offset : offset + piece_length]).digest()
        for offset in range(0, len(content), piece_length)
    )
    return _bencode(
        {
            b"info": {
                b"length": len(content),
                b"name": name,
                b"piece length": piece_length,
                b"pieces": pieces,
            }
        }
    )


def _bencode(value: object) -> bytes:
    if isinstance(value, int):
        return f"i{value}e".encode()
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        return b"l" + b"".join(_bencode(item) for item in value) + b"e"
    if isinstance(value, Mapping):
        items = sorted(value.items(), key=lambda item: item[0])
        return b"d" + b"".join(_bencode(key) + _bencode(item) for key, item in items) + b"e"
    raise TypeError(type(value).__name__)
