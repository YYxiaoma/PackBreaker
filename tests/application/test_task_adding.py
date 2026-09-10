from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.downloader_operations import QbittorrentAddOperationService
from backend.app.application.downloaders import QbittorrentWriteBinding
from backend.app.application.errors import ApplicationError
from backend.app.application.sites import EnabledSiteAdapter
from backend.app.application.task_adding import TaskAddingCoordinator
from backend.app.domain.downloader import (
    PathMappingRule,
    ProbeStatus,
    downloader_execution_binding_digest,
)
from backend.app.domain.execution_plan import (
    ExecutionPlanAction,
    ExecutionPlanActionKind,
    ExecutionPlanSnapshot,
)
from backend.app.domain.site_adapter import SiteConnectionResult, TorrentDetails, TorrentPayload
from backend.app.domain.site_search import (
    SearchPage,
    SearchQuery,
    SiteSearchCapabilities,
    normalize_candidate_meta,
)
from backend.app.domain.task_state import TaskStatus
from backend.app.domain.verification import DownloaderKind, VerificationLevel
from backend.app.infrastructure.adapters.downloaders import (
    DownloaderAdapterError,
    QbittorrentAddRequest,
    QbittorrentAddResult,
    QbittorrentTorrentState,
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
        self.raise_after_apply_once = False

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

    async def start_torrent(self, torrent_hash: str) -> NoReturn:
        raise AssertionError("ADDING 协调器不应启动 qB torrent")

    async def recheck_torrent(self, torrent_hash: str) -> NoReturn:
        raise AssertionError("本切片尚不启动客户端下载器校验")


@dataclass
class _FakeDownloaderProvider:
    binding: QbittorrentWriteBinding

    def qbittorrent_write_binding(self, downloader_id: str) -> QbittorrentWriteBinding:
        assert downloader_id == self.binding.downloader_id
        return self.binding


@dataclass(frozen=True, slots=True)
class _AddingFixture:
    coordinator: TaskAddingCoordinator
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
    return _AddingFixture(
        coordinator=coordinator,
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
