import asyncio
import hashlib
import os
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import cast

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.downloaders import (
    QbittorrentWriteBinding,
    TransmissionWriteBinding,
)
from backend.app.application.sites import EnabledSiteAdapter
from backend.app.application.unpack_seeding import UnpackSeedingService
from backend.app.domain.downloader import PathMappingRule
from backend.app.domain.operation import OperationStatus
from backend.app.domain.site_adapter import SiteAdapter, TorrentPayload
from backend.app.domain.task_definition import TaskConflictPolicy, TaskStorageMode
from backend.app.domain.unpack import (
    UnpackExecutionStatus,
    UnpackItemStatus,
)
from backend.app.domain.unpack_execution_plan import (
    UnpackExecutionAction,
    UnpackExecutionActionKind,
    UnpackExecutionPlan,
    unpack_execution_plan_to_payload,
)
from backend.app.domain.verification import VerificationLevel
from backend.app.infrastructure.adapters.downloaders import (
    DownloaderAdapterError,
    QbittorrentAddRequest,
    QbittorrentAddResult,
    QbittorrentTorrentState,
    QbittorrentWriteAdapter,
    TransmissionAddRequest,
    TransmissionAddResult,
    TransmissionTorrentState,
    TransmissionWriteAdapter,
)
from backend.app.infrastructure.adapters.site_errors import SiteAdapterError
from backend.app.infrastructure.persistence.base import Base
from backend.app.infrastructure.persistence.models import (
    UnpackDefinition,
    UnpackExecution,
    UnpackExecutionItem,
    UnpackExternalOperationJournal,
    UnpackMatchCandidate,
    utc_now,
)
from backend.app.infrastructure.source_inventory import current_file_snapshot
from backend.app.infrastructure.torrent_parser import parse_torrent


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


def _torrent(name: str, content: bytes) -> bytes:
    piece_length = 4
    pieces = b"".join(
        hashlib.sha1(content[offset : offset + piece_length]).digest()
        for offset in range(0, len(content), piece_length)
    )
    return _bencode(
        {
            b"info": {
                b"length": len(content),
                b"name": name.encode(),
                b"piece length": piece_length,
                b"pieces": pieces,
            }
        }
    )


class _FakeSite:
    def __init__(self, torrent: bytes) -> None:
        self._torrent = torrent
        self.calls: list[str] = []
        self.fail_fetch = False
        self.delay_seconds = 0.0

    async def fetch_torrent(self, torrent_id: str) -> TorrentPayload:
        assert torrent_id == "torrent-1"
        self.calls.append(torrent_id)
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        if self.fail_fetch:
            raise SiteAdapterError("SITE_UNAVAILABLE", "synthetic maintenance", retryable=True)
        return TorrentPayload("fake-site", torrent_id, self._torrent)


class _SiteProvider:
    def __init__(self, site: _FakeSite) -> None:
        self._site = site

    def enabled_adapters(self) -> tuple[EnabledSiteAdapter, ...]:
        return (
            EnabledSiteAdapter(
                config_id="site-config-1",
                config_version=7,
                site_id="fake-site",
                adapter=cast(SiteAdapter, self._site),
            ),
        )


class _FakeQb:
    def __init__(
        self,
        factory: sessionmaker[Session],
        torrent: bytes,
        *,
        lose_add_response: bool = False,
        start_behavior: str = "success",
    ) -> None:
        self._factory = factory
        self._meta = parse_torrent(torrent)
        self.lose_add_response = lose_add_response
        self.start_behavior = start_behavior
        self.lose_verify_response = False
        self.state: QbittorrentTorrentState | None = None
        self.calls: list[str] = []

    def _assert_intent(self, operation_type: str) -> None:
        with self._factory() as session:
            row = session.scalar(
                select(UnpackExternalOperationJournal).where(
                    UnpackExternalOperationJournal.operation_type == operation_type
                )
            )
            assert row is not None
            assert row.status == OperationStatus.INTENT_RECORDED.value

    async def add_torrent(self, request: QbittorrentAddRequest) -> QbittorrentAddResult:
        self._assert_intent("UNPACK_EXEC_QBITTORRENT_ADD")
        self.calls.append("add")
        torrent_hash = self._meta.v1_info_hash
        assert torrent_hash is not None
        assert request.paused is True
        self.state = QbittorrentTorrentState(
            torrent_hash=torrent_hash,
            save_path=request.save_path,
            content_path=None,
            state="stoppedUP" if request.skip_checking else "stoppedDL",
            tags=request.tags,
            progress=1.0 if request.skip_checking else 0.0,
        )
        if self.lose_add_response:
            raise DownloaderAdapterError("DOWNLOADER_TIMEOUT", "synthetic lost add response")
        return QbittorrentAddResult(1, 0, 0, (torrent_hash,), "2.15.1")

    async def get_torrents(
        self,
        torrent_hashes: tuple[str, ...],
    ) -> tuple[QbittorrentTorrentState, ...]:
        if self.state is None or self.state.torrent_hash not in torrent_hashes:
            return ()
        return (self.state,)

    async def recheck_torrent(self, torrent_hash: str) -> None:
        self._assert_intent("UNPACK_EXEC_QBITTORRENT_RECHECK")
        assert self.state is not None and self.state.torrent_hash == torrent_hash
        self.calls.append("verify")
        if self.lose_verify_response:
            raise DownloaderAdapterError(
                "DOWNLOADER_TIMEOUT",
                "synthetic lost verify response before any effect",
            )
        self.state = replace(self.state, state="stoppedUP", progress=1.0)

    async def start_torrent(self, torrent_hash: str) -> None:
        self._assert_intent("UNPACK_EXEC_QBITTORRENT_START")
        assert self.state is not None and self.state.torrent_hash == torrent_hash
        self.calls.append("start")
        if self.start_behavior == "accepted_no_state":
            return
        if self.start_behavior == "lost_before_effect":
            raise DownloaderAdapterError(
                "DOWNLOADER_TIMEOUT",
                "synthetic lost start response",
            )
        self.state = replace(self.state, state="uploading", progress=1.0)


class _FakeTransmission:
    def __init__(
        self,
        factory: sessionmaker[Session],
        torrent: bytes,
    ) -> None:
        self._factory = factory
        self._meta = parse_torrent(torrent)
        self.state: TransmissionTorrentState | None = None
        self.calls: list[str] = []

    def _assert_intent(self, operation_type: str) -> None:
        with self._factory() as session:
            row = session.scalar(
                select(UnpackExternalOperationJournal).where(
                    UnpackExternalOperationJournal.operation_type == operation_type
                )
            )
            assert row is not None
            assert row.status == OperationStatus.INTENT_RECORDED.value

    async def add_torrent(self, request: TransmissionAddRequest) -> TransmissionAddResult:
        self._assert_intent("UNPACK_EXEC_TRANSMISSION_ADD")
        self.calls.append("add")
        torrent_hash = self._meta.v1_info_hash
        assert torrent_hash is not None
        assert request.paused is True
        self.state = TransmissionTorrentState(
            torrent_hash=torrent_hash,
            download_dir=request.save_path,
            status=0,
            labels=request.labels,
            percent_done=0.0,
            recheck_progress=0.0,
        )
        return TransmissionAddResult(torrent_hash=torrent_hash, duplicate=False)

    async def get_torrents(
        self,
        torrent_hashes: tuple[str, ...],
    ) -> tuple[TransmissionTorrentState, ...]:
        if self.state is None or self.state.torrent_hash not in torrent_hashes:
            return ()
        return (self.state,)

    async def verify_torrent(self, torrent_hash: str) -> None:
        self._assert_intent("UNPACK_EXEC_TRANSMISSION_VERIFY")
        assert self.state is not None and self.state.torrent_hash == torrent_hash
        self.calls.append("verify")
        self.state = replace(
            self.state,
            status=0,
            percent_done=1.0,
            recheck_progress=1.0,
        )

    async def start_torrent(self, torrent_hash: str) -> None:
        self._assert_intent("UNPACK_EXEC_TRANSMISSION_START")
        assert self.state is not None and self.state.torrent_hash == torrent_hash
        self.calls.append("start")
        self.state = replace(self.state, status=6, percent_done=1.0)


class _DownloaderProvider:
    def __init__(
        self,
        binding: QbittorrentWriteBinding | TransmissionWriteBinding,
    ) -> None:
        self._binding = binding

    def write_binding(
        self,
        downloader_id: str,
    ) -> QbittorrentWriteBinding | TransmissionWriteBinding:
        assert downloader_id == "downloader-target"
        return self._binding


def _fixture(
    tmp_path: Path,
    *,
    kind: str,
    lose_add_response: bool = False,
    force_client_check: bool = False,
    start_behavior: str = "success",
) -> tuple[
    UnpackSeedingService,
    sessionmaker[Session],
    _FakeQb | _FakeTransmission,
    bytes,
]:
    data_root = tmp_path / "data"
    source = data_root / "source" / "Movie.mkv"
    output = data_root / "seeding"
    source.parent.mkdir(parents=True)
    output.mkdir()
    content = b"abcdefgh"
    source.write_bytes(content)
    target = output / "Movie.mkv"
    os.link(source, target)
    snapshot = current_file_snapshot(source)
    torrent = _torrent("Movie.mkv", content)
    meta = parse_torrent(torrent)
    assert meta.v1_info_hash is not None

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory: sessionmaker[Session] = sessionmaker(
        bind=engine,
        expire_on_commit=False,
        autoflush=False,
    )
    now = utc_now()
    client_check_required = kind == "transmission" or force_client_check
    plan = UnpackExecutionPlan(
        item_id="item-1",
        item_version_before=4,
        candidate_id="candidate-1",
        candidate_generation=1,
        metainfo_digest=meta.metainfo_digest,
        verification_level=VerificationLevel.FULL_VERIFIED,
        output_directory=output.as_posix(),
        storage_mode=TaskStorageMode.HARDLINK,
        conflict_policy=TaskConflictPolicy.VERIFY_REUSE_OR_STOP,
        target_device=output.stat(follow_symlinks=False).st_dev,
        target_downloader_id="downloader-target",
        target_downloader_version=3,
        target_downloader_binding_digest="b" * 64,
        target_remote_save_path="/downloads/seeding",
        client_check_required=client_check_required,
        actions=(
            UnpackExecutionAction(
                torrent_path="Movie.mkv",
                kind=UnpackExecutionActionKind.MATERIALIZE,
                length=snapshot.size,
                source_path=source.as_posix(),
                source_snapshot=snapshot,
            ),
        ),
        create_directories=(),
        blocked_reasons=(),
        created_at=now,
    )

    with factory() as session:
        session.add(
            UnpackDefinition(
                id="definition-1",
                name="seeding",
                trigger_kind="MANUAL",
                status="PENDING_EXECUTION",
                source_kind="DIRECTORY",
                execution_scope_kind="ALL_MATCHING_MEDIA",
                source_config={"directory_path": source.parent.as_posix()},
                file_filter={"extensions": [".mkv"]},
                site_ids=["site-config-1"],
                output_config={
                    "output_directory": output.as_posix(),
                    "storage_mode": "HARDLINK",
                    "conflict_policy": "VERIFY_REUSE_OR_STOP",
                    "target_downloader_id": "downloader-target",
                },
                retry_enabled=True,
                max_retries=3,
                auto_match_threshold_bps=10000,
                version=1,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            UnpackExecution(
                id="execution-1",
                definition_id="definition-1",
                trigger="MANUAL",
                status=UnpackExecutionStatus.EXECUTING.value,
                config_snapshot={},
                discovery_complete=True,
                total_count=1,
                matched_auto_count=1,
                review_count=0,
                content_verified_count=1,
                content_mismatch_count=0,
                timeout_count=0,
                error_count=0,
                completed_count=0,
                started_at=now,
                version=1,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            UnpackExecutionItem(
                id="item-1",
                execution_id="execution-1",
                source_object_key="source-1",
                source_snapshot={},
                media_identity={},
                status=UnpackItemStatus.EXECUTING.value,
                selected_candidate_id="candidate-1",
                candidate_generation=1,
                retry_count=0,
                content_verification_level=VerificationLevel.FULL_VERIFIED.value,
                torrent_metainfo_digest=meta.metainfo_digest,
                execution_plan=unpack_execution_plan_to_payload(plan),
                execution_plan_digest=plan.plan_digest,
                execution_plan_created_at=now,
                execution_state={
                    "stage": "FILES_MATERIALIZED",
                    "plan_digest": plan.plan_digest,
                },
                version=8,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            UnpackMatchCandidate(
                id="candidate-1",
                item_id="item-1",
                generation=1,
                site_id="site-config-1",
                candidate_key="torrent-1",
                title="Movie",
                size_bytes=len(content),
                score_bps=10000,
                is_exact_match=True,
                evidence={},
                verification_status="VERIFIED",
                verification_level=VerificationLevel.FULL_VERIFIED.value,
                metainfo_digest=meta.metainfo_digest,
                raw_ref={
                    "torrent_id": "torrent-1",
                    "adapter_site_id": "fake-site",
                    "site_config_id": "site-config-1",
                    "site_config_version": 7,
                },
                created_at=now,
            )
        )
        session.commit()

    site = _FakeSite(torrent)
    if kind == "qb":
        fake: _FakeQb | _FakeTransmission = _FakeQb(
            factory,
            torrent,
            lose_add_response=lose_add_response,
            start_behavior=start_behavior,
        )
        binding: QbittorrentWriteBinding | TransmissionWriteBinding = QbittorrentWriteBinding(
            downloader_id="downloader-target",
            downloader_version=3,
            binding_digest="b" * 64,
            path_mappings=(PathMappingRule("/downloads", data_root.as_posix()),),
            capabilities={"supports_skip_checking": True, "api_version": "2.15.1"},
            adapter=cast(QbittorrentWriteAdapter, fake),
            data_root=data_root,
        )
    else:
        fake = _FakeTransmission(factory, torrent)
        binding = TransmissionWriteBinding(
            downloader_id="downloader-target",
            downloader_version=3,
            binding_digest="b" * 64,
            path_mappings=(PathMappingRule("/downloads", data_root.as_posix()),),
            capabilities={
                "supports_skip_checking": False,
                "supports_force_recheck": True,
                "api_version": "4.1.3",
            },
            adapter=cast(TransmissionWriteAdapter, fake),
            data_root=data_root,
        )

    service = UnpackSeedingService(
        factory,
        _SiteProvider(site),
        _DownloaderProvider(binding),
    )
    return service, factory, fake, torrent


def test_qb_full_verified_skip_checking_adds_paused_then_starts(tmp_path: Path) -> None:
    service, factory, fake, _torrent_bytes = _fixture(tmp_path, kind="qb")
    qb = cast(_FakeQb, fake)

    report = asyncio.run(service.advance_next_batch("execution-1"))

    assert report.completed_count == 1
    assert qb.calls == ["add", "start"]
    assert qb.state is not None and qb.state.seeding
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        execution = session.get(UnpackExecution, "execution-1")
        assert item is not None and execution is not None
        assert item.status == UnpackItemStatus.COMPLETED.value
        assert item.execution_state is not None
        assert item.execution_state["stage"] == "COMPLETED"
        assert execution.status == UnpackExecutionStatus.COMPLETED.value
        journals = session.scalars(
            select(UnpackExternalOperationJournal).where(
                UnpackExternalOperationJournal.item_id == "item-1"
            )
        ).all()
        assert {row.operation_type for row in journals} == {
            "UNPACK_EXEC_QBITTORRENT_ADD",
            "UNPACK_EXEC_QBITTORRENT_START",
        }
        assert all(row.status == OperationStatus.APPLIED.value for row in journals)


def test_final_seeding_site_timeout_is_recorded_without_side_effects(tmp_path: Path) -> None:
    service, factory, fake, _torrent_bytes = _fixture(tmp_path, kind="transmission")
    site = cast(_SiteProvider, service._site_provider)._site
    site.delay_seconds = 0.1
    service._fetch_timeout_seconds = 0.001

    report = asyncio.run(service.advance_next_batch("execution-1"))

    assert report.processed_count == 1
    assert report.error_count == 1
    assert fake.calls == []
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == UnpackItemStatus.EXECUTION_ERROR.value
        assert item.last_error_code == "UNPACK_SEED_TIMEOUT"
        assert session.scalars(select(UnpackExternalOperationJournal)).all() == []


def test_transmission_resumes_after_add_when_site_goes_down(tmp_path: Path) -> None:
    service, factory, fake, _torrent_bytes = _fixture(tmp_path, kind="transmission")
    site = cast(_SiteProvider, service._site_provider)._site
    first = asyncio.run(service.advance_next_batch("execution-1"))
    assert first.client_verifying_count == 1
    assert site.calls == ["torrent-1"]

    site.fail_fetch = True
    second = asyncio.run(service.advance_next_batch("execution-1"))

    assert second.completed_count == 1
    assert site.calls == ["torrent-1"]
    assert fake.calls == ["add", "verify", "start"]
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == UnpackItemStatus.COMPLETED.value


def test_qb_resumes_client_check_after_add_when_site_goes_down(tmp_path: Path) -> None:
    service, factory, fake, _torrent_bytes = _fixture(tmp_path, kind="qb", force_client_check=True)
    qb = cast(_FakeQb, fake)
    site = cast(_SiteProvider, service._site_provider)._site
    first = asyncio.run(service.advance_next_batch("execution-1"))
    assert first.client_verifying_count == 1
    assert site.calls == ["torrent-1"]

    site.fail_fetch = True
    second = asyncio.run(service.advance_next_batch("execution-1"))

    assert second.completed_count == 1
    assert site.calls == ["torrent-1"]
    assert qb.calls == ["add", "verify", "start"]
    assert qb.state is not None and qb.state.seeding
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == UnpackItemStatus.COMPLETED.value


def test_transmission_resume_rejects_tampered_add_journal(tmp_path: Path) -> None:
    service, factory, fake, _torrent_bytes = _fixture(tmp_path, kind="transmission")
    site = cast(_SiteProvider, service._site_provider)._site
    first = asyncio.run(service.advance_next_batch("execution-1"))
    assert first.client_verifying_count == 1
    with factory() as session:
        row = session.scalar(
            select(UnpackExternalOperationJournal).where(
                UnpackExternalOperationJournal.operation_type == "UNPACK_EXEC_TRANSMISSION_ADD"
            )
        )
        assert row is not None
        row.after_snapshot = {**(row.after_snapshot or {}), "ownership_tag": "unowned"}
        session.commit()
    site.fail_fetch = True

    second = asyncio.run(service.advance_next_batch("execution-1"))

    assert second.error_count == 1
    assert site.calls == ["torrent-1"]
    assert fake.calls == ["add"]
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == UnpackItemStatus.EXECUTION_ERROR.value
        assert item.last_error_code == "UNPACK_SEED_RECONCILE_REQUIRED"


def test_transmission_requires_explicit_verify_before_start(tmp_path: Path) -> None:
    service, factory, fake, _torrent_bytes = _fixture(tmp_path, kind="transmission")
    transmission = cast(_FakeTransmission, fake)

    first = asyncio.run(service.advance_next_batch("execution-1"))
    assert first.client_verifying_count == 1
    assert transmission.calls == ["add"]

    second = asyncio.run(service.advance_next_batch("execution-1"))

    assert second.completed_count == 1
    assert transmission.calls == ["add", "verify", "start"]
    assert transmission.state is not None and transmission.state.seeding
    with factory() as session:
        journals = session.scalars(
            select(UnpackExternalOperationJournal).where(
                UnpackExternalOperationJournal.item_id == "item-1"
            )
        ).all()
        assert {row.operation_type for row in journals} == {
            "UNPACK_EXEC_TRANSMISSION_ADD",
            "UNPACK_EXEC_TRANSMISSION_VERIFY",
            "UNPACK_EXEC_TRANSMISSION_START",
        }
        assert all(row.status == OperationStatus.APPLIED.value for row in journals)


def test_qb_client_check_plan_requires_explicit_recheck_before_start(
    tmp_path: Path,
) -> None:
    service, factory, fake, _torrent_bytes = _fixture(
        tmp_path,
        kind="qb",
        force_client_check=True,
    )
    qb = cast(_FakeQb, fake)

    first = asyncio.run(service.advance_next_batch("execution-1"))
    assert first.client_verifying_count == 1
    assert qb.calls == ["add"]

    second = asyncio.run(service.advance_next_batch("execution-1"))

    assert second.completed_count == 1
    assert qb.calls == ["add", "verify", "start"]
    assert qb.state is not None and qb.state.seeding
    with factory() as session:
        journals = session.scalars(
            select(UnpackExternalOperationJournal).where(
                UnpackExternalOperationJournal.item_id == "item-1"
            )
        ).all()
        assert {row.operation_type for row in journals} == {
            "UNPACK_EXEC_QBITTORRENT_ADD",
            "UNPACK_EXEC_QBITTORRENT_RECHECK",
            "UNPACK_EXEC_QBITTORRENT_START",
        }
        assert all(row.status == OperationStatus.APPLIED.value for row in journals)


def test_preexisting_unowned_same_hash_is_never_claimed(tmp_path: Path) -> None:
    service, factory, fake, torrent = _fixture(tmp_path, kind="qb")
    qb = cast(_FakeQb, fake)
    meta = parse_torrent(torrent)
    assert meta.v1_info_hash is not None
    qb.state = QbittorrentTorrentState(
        torrent_hash=meta.v1_info_hash,
        save_path="/downloads/seeding",
        content_path=None,
        state="stoppedUP",
        tags=(),
        progress=1.0,
    )

    report = asyncio.run(service.advance_next_batch("execution-1"))

    assert report.error_count == 1
    assert qb.calls == []
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == UnpackItemStatus.EXECUTION_ERROR.value
        assert item.last_error_code == "UNPACK_SEED_TORRENT_ALREADY_EXISTS"
        journals = session.scalars(select(UnpackExternalOperationJournal)).all()
        assert journals == []


def test_qb_lost_add_response_recovers_only_from_owned_real_state(tmp_path: Path) -> None:
    service, factory, fake, _torrent_bytes = _fixture(
        tmp_path,
        kind="qb",
        lose_add_response=True,
    )
    qb = cast(_FakeQb, fake)

    report = asyncio.run(service.advance_next_batch("execution-1"))

    assert report.completed_count == 1
    assert qb.calls == ["add", "start"]
    with factory() as session:
        add = session.scalar(
            select(UnpackExternalOperationJournal).where(
                UnpackExternalOperationJournal.operation_type == "UNPACK_EXEC_QBITTORRENT_ADD"
            )
        )
        assert add is not None
        assert add.status == OperationStatus.APPLIED.value
        assert add.after_snapshot is not None
        assert add.after_snapshot["ownership_tag"].startswith("packbreaker-unpack-")


def test_qb_accepted_start_request_is_polled_without_replay(tmp_path: Path) -> None:
    service, factory, fake, _torrent_bytes = _fixture(
        tmp_path,
        kind="qb",
        start_behavior="accepted_no_state",
    )
    qb = cast(_FakeQb, fake)

    first = asyncio.run(service.advance_next_batch("execution-1"))
    second = asyncio.run(service.advance_next_batch("execution-1"))

    assert first.completed_count == 0
    assert second.completed_count == 0
    assert qb.calls == ["add", "start"]
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == UnpackItemStatus.EXECUTING.value
        assert item.execution_state is not None
        assert item.execution_state["stage"] == "STARTING"
        start = session.scalar(
            select(UnpackExternalOperationJournal).where(
                UnpackExternalOperationJournal.operation_type == "UNPACK_EXEC_QBITTORRENT_START"
            )
        )
        assert start is not None
        assert start.status == OperationStatus.INTENT_RECORDED.value
        assert start.after_snapshot is not None
        assert start.after_snapshot["request_accepted"] is True


def test_qb_starting_checkpoint_with_site_offline_does_not_replay_start(
    tmp_path: Path,
) -> None:
    service, factory, fake, _torrent_bytes = _fixture(
        tmp_path, kind="qb", start_behavior="accepted_no_state"
    )
    qb = cast(_FakeQb, fake)
    site = cast(_SiteProvider, service._site_provider)._site
    first = asyncio.run(service.advance_next_batch("execution-1"))
    assert first.completed_count == 0
    assert qb.calls == ["add", "start"]

    site.fail_fetch = True
    second = asyncio.run(service.advance_next_batch("execution-1"))

    assert second.error_count == 0
    assert second.completed_count == 0
    assert site.calls == ["torrent-1"]
    assert qb.calls == ["add", "start"]
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == UnpackItemStatus.EXECUTING.value
        assert item.execution_state is not None
        assert item.execution_state["stage"] == "STARTING"


def test_qb_lost_start_response_without_observed_effect_requires_reconcile(
    tmp_path: Path,
) -> None:
    service, factory, fake, _torrent_bytes = _fixture(
        tmp_path,
        kind="qb",
        start_behavior="lost_before_effect",
    )
    qb = cast(_FakeQb, fake)

    report = asyncio.run(service.advance_next_batch("execution-1"))

    assert report.error_count == 1
    assert qb.calls == ["add", "start"]
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == UnpackItemStatus.EXECUTION_ERROR.value
        start = session.scalar(
            select(UnpackExternalOperationJournal).where(
                UnpackExternalOperationJournal.operation_type == "UNPACK_EXEC_QBITTORRENT_START"
            )
        )
        assert start is not None
        assert start.status == OperationStatus.RECONCILE_REQUIRED.value
        assert start.last_error_code == "UNPACK_SEED_START_RESULT_UNKNOWN"


def test_verified_checkpoint_without_add_journal_is_blocked(tmp_path: Path) -> None:
    service, factory, fake, torrent = _fixture(tmp_path, kind="qb")
    qb = cast(_FakeQb, fake)
    info_hash = parse_torrent(torrent).v1_info_hash
    assert info_hash is not None
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        item.execution_state = {
            "stage": "CLIENT_VERIFIED",
            "plan_digest": item.execution_plan_digest,
        }
        assert item.execution_plan_digest is not None
        owner = hashlib.sha256(f"{item.id}:{item.execution_plan_digest}".encode()).hexdigest()[:16]
        session.commit()
    qb.state = QbittorrentTorrentState(
        torrent_hash=info_hash,
        save_path="/downloads/seeding",
        content_path=None,
        state="stoppedUP",
        tags=(f"packbreaker-unpack-{owner}",),
        progress=1.0,
    )

    report = asyncio.run(service.advance_next_batch("execution-1"))

    assert report.error_count == 1
    assert qb.calls == []
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.last_error_code == "UNPACK_SEED_RECONCILE_REQUIRED"


def test_transmission_client_verified_without_verify_journal_is_blocked(
    tmp_path: Path,
) -> None:
    service, factory, fake, _ = _fixture(tmp_path, kind="transmission")
    transmission = cast(_FakeTransmission, fake)
    asyncio.run(service.advance_next_batch("execution-1"))
    assert transmission.state is not None
    transmission.state = replace(
        transmission.state,
        status=0,
        percent_done=1.0,
        recheck_progress=1.0,
    )
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        item.status = UnpackItemStatus.EXECUTING.value
        item.execution_state = {
            "stage": "CLIENT_VERIFIED",
            "plan_digest": item.execution_plan_digest,
        }
        session.commit()

    report = asyncio.run(service.advance_next_batch("execution-1"))

    assert report.error_count == 1
    assert transmission.calls == ["add"]
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.last_error_code == "UNPACK_SEED_RECONCILE_REQUIRED"


def test_seeding_state_without_start_journal_cannot_complete(tmp_path: Path) -> None:
    service, factory, fake, _ = _fixture(tmp_path, kind="qb")
    qb = cast(_FakeQb, fake)
    asyncio.run(service.advance_next_batch("execution-1"))
    assert qb.state is not None and qb.state.seeding
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        item.status = UnpackItemStatus.EXECUTING.value
        item.execution_state = {
            "stage": "STARTING",
            "plan_digest": item.execution_plan_digest,
        }
        start = session.scalar(
            select(UnpackExternalOperationJournal).where(
                UnpackExternalOperationJournal.operation_type == "UNPACK_EXEC_QBITTORRENT_START"
            )
        )
        assert start is not None
        session.delete(start)
        session.commit()

    report = asyncio.run(service.advance_next_batch("execution-1"))

    assert report.completed_count == 0
    assert report.error_count == 1
    assert qb.calls == ["add", "start"]
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.last_error_code == "UNPACK_SEED_RECONCILE_REQUIRED"


def test_lost_verify_response_cannot_use_preexisting_100_percent_progress(
    tmp_path: Path,
) -> None:
    service, factory, fake, _ = _fixture(
        tmp_path,
        kind="qb",
        force_client_check=True,
    )
    qb = cast(_FakeQb, fake)
    first = asyncio.run(service.advance_next_batch("execution-1"))
    assert first.client_verifying_count == 1
    assert qb.state is not None
    qb.state = replace(qb.state, state="stoppedUP", progress=1.0)
    qb.lose_verify_response = True

    result = asyncio.run(service.advance_next_batch("execution-1"))

    assert result.error_count == 1
    assert result.completed_count == 0
    assert qb.calls == ["add", "verify"]
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == UnpackItemStatus.EXECUTION_ERROR.value
        verify_journal = session.scalar(
            select(UnpackExternalOperationJournal).where(
                UnpackExternalOperationJournal.operation_type == "UNPACK_EXEC_QBITTORRENT_RECHECK"
            )
        )
        assert verify_journal is not None
        assert verify_journal.status == OperationStatus.RECONCILE_REQUIRED.value
        assert verify_journal.last_error_code == "UNPACK_SEED_VERIFY_RESULT_UNKNOWN"
