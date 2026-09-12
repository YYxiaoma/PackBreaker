from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.downloader_operations import (
    QBITTORRENT_ADD_OPERATION,
    QBITTORRENT_RECHECK_OPERATION,
)
from backend.app.application.downloaders import QbittorrentWriteBinding
from backend.app.application.errors import ApplicationError
from backend.app.application.filesystem_operations import (
    CREATE_HARDLINK_OPERATION,
    FILESYSTEM_OPERATION_SCHEMA_VERSION,
)
from backend.app.application.sites import EnabledSiteAdapter
from backend.app.application.task_adding import CLIENT_VERIFICATION_CHECKPOINT_SCHEMA_VERSION
from backend.app.application.task_repairs import TaskRepairPlanService
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
from backend.app.domain.operation import OperationStatus
from backend.app.domain.repair import RepairActionKind, RepairMode
from backend.app.domain.site_adapter import SiteConnectionResult, TorrentDetails, TorrentPayload
from backend.app.domain.site_search import SearchPage, SearchQuery, SiteSearchCapabilities
from backend.app.domain.task_state import TaskStatus
from backend.app.domain.verification import DownloaderKind, VerificationLevel
from backend.app.infrastructure.adapters.downloaders import (
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


class _FakeSiteAdapter:
    def __init__(self, content: bytes) -> None:
        self.content = content

    async def capabilities(self) -> SiteSearchCapabilities:
        return SiteSearchCapabilities()

    async def test_connection(self) -> SiteConnectionResult:
        return SiteConnectionResult("fake")

    async def search(self, query: SearchQuery) -> SearchPage:
        return SearchPage("fake", query.page, (), False, 0)

    async def fetch_details(self, torrent_id: str) -> TorrentDetails:
        raise AssertionError(f"repair plan 不应读取 details: {torrent_id}")

    async def fetch_torrent(self, torrent_id: str) -> TorrentPayload:
        return TorrentPayload("fake", torrent_id, self.content, datetime.now(UTC))


@dataclass
class _SiteProvider:
    adapter: _FakeSiteAdapter

    def enabled_adapters(self) -> tuple[EnabledSiteAdapter, ...]:
        return (EnabledSiteAdapter("site-config", 1, "fake", self.adapter),)

    def enabled_site_versions(self) -> tuple[tuple[str, int], ...]:
        return (("site-config", 1),)


class _ReadOnlyQbittorrent:
    def __init__(self, state: QbittorrentTorrentState) -> None:
        self.state = state
        self.write_calls = 0

    async def get_torrents(
        self, torrent_hashes: tuple[str, ...]
    ) -> tuple[QbittorrentTorrentState, ...]:
        if self.state.torrent_hash in torrent_hashes:
            return (self.state,)
        return ()

    async def add_torrent(self, request: QbittorrentAddRequest) -> QbittorrentAddResult:
        self.write_calls += 1
        raise AssertionError(f"repair plan 不得 add: {request.save_path}")

    async def stop_torrent(self, torrent_hash: str) -> None:
        self.write_calls += 1
        raise AssertionError(f"repair plan 不得 stop: {torrent_hash}")

    async def start_torrent(self, torrent_hash: str) -> None:
        self.write_calls += 1
        raise AssertionError(f"repair plan 不得 start: {torrent_hash}")

    async def recheck_torrent(self, torrent_hash: str) -> None:
        self.write_calls += 1
        raise AssertionError(f"repair plan 不得 recheck: {torrent_hash}")

    async def remove_torrent_keep_files(self, torrent_hash: str) -> None:
        self.write_calls += 1
        raise AssertionError(f"repair plan 不得 remove: {torrent_hash}")


@dataclass
class _DownloaderProvider:
    binding: QbittorrentWriteBinding

    def write_binding(self, downloader_id: str) -> QbittorrentWriteBinding:
        assert downloader_id == self.binding.downloader_id
        return self.binding


@dataclass(frozen=True, slots=True)
class _RepairFixture:
    service: TaskRepairPlanService
    factory: sessionmaker[Session]
    data_root: Path
    source: Path
    target: Path
    task_id: str
    unit_id: str
    plan_id: str
    add_journal_id: str
    verify_journal_id: str
    hardlink_journal_id: str
    ownership_tag: str
    qbit: _ReadOnlyQbittorrent


@pytest.fixture
def repair_fixture(tmp_path: Path) -> Iterator[_RepairFixture]:
    data_root = tmp_path / "data"
    source_root = data_root / "source"
    target_root = data_root / "target"
    source_root.mkdir(parents=True)
    target_root.mkdir()

    expected_content = b"0123456789abcdef"
    source_content = b"012X456789abcdef"
    source = source_root / "Movie.2026.mkv"
    target = target_root / source.name
    source.write_bytes(source_content)
    os.link(source, target)
    inventory = scan_source_inventory(source_root)
    inventory_digest = source_inventory_digest(inventory)
    source_item = inventory[0]

    torrent_content = _v1_torrent(source.name.encode(), expected_content, piece_length=4)
    meta = parse_torrent(torrent_content)
    torrent_hash = meta.v1_info_hash
    assert torrent_hash is not None
    ownership_tag = "pb-repair-ownership-canary"
    downloader_id = "target-qb"
    path_mappings = (PathMappingRule("/downloads", str(data_root)),)
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
        path_mappings=path_mappings,
        capabilities=capabilities,
    )
    qbit = _ReadOnlyQbittorrent(
        QbittorrentTorrentState(
            torrent_hash=torrent_hash,
            save_path="/downloads/target",
            content_path=None,
            state="stoppedDL",
            tags=(ownership_tag,),
            progress=0.75,
        )
    )
    binding = QbittorrentWriteBinding(
        downloader_id=downloader_id,
        downloader_version=3,
        binding_digest=binding_digest,
        path_mappings=path_mappings,
        capabilities=capabilities,
        adapter=qbit,
        data_root=data_root,
    )

    engine = create_sqlite_engine(tmp_path / "repair.db")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    task_id = new_uuid()
    unit_id = new_uuid()
    preflight_id = new_uuid()
    review_id = new_uuid()
    candidate_id = new_uuid()
    gate_id = new_uuid()
    now = utc_now()
    with factory() as session:
        session.add(
            UnpackTask(
                id=task_id,
                type="PACKAGE_UNPACK",
                source_downloader_id="source-downloader",
                source_hash="source-hash",
                normalized_unit_key="synthetic-repair",
                idempotency_key="a" * 64,
                status=TaskStatus.RETRY.value,
                trace_id=new_uuid(),
                checkpoint={},
                error_code=None,
                version=10,
                created_at=now,
                updated_at=now,
            )
        )
        session.flush()
        session.add(
            PreflightSnapshotRecord(
                id=preflight_id,
                task_id=task_id,
                task_version=7,
                normalized_unit_key="synthetic-repair",
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
                normalized_unit_key="synthetic-repair",
                source_root="source",
                source_inventory_digest=inventory_digest,
                kind="MOVIE",
                source_relative_path=source.name,
                length=source_item.length,
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
                normalized_unit_key="synthetic-repair",
                site_id="fake",
                torrent_id="42",
                display_name="Synthetic Repair",
                score=95.0,
                rejected=False,
                selected_for_verification=True,
                verification_level=VerificationLevel.CLIENT_CHECK_REQUIRED.value,
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
                task_version=7,
                eligible=True,
                client_check_required=True,
                verification_level=VerificationLevel.CLIENT_CHECK_REQUIRED.value,
                metainfo_digest=meta.metainfo_digest,
                blocked_reasons=[],
                gate_digest="c" * 64,
                payload={"review_version": 1},
                created_at=now,
            )
        )
        session.flush()
        plan_snapshot = ExecutionPlanSnapshot(
            task_id=task_id,
            task_version=7,
            task_unit_id=unit_id,
            execution_gate_id=gate_id,
            execution_gate_digest="c" * 64,
            preflight_snapshot_id=preflight_id,
            review_revision_id=review_id,
            candidate_id=candidate_id,
            source_inventory_digest=inventory_digest,
            metainfo_digest=meta.metainfo_digest,
            verification_level=VerificationLevel.CLIENT_CHECK_REQUIRED,
            client_check_required=True,
            source_root="source",
            target_root="target",
            target_device=target_root.stat().st_dev,
            target_downloader_id=downloader_id,
            target_downloader_version=3,
            target_downloader_binding_digest=binding_digest,
            target_remote_save_path="/downloads/target",
            actions=(
                ExecutionPlanAction(
                    torrent_path=source.name,
                    kind=ExecutionPlanActionKind.HARDLINK,
                    length=source_item.length,
                    source_relative_path=source.name,
                    source_snapshot=source_item.snapshot,
                ),
            ),
            create_directories=(),
            estimated_download_bytes_upper_bound=source_item.length,
            blocked_reasons=(),
            created_at=now,
        )
        plan, _ = TaskExecutionPlanRepository(session).create_or_get(plan_snapshot)

        add_id = new_uuid()
        verify_id = new_uuid()
        hardlink_id = new_uuid()
        state_snapshot = {
            "torrent_hash": torrent_hash,
            "save_path": "/downloads/target",
            "content_path": None,
            "state": "stoppedDL",
            "progress": 0.75,
            "ownership_tag": ownership_tag,
            "tags": [ownership_tag],
        }
        session.add_all(
            [
                OperationJournal(
                    id=add_id,
                    task_id=task_id,
                    idempotency_key="1" * 64,
                    operation_type=QBITTORRENT_ADD_OPERATION,
                    target={"downloader_id": downloader_id},
                    intent={
                        "execution_plan_id": plan.id,
                        "execution_plan_digest": plan.plan_digest,
                        "expected_metainfo_digest": meta.metainfo_digest,
                        "downloader_version": 3,
                    },
                    status=OperationStatus.APPLIED.value,
                    before_snapshot={"torrent_absent": True},
                    after_snapshot=state_snapshot,
                    created_at=now,
                    updated_at=now,
                ),
                OperationJournal(
                    id=verify_id,
                    task_id=task_id,
                    idempotency_key="2" * 64,
                    operation_type=QBITTORRENT_RECHECK_OPERATION,
                    target={"downloader_id": downloader_id, "torrent_hash": torrent_hash},
                    intent={
                        "execution_plan_id": plan.id,
                        "downloader_version": 3,
                        "qbit_add_journal_id": add_id,
                        "torrent_hash": torrent_hash,
                        "remote_save_path": "/downloads/target",
                        "ownership_tag": ownership_tag,
                    },
                    status=OperationStatus.APPLIED.value,
                    before_snapshot=state_snapshot,
                    after_snapshot=state_snapshot,
                    created_at=now,
                    updated_at=now,
                ),
            ]
        )
        target_stat = target.stat(follow_symlinks=False)
        session.add(
            OperationJournal(
                id=hardlink_id,
                task_id=task_id,
                idempotency_key="3" * 64,
                operation_type=CREATE_HARDLINK_OPERATION,
                target={"target_root": "target", "relative_path": source.name},
                intent={
                    "schema_version": FILESYSTEM_OPERATION_SCHEMA_VERSION,
                    "resource_kind": "hardlink",
                    "source_relative_path": f"source/{source.name}",
                    "source_snapshot": {
                        "device": source_item.snapshot.device,
                        "inode": source_item.snapshot.inode,
                        "size": source_item.snapshot.size,
                        "mtime_ns": source_item.snapshot.mtime_ns,
                        "file_type": source_item.snapshot.file_type,
                    },
                },
                status=OperationStatus.APPLIED.value,
                before_snapshot={"target_absent": True},
                after_snapshot={
                    "device": target_stat.st_dev,
                    "inode": target_stat.st_ino,
                    "size": target_stat.st_size,
                    "mtime_ns": target_stat.st_mtime_ns,
                    "file_type": "regular",
                    "link_count": target_stat.st_nlink,
                },
                created_at=now,
                updated_at=now,
            )
        )
        task = session.get(UnpackTask, task_id)
        assert task is not None
        task.checkpoint = {
            "schema_version": CLIENT_VERIFICATION_CHECKPOINT_SCHEMA_VERSION,
            "stage": TaskStatus.RETRY.value,
            "execution_plan_id": plan.id,
            "execution_plan_digest": plan.plan_digest,
            "target_downloader_id": downloader_id,
            "target_downloader_version": 3,
            "target_downloader_binding_digest": binding_digest,
            "target_remote_save_path": "/downloads/target",
            "downloader_kind": DownloaderKind.QBITTORRENT.value,
            "torrent_hash": torrent_hash,
            "remote_save_path": "/downloads/target",
            "ownership_tag": ownership_tag,
            "qbit_journal_id": add_id,
            "add_journal_id": add_id,
            "recheck_journal_id": verify_id,
            "verification_journal_id": verify_id,
            "client_state": "stoppedDL",
            "client_progress": 0.75,
            "checking_observed": True,
            "verification_outcome": "INCOMPLETE",
            "skip_checking": False,
        }
        session.commit()
        plan_id = plan.id

    service = TaskRepairPlanService(
        factory,
        _SiteProvider(_FakeSiteAdapter(torrent_content)),
        _DownloaderProvider(binding),
        data_root=data_root,
    )
    try:
        yield _RepairFixture(
            service=service,
            factory=factory,
            data_root=data_root,
            source=source,
            target=target,
            task_id=task_id,
            unit_id=unit_id,
            plan_id=plan_id,
            add_journal_id=add_id,
            verify_journal_id=verify_id,
            hardlink_journal_id=hardlink_id,
            ownership_tag=ownership_tag,
            qbit=qbit,
        )
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_repair_plan_uses_trusted_retry_journals_and_is_read_only(
    repair_fixture: _RepairFixture,
) -> None:
    before_source = repair_fixture.source.stat(follow_symlinks=False)
    before_target = repair_fixture.target.stat(follow_symlinks=False)
    with repair_fixture.factory() as session:
        before_task = session.get(UnpackTask, repair_fixture.task_id)
        assert before_task is not None
        checkpoint = dict(before_task.checkpoint)
        journal_statuses = {
            item.id: item.status
            for item in session.scalars(
                select(OperationJournal).where(OperationJournal.task_id == repair_fixture.task_id)
            )
        }

    result = await repair_fixture.service.generate(
        repair_fixture.unit_id,
        mode=RepairMode.AUTO_PIECE,
    )

    assert result.task_id == repair_fixture.task_id
    assert result.execution_plan_id == repair_fixture.plan_id
    assert result.evidence_source == "CLIENT_VERIFICATION_INCOMPLETE"
    assert result.plan.ready is True
    assert result.plan.execution_allowed is False
    assert result.plan.downloader_paused is True
    assert result.plan.isolation_bytes_required == repair_fixture.source.stat().st_size
    assert result.plan.affected_pieces[0].index == 0
    assert result.plan.affected_files[0].isolation_required is True
    assert RepairActionKind.ISOLATE_TARGET in {item.kind for item in result.plan.actions}
    assert RepairActionKind.REPAIR_PIECES in {item.kind for item in result.plan.actions}
    assert repair_fixture.qbit.write_calls == 0
    serialized = json.dumps(asdict(result), ensure_ascii=False, default=str)
    for forbidden in (
        repair_fixture.qbit.state.torrent_hash,
        repair_fixture.ownership_tag,
        repair_fixture.add_journal_id,
        repair_fixture.verify_journal_id,
        repair_fixture.hardlink_journal_id,
        str(repair_fixture.source),
    ):
        assert forbidden not in serialized

    after_source = repair_fixture.source.stat(follow_symlinks=False)
    after_target = repair_fixture.target.stat(follow_symlinks=False)
    assert (before_source.st_ino, before_source.st_size, before_source.st_mtime_ns) == (
        after_source.st_ino,
        after_source.st_size,
        after_source.st_mtime_ns,
    )
    assert (before_target.st_ino, before_target.st_size, before_target.st_mtime_ns) == (
        after_target.st_ino,
        after_target.st_size,
        after_target.st_mtime_ns,
    )
    with repair_fixture.factory() as session:
        task = session.get(UnpackTask, repair_fixture.task_id)
        assert task is not None
        assert task.checkpoint == checkpoint
        assert {
            item.id: item.status
            for item in session.scalars(
                select(OperationJournal).where(OperationJournal.task_id == repair_fixture.task_id)
            )
        } == journal_statuses


@pytest.mark.asyncio
async def test_repair_plan_requires_current_downloader_pause_before_hashing(
    repair_fixture: _RepairFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repair_fixture.qbit.state = replace(repair_fixture.qbit.state, state="downloading")

    def should_not_read_target(*args: object, **kwargs: object) -> object:
        raise AssertionError(f"未暂停时不应检查 target: {args!r}/{kwargs!r}")

    monkeypatch.setattr(
        repair_fixture.service._filesystem, "inspect_repair_target", should_not_read_target
    )
    with pytest.raises(ApplicationError) as failure:
        await repair_fixture.service.generate(repair_fixture.unit_id, mode=RepairMode.AUTO_PIECE)

    assert failure.value.code == "REPAIR_PLAN_DOWNLOADER_NOT_PAUSED"
    assert repair_fixture.qbit.write_calls == 0


@pytest.mark.asyncio
async def test_repair_plan_rejects_downloader_ownership_drift(
    repair_fixture: _RepairFixture,
) -> None:
    repair_fixture.qbit.state = replace(repair_fixture.qbit.state, tags=("external-owner",))

    with pytest.raises(ApplicationError) as failure:
        await repair_fixture.service.generate(repair_fixture.unit_id, mode=RepairMode.AUTO_PIECE)

    assert failure.value.code == "REPAIR_PLAN_DOWNLOADER_OWNERSHIP_UNPROVEN"
    assert repair_fixture.qbit.write_calls == 0


@pytest.mark.asyncio
async def test_repair_plan_requires_applied_hardlink_ownership(
    repair_fixture: _RepairFixture,
) -> None:
    with repair_fixture.factory() as session:
        journal = session.get(OperationJournal, repair_fixture.hardlink_journal_id)
        assert journal is not None
        journal.status = OperationStatus.RECONCILE_REQUIRED.value
        session.commit()

    with pytest.raises(ApplicationError) as failure:
        await repair_fixture.service.generate(repair_fixture.unit_id, mode=RepairMode.AUTO_PIECE)

    assert failure.value.code == "REPAIR_PLAN_TARGET_OWNERSHIP_UNPROVEN"
    assert repair_fixture.qbit.write_calls == 0


@pytest.mark.asyncio
async def test_repair_plan_rejects_add_journal_plan_digest_drift(
    repair_fixture: _RepairFixture,
) -> None:
    with repair_fixture.factory() as session:
        journal = session.get(OperationJournal, repair_fixture.add_journal_id)
        assert journal is not None
        intent = dict(journal.intent)
        intent["execution_plan_digest"] = "f" * 64
        journal.intent = intent
        session.commit()

    with pytest.raises(ApplicationError) as failure:
        await repair_fixture.service.generate(repair_fixture.unit_id, mode=RepairMode.AUTO_PIECE)

    assert failure.value.code == "REPAIR_PLAN_EVIDENCE_INVALID"
    assert repair_fixture.qbit.write_calls == 0


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
