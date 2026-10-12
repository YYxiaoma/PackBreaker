import asyncio
import hashlib
import os
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.downloaders import (
    QbittorrentWriteBinding,
    TransmissionWriteBinding,
)
from backend.app.application.errors import ApplicationError
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


@pytest.mark.parametrize(
    ("current_status", "current_stage"),
    (
        (UnpackItemStatus.COMPLETED, "COMPLETED"),
        (UnpackItemStatus.EXECUTION_ERROR, "FILES_MATERIALIZED"),
        (UnpackItemStatus.EXECUTING, "CLIENT_VERIFIED"),
    ),
)
def test_stale_stage_write_cannot_overwrite_newer_item_checkpoint(
    tmp_path: Path,
    current_status: UnpackItemStatus,
    current_stage: str,
) -> None:
    service, factory, fake, torrent = _fixture(tmp_path, kind="transmission")
    prepared = asyncio.run(service._prepare("item-1"))
    infohash = parse_torrent(torrent).v1_info_hash
    assert infohash is not None
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        item.status = current_status.value
        item.execution_state = {
            "stage": current_stage,
            "plan_digest": prepared.plan.plan_digest,
            "torrent_hash": infohash,
            "ownership_tag": prepared.ownership_tag,
        }
        item.version += 1
        session.commit()
        original_version = item.version
        original_state = dict(item.execution_state)

    with pytest.raises(ApplicationError):
        service._set_stage(
            prepared,
            "CLIENT_VERIFYING",
            status=UnpackItemStatus.CLIENT_VERIFYING,
            torrent_hash=infohash,
        )
    assert fake.calls == []
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == current_status.value
        assert item.execution_state == original_state
        assert item.version == original_version


@pytest.mark.parametrize(
    ("current_status", "current_stage"),
    (
        (UnpackItemStatus.EXECUTION_ERROR, "STARTING"),
        (UnpackItemStatus.COMPLETED, "COMPLETED"),
        (UnpackItemStatus.EXECUTING, "FILES_MATERIALIZED"),
    ),
)
def test_stale_completion_cannot_override_terminal_or_unverified_checkpoint(
    tmp_path: Path,
    current_status: UnpackItemStatus,
    current_stage: str,
) -> None:
    service, factory, fake, torrent = _fixture(tmp_path, kind="transmission")
    prepared = asyncio.run(service._prepare("item-1"))
    infohash = parse_torrent(torrent).v1_info_hash
    assert infohash is not None
    state = TransmissionTorrentState(
        torrent_hash=infohash,
        download_dir=prepared.plan.target_remote_save_path,
        status=6,
        labels=(prepared.ownership_tag,),
        percent_done=1.0,
        recheck_progress=1.0,
    )
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        item.status = current_status.value
        item.execution_state = {
            "stage": current_stage,
            "plan_digest": prepared.plan.plan_digest,
            "torrent_hash": infohash,
            "ownership_tag": prepared.ownership_tag,
        }
        item.last_error_code = (
            "UNPACK_SEED_RECONCILE_REQUIRED"
            if current_status == UnpackItemStatus.EXECUTION_ERROR
            else None
        )
        item.version += 1
        session.commit()
        original_version = item.version
        original_state = dict(item.execution_state)

    with pytest.raises(ApplicationError):
        service._mark_completed(prepared, state)
    assert fake.calls == []
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == current_status.value
        assert item.execution_state == original_state
        assert item.version == original_version
        assert item.last_error_code == (
            "UNPACK_SEED_RECONCILE_REQUIRED"
            if current_status == UnpackItemStatus.EXECUTION_ERROR
            else None
        )


@pytest.mark.parametrize("kind", ("qb", "transmission"))
@pytest.mark.parametrize("race_at", ("add", "start"))
def test_batch_stale_worker_does_not_change_newer_terminal_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, race_at: str
) -> None:
    service, factory, fake, torrent = _fixture(tmp_path, kind=kind)
    infohash = parse_torrent(torrent).v1_info_hash
    assert infohash is not None
    prepared = asyncio.run(service._prepare("item-1"))
    # Fake clients deliberately have distinct ADD request types; this hook
    # intercepts either method without changing the adapter interfaces.
    original = cast(Any, fake.add_torrent if race_at == "add" else fake.start_torrent)
    terminal = UnpackItemStatus.COMPLETED if race_at == "add" else UnpackItemStatus.EXECUTION_ERROR
    terminal_stage = "COMPLETED" if race_at == "add" else "STARTING"
    terminal_code = None if race_at == "add" else "UNPACK_SEED_RECONCILE_REQUIRED"

    def concurrent_terminal_commit() -> None:
        with factory() as session:
            item = session.get(UnpackExecutionItem, "item-1")
            execution = session.get(UnpackExecution, "execution-1")
            assert item is not None and execution is not None
            item.status = terminal.value
            item.execution_state = {
                "stage": terminal_stage,
                "plan_digest": prepared.plan.plan_digest,
                "torrent_hash": infohash,
                "ownership_tag": prepared.ownership_tag,
            }
            item.last_error_code = terminal_code
            item.version += 1
            execution.status = (
                UnpackExecutionStatus.COMPLETED.value
                if race_at == "add"
                else UnpackExecutionStatus.COMPLETED_WITH_ERRORS.value
            )
            execution.error_count = int(race_at == "start")
            execution.completed_count = int(race_at == "add")
            session.commit()

    async def add_with_race(request: QbittorrentAddRequest | TransmissionAddRequest) -> Any:
        result = await original(request)
        concurrent_terminal_commit()
        return result

    async def start_with_race(torrent_hash: str) -> None:
        await original(torrent_hash)
        concurrent_terminal_commit()

    monkeypatch.setattr(
        fake,
        "add_torrent" if race_at == "add" else "start_torrent",
        add_with_race if race_at == "add" else start_with_race,
    )
    if race_at == "start" and kind == "transmission":
        first = asyncio.run(service.advance_next_batch("execution-1"))
        assert first.client_verifying_count == 1

    report = asyncio.run(service.advance_next_batch("execution-1"))
    assert report.error_count == int(race_at == "start")
    assert report.completed_count == int(race_at == "add")
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        execution = session.get(UnpackExecution, "execution-1")
        assert item is not None and execution is not None
        assert item.status == terminal.value
        assert dict(item.execution_state or {}).get("stage") == terminal_stage
        assert item.last_error_code == terminal_code
        assert execution.status == (
            UnpackExecutionStatus.COMPLETED.value
            if race_at == "add"
            else UnpackExecutionStatus.COMPLETED_WITH_ERRORS.value
        )


@pytest.mark.parametrize("kind", ("qb", "transmission"))
@pytest.mark.parametrize(
    "tamper",
    (
        "start_reconcile",
        "start_missing",
        "start_wrong_hash",
        "start_bad_after",
        "start_bad_before",
        "add_reconcile",
        "verify_reconcile",
        "verify_wrong_ownership",
    ),
)
def test_final_completion_requires_current_atomic_operation_proofs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    tamper: str,
) -> None:
    # All three RPCs and the initial journal checks finished, but another
    # worker changed a journal before the item COMPLETED commit.
    service, factory, fake, _ = _fixture(tmp_path, kind=kind, force_client_check=True)
    first = asyncio.run(service.advance_next_batch("execution-1"))
    assert first.client_verifying_count == 1
    original_complete = service._mark_completed
    operation = (
        {
            "add": "UNPACK_EXEC_QBITTORRENT_ADD",
            "verify": "UNPACK_EXEC_QBITTORRENT_RECHECK",
            "start": "UNPACK_EXEC_QBITTORRENT_START",
        }
        if kind == "qb"
        else {
            "add": "UNPACK_EXEC_TRANSMISSION_ADD",
            "verify": "UNPACK_EXEC_TRANSMISSION_VERIFY",
            "start": "UNPACK_EXEC_TRANSMISSION_START",
        }
    )
    changed = False

    def completion_after_other_worker_change(*args: Any, **kwargs: Any) -> None:
        nonlocal changed
        with factory() as session:
            which = tamper.split("_", 1)[0]
            journal = session.scalar(
                select(UnpackExternalOperationJournal).where(
                    UnpackExternalOperationJournal.operation_type == operation[which]
                )
            )
            assert journal is not None
            assert journal.status == OperationStatus.APPLIED.value
            if tamper.endswith("_reconcile"):
                journal.status = OperationStatus.RECONCILE_REQUIRED.value
                journal.last_error_code = "UNPACK_SEED_RACE_RECONCILE"
            elif tamper == "start_missing":
                session.delete(journal)
            elif tamper == "start_wrong_hash":
                journal.target = {**journal.target, "torrent_hash": "e" * 40}
            elif tamper == "start_bad_after":
                assert journal.after_snapshot is not None
                journal.after_snapshot = {**journal.after_snapshot, "seeding": False}
            elif tamper == "start_bad_before":
                assert journal.before_snapshot is not None
                journal.before_snapshot = {
                    **journal.before_snapshot,
                    "verification_complete": False,
                }
            elif tamper == "verify_wrong_ownership":
                journal.intent = {**journal.intent, "ownership_tag": "unowned"}
            session.commit()
        changed = True
        original_complete(*args, **kwargs)

    monkeypatch.setattr(service, "_mark_completed", completion_after_other_worker_change)
    report = asyncio.run(service.advance_next_batch("execution-1"))
    assert changed
    assert report.completed_count == 0
    assert report.error_count == 1
    assert fake.calls == ["add", "verify", "start"]
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        execution = session.get(UnpackExecution, "execution-1")
        assert item is not None and execution is not None
        assert item.status == UnpackItemStatus.EXECUTION_ERROR.value
        assert item.last_error_code == "UNPACK_SEED_RECONCILE_REQUIRED"
        assert execution.status == UnpackExecutionStatus.COMPLETED_WITH_ERRORS.value


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


@pytest.mark.parametrize("kind", ("qb", "transmission"))
@pytest.mark.parametrize("failure_point", ("site_fetch", "downloader_read"))
def test_stale_worker_error_cannot_fail_newer_active_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    failure_point: str,
) -> None:
    service, factory, fake, _torrent = _fixture(tmp_path, kind=kind)
    site = cast(_SiteProvider, service._site_provider)._site

    def advance_checkpoint() -> None:
        with factory() as session:
            item = session.get(UnpackExecutionItem, "item-1")
            assert item is not None
            item.status = UnpackItemStatus.CLIENT_VERIFYING.value
            item.execution_state = {
                "stage": "CLIENT_VERIFYING",
                "plan_digest": item.execution_plan_digest,
                "torrent_hash": "a" * 40,
            }
            item.version += 1
            session.commit()

    async def failed_site_fetch(_torrent_id: str) -> TorrentPayload:
        advance_checkpoint()
        raise SiteAdapterError("SITE_UNAVAILABLE", "synthetic slow stale worker", retryable=True)

    async def failed_read(_hashes: tuple[str, ...]) -> Any:
        advance_checkpoint()
        raise DownloaderAdapterError("DOWNLOADER_UNAVAILABLE", "synthetic stale read")

    if failure_point == "site_fetch":
        monkeypatch.setattr(site, "fetch_torrent", failed_site_fetch)
    else:
        monkeypatch.setattr(fake, "get_torrents", failed_read)

    report = asyncio.run(service.advance_next_batch("execution-1"))
    assert report.processed_count == 1
    assert report.error_count == 0
    assert report.client_verifying_count == 1
    assert fake.calls == []
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        execution = session.get(UnpackExecution, "execution-1")
        assert item is not None and execution is not None
        assert item.status == UnpackItemStatus.CLIENT_VERIFYING.value
        assert dict(item.execution_state or {}).get("stage") == "CLIENT_VERIFYING"
        assert item.last_error_code is None
        assert execution.status == UnpackExecutionStatus.EXECUTING.value


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


def test_transmission_duplicate_add_response_cannot_claim_existing_torrent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ADD race may return torrent-duplicate even with matching labels."""
    service, factory, fake, torrent = _fixture(tmp_path, kind="transmission")
    transmission = cast(_FakeTransmission, fake)
    infohash = parse_torrent(torrent).v1_info_hash
    assert infohash is not None

    async def duplicate_add(request: TransmissionAddRequest) -> TransmissionAddResult:
        transmission._assert_intent("UNPACK_EXEC_TRANSMISSION_ADD")
        transmission.calls.append("duplicate-add")
        # Synthetic concurrent owner shares the label and path: neither
        # proves that this particular ADD created the existing torrent.
        transmission.state = TransmissionTorrentState(
            torrent_hash=infohash,
            download_dir=request.save_path,
            status=0,
            labels=request.labels,
            percent_done=0.0,
            recheck_progress=0.0,
        )
        return TransmissionAddResult(torrent_hash=infohash, duplicate=True)

    monkeypatch.setattr(transmission, "add_torrent", duplicate_add)
    result = asyncio.run(service.advance_next_batch("execution-1"))
    assert result.error_count == 1
    assert result.client_verifying_count == 0
    assert transmission.calls == ["duplicate-add"]
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == UnpackItemStatus.EXECUTION_ERROR.value
        assert item.last_error_code == "UNPACK_SEED_TORRENT_ALREADY_EXISTS"
        journal = session.scalar(
            select(UnpackExternalOperationJournal).where(
                UnpackExternalOperationJournal.operation_type == "UNPACK_EXEC_TRANSMISSION_ADD"
            )
        )
        assert journal is not None
        assert journal.status == OperationStatus.RECONCILE_REQUIRED.value
        assert journal.last_error_code == "UNPACK_SEED_ADD_DUPLICATE"
        item.status = UnpackItemStatus.EXECUTING.value
        session.commit()

    retry = asyncio.run(service.advance_next_batch("execution-1"))
    assert retry.error_count == 1
    assert transmission.calls == ["duplicate-add"]


@pytest.mark.parametrize(
    ("success", "pending", "failure"),
    (
        (0, 1, 0),
        (0, 0, 1),
        (2, 0, 0),
    ),
)
def test_qb_uncertain_add_counts_require_reconcile_before_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    success: int,
    pending: int,
    failure: int,
) -> None:
    service, factory, fake, _ = _fixture(tmp_path, kind="qb")
    qb = cast(_FakeQb, fake)

    async def ambiguous_add(_request: QbittorrentAddRequest) -> QbittorrentAddResult:
        qb._assert_intent("UNPACK_EXEC_QBITTORRENT_ADD")
        qb.calls.append("ambiguous-add")
        return QbittorrentAddResult(success, pending, failure, (), "2.15.1")

    monkeypatch.setattr(qb, "add_torrent", ambiguous_add)
    report = asyncio.run(service.advance_next_batch("execution-1"))
    assert report.error_count == 1
    assert qb.calls == ["ambiguous-add"]
    with factory() as session:
        row = session.scalar(
            select(UnpackExternalOperationJournal).where(
                UnpackExternalOperationJournal.operation_type == "UNPACK_EXEC_QBITTORRENT_ADD"
            )
        )
        assert row is not None
        assert row.status == OperationStatus.RECONCILE_REQUIRED.value
        assert row.last_error_code == "UNPACK_SEED_ADD_NOT_CONFIRMED"
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        item.status = UnpackItemStatus.EXECUTING.value
        session.commit()

    retried = asyncio.run(service.advance_next_batch("execution-1"))
    assert retried.error_count == 1
    assert qb.calls == ["ambiguous-add"]


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


@pytest.mark.parametrize("kind", ("qb", "transmission"))
@pytest.mark.parametrize(
    "tamper",
    (
        "status_reconcile",
        "target_downloader",
        "target_path",
        "intent_skip_check",
        "intent_payload_digest",
        "before_absence",
        "snapshot_hash",
        "snapshot_path",
        "snapshot_owner",
    ),
)
def test_add_journal_changed_after_verify_cannot_authorize_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, tamper: str
) -> None:
    # The first worker has a valid ADD journal at _prepare(), but a
    # concurrent worker changes that journal after VERIFY, before START.
    # The START preflight must check the current ADD evidence, not trust the
    # earlier snapshot of the journal.
    service, factory, fake, _ = _fixture(tmp_path, kind=kind, force_client_check=True)
    first = asyncio.run(service.advance_next_batch("execution-1"))
    assert first.client_verifying_count == 1
    original_set_stage = service._set_stage
    operation = "UNPACK_EXEC_QBITTORRENT_ADD" if kind == "qb" else "UNPACK_EXEC_TRANSMISSION_ADD"
    changed = False

    def after_verified_then_corrupt(*args: Any, **kwargs: Any) -> None:
        nonlocal changed
        original_set_stage(*args, **kwargs)
        if args[1] != "CLIENT_VERIFIED":
            return
        with factory() as session:
            journal = session.scalar(
                select(UnpackExternalOperationJournal).where(
                    UnpackExternalOperationJournal.operation_type == operation
                )
            )
            assert journal is not None
            assert journal.status == OperationStatus.APPLIED.value
            if tamper == "status_reconcile":
                journal.status = OperationStatus.RECONCILE_REQUIRED.value
                journal.last_error_code = "UNPACK_SEED_CONCURRENT_RECONCILE"
            elif tamper == "target_downloader":
                journal.target = {**journal.target, "downloader_id": "wrong-client"}
            elif tamper == "target_path":
                journal.target = {**journal.target, "remote_save_path": "/other"}
            elif tamper == "intent_skip_check":
                journal.intent = {**journal.intent, "skip_checking": True}
            elif tamper == "intent_payload_digest":
                journal.intent = {**journal.intent, "torrent_payload_digest": "wrong"}
            elif tamper == "before_absence":
                journal.before_snapshot = {
                    **(journal.before_snapshot or {}),
                    "torrent_absent": False,
                }
            else:
                assert isinstance(journal.after_snapshot, dict)
                field = {
                    "snapshot_hash": "torrent_hash",
                    "snapshot_path": "save_path",
                    "snapshot_owner": "ownership_tag",
                }[tamper]
                journal.after_snapshot = {**journal.after_snapshot, field: "wrong"}
            session.commit()
            changed = True

    monkeypatch.setattr(service, "_set_stage", after_verified_then_corrupt)
    report = asyncio.run(service.advance_next_batch("execution-1"))
    assert changed
    assert report.error_count == 1
    assert report.completed_count == 0
    assert fake.calls == ["add", "verify"]
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == UnpackItemStatus.EXECUTION_ERROR.value
        assert item.last_error_code == "UNPACK_SEED_RECONCILE_REQUIRED"


@pytest.mark.parametrize("kind", ("qb", "transmission"))
@pytest.mark.parametrize(
    "tamper",
    ("target_hash", "intent_ownership", "snapshot_hash", "snapshot_path", "snapshot_ownership"),
)
def test_tampered_verify_journal_cannot_authorize_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, tamper: str
) -> None:
    service, factory, fake, _ = _fixture(tmp_path, kind=kind, force_client_check=True)
    first = asyncio.run(service.advance_next_batch("execution-1"))
    assert first.client_verifying_count == 1
    original_set_stage = service._set_stage
    operation = (
        "UNPACK_EXEC_QBITTORRENT_RECHECK" if kind == "qb" else "UNPACK_EXEC_TRANSMISSION_VERIFY"
    )
    tampered = False

    def checkpoint_then_corrupt(*args: Any, **kwargs: Any) -> None:
        nonlocal tampered
        original_set_stage(*args, **kwargs)
        if args[1] != "CLIENT_VERIFIED":
            return
        with factory() as session:
            journal = session.scalar(
                select(UnpackExternalOperationJournal).where(
                    UnpackExternalOperationJournal.operation_type == operation
                )
            )
            assert journal is not None
            assert journal.status == OperationStatus.APPLIED.value
            if tamper == "target_hash":
                journal.target = {**journal.target, "torrent_hash": "f" * 40}
            elif tamper == "intent_ownership":
                journal.intent = {**journal.intent, "ownership_tag": "unowned"}
            else:
                assert isinstance(journal.after_snapshot, dict)
                field = {
                    "snapshot_hash": "torrent_hash",
                    "snapshot_path": "save_path",
                    "snapshot_ownership": "ownership_tag",
                }[tamper]
                journal.after_snapshot = {**journal.after_snapshot, field: "unowned"}
            session.commit()
            tampered = True

    monkeypatch.setattr(service, "_set_stage", checkpoint_then_corrupt)
    report = asyncio.run(service.advance_next_batch("execution-1"))
    assert tampered is True
    assert report.error_count == 1
    assert report.completed_count == 0
    assert fake.calls == ["add", "verify"]
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == UnpackItemStatus.EXECUTION_ERROR.value
        assert item.last_error_code == "UNPACK_SEED_RECONCILE_REQUIRED"


@pytest.mark.parametrize("kind", ("qb", "transmission"))
@pytest.mark.parametrize("phase", ("add", "verify", "start"))
def test_reconcile_journal_is_not_resurrected_by_stale_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, phase: str
) -> None:
    # Another worker has made the external outcome ambiguous while this
    # worker is processing an earlier positive response. Reconciliation
    # must remain sticky regardless of that response.
    service, factory, fake, _ = _fixture(tmp_path, kind=kind, force_client_check=True)
    operations = {
        "qb": {
            "add": "UNPACK_EXEC_QBITTORRENT_ADD",
            "verify": "UNPACK_EXEC_QBITTORRENT_RECHECK",
            "start": "UNPACK_EXEC_QBITTORRENT_START",
        },
        "transmission": {
            "add": "UNPACK_EXEC_TRANSMISSION_ADD",
            "verify": "UNPACK_EXEC_TRANSMISSION_VERIFY",
            "start": "UNPACK_EXEC_TRANSMISSION_START",
        },
    }
    operation = operations[kind][phase]
    if phase != "add":
        first = asyncio.run(service.advance_next_batch("execution-1"))
        assert first.client_verifying_count == 1

    method = {
        "add": "add_torrent",
        "verify": "recheck_torrent" if kind == "qb" else "verify_torrent",
        "start": "start_torrent",
    }[phase]
    original = cast(Any, getattr(fake, method))

    async def reconcile_during_response(arg: Any) -> Any:
        result = await original(arg)
        with factory() as session:
            journal = session.scalar(
                select(UnpackExternalOperationJournal).where(
                    UnpackExternalOperationJournal.operation_type == operation
                )
            )
            assert journal is not None
            assert journal.status == OperationStatus.INTENT_RECORDED.value
            journal_id = journal.id
        service._mark_reconcile(journal_id, "UNPACK_SEED_CONCURRENT_RECONCILE")
        return result

    monkeypatch.setattr(fake, method, reconcile_during_response)
    report = asyncio.run(service.advance_next_batch("execution-1"))
    assert report.error_count == 1
    assert report.completed_count == 0
    expected_calls = {
        "add": ["add"],
        "verify": ["add", "verify"],
        "start": ["add", "verify", "start"],
    }[phase]
    assert fake.calls == expected_calls

    with factory() as session:
        journal = session.scalar(
            select(UnpackExternalOperationJournal).where(
                UnpackExternalOperationJournal.operation_type == operation
            )
        )
        item = session.get(UnpackExecutionItem, "item-1")
        assert journal is not None and item is not None
        assert journal.status == OperationStatus.RECONCILE_REQUIRED.value
        assert journal.last_error_code == "UNPACK_SEED_CONCURRENT_RECONCILE"
        assert item.status == UnpackItemStatus.EXECUTION_ERROR.value
        assert item.last_error_code == "UNPACK_SEED_RECONCILE_REQUIRED"


@pytest.mark.parametrize("kind", ("qb", "transmission"))
@pytest.mark.parametrize("phase", ("add", "verify", "start"))
def test_final_seeding_competing_worker_intent_does_not_dispatch_duplicate_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, phase: str
) -> None:
    # Reproduce an interleaving where both workers loaded journal=None,
    # but the other committed the unique intent first. Only the creator
    # may send the external RPC.
    service, factory, fake, _ = _fixture(tmp_path, kind=kind, force_client_check=True)
    types = {
        "qb": {
            "add": "UNPACK_EXEC_QBITTORRENT_ADD",
            "verify": "UNPACK_EXEC_QBITTORRENT_RECHECK",
            "start": "UNPACK_EXEC_QBITTORRENT_START",
        },
        "transmission": {
            "add": "UNPACK_EXEC_TRANSMISSION_ADD",
            "verify": "UNPACK_EXEC_TRANSMISSION_VERIFY",
            "start": "UNPACK_EXEC_TRANSMISSION_START",
        },
    }
    selected = types[kind][phase]
    if phase != "add":
        initial = asyncio.run(service.advance_next_batch("execution-1"))
        assert initial.client_verifying_count == 1
        assert fake.calls == ["add"]

    real_record = service._record_intent
    competing_intents = 0

    def other_worker_committed_first(*args: Any, **kwargs: Any) -> Any:
        nonlocal competing_intents
        if args[1] != selected:
            return real_record(*args, **kwargs)
        competing_intents += 1
        first, created = real_record(*args, **kwargs)
        assert created is True
        previous, created = real_record(*args, **kwargs)
        assert not created
        assert previous.id == first.id
        return previous, False

    monkeypatch.setattr(service, "_record_intent", other_worker_committed_first)
    report = asyncio.run(service.advance_next_batch("execution-1"))
    # Losing an exclusive intent is a normal concurrent handoff, not a task
    # error. The winning worker must retain the shared item checkpoint.
    assert report.error_count == 0
    assert competing_intents == 1
    # START can be preceded by the legitimate VERIFY in the same worker.
    expected_calls = {
        "add": [],
        "verify": ["add"],
        "start": ["add", "verify"],
    }
    assert fake.calls == expected_calls[phase]
    with factory() as session:
        journal = session.scalar(
            select(UnpackExternalOperationJournal).where(
                UnpackExternalOperationJournal.operation_type == selected
            )
        )
        assert journal is not None
        # The losing worker must leave the winning worker's intent intact,
        # without treating it as its own RPC result.
        assert journal.status == OperationStatus.INTENT_RECORDED.value
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.last_error_code is None
        assert item.status == (
            UnpackItemStatus.CLIENT_VERIFYING.value
            if phase == "verify"
            else UnpackItemStatus.EXECUTING.value
        )
        assert (
            dict(item.execution_state or {}).get("stage")
            == {
                "add": "FILES_MATERIALIZED",
                "verify": "CLIENT_VERIFYING",
                "start": "CLIENT_VERIFIED",
            }[phase]
        )


@pytest.mark.parametrize("kind", ("qb", "transmission"))
def test_final_seeding_native_add_timeout_requires_reconcile_without_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """Unknown native ADD timeout must never be retried, for either client."""
    service, factory, fake, _torrent_bytes = _fixture(tmp_path, kind=kind)
    operation = "UNPACK_EXEC_QBITTORRENT_ADD" if kind == "qb" else "UNPACK_EXEC_TRANSMISSION_ADD"

    async def unknown_add(_request: QbittorrentAddRequest | TransmissionAddRequest) -> None:
        fake._assert_intent(operation)
        fake.calls.append("add-native-unknown")
        raise TimeoutError("synthetic native ADD response lost")

    monkeypatch.setattr(fake, "add_torrent", unknown_add)
    first = asyncio.run(service.advance_next_batch("execution-1"))
    assert first.error_count == 1
    assert fake.calls == ["add-native-unknown"]

    with factory() as session:
        journal = session.scalar(
            select(UnpackExternalOperationJournal).where(
                UnpackExternalOperationJournal.operation_type == operation
            )
        )
        assert journal is not None
        assert journal.status == OperationStatus.RECONCILE_REQUIRED.value
        assert journal.last_error_code == "UNPACK_SEED_ADD_RESULT_UNKNOWN"

        # Simulate an operator retry attempt: an unknown previous external
        # write must never allow a second ADD without reconciliation.
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        item.status = UnpackItemStatus.EXECUTING.value
        session.commit()

    retried = asyncio.run(service.advance_next_batch("execution-1"))
    assert retried.error_count == 1
    assert fake.calls == ["add-native-unknown"]


@pytest.mark.parametrize("kind", ("qb", "transmission"))
def test_final_seeding_crash_after_add_intent_never_blindly_readds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    service, factory, fake, _torrent_bytes = _fixture(tmp_path, kind=kind)
    operation = "UNPACK_EXEC_QBITTORRENT_ADD" if kind == "qb" else "UNPACK_EXEC_TRANSMISSION_ADD"
    real_record = service._record_intent

    def crash_after_intent(*args: Any, **kwargs: Any) -> None:
        real_record(*args, **kwargs)
        raise TimeoutError("synthetic process lost after durable ADD intent")

    monkeypatch.setattr(service, "_record_intent", crash_after_intent)
    first = asyncio.run(service.advance_next_batch("execution-1"))
    assert first.error_count == 1
    assert fake.calls == []
    with factory() as session:
        intent = session.scalar(
            select(UnpackExternalOperationJournal).where(
                UnpackExternalOperationJournal.operation_type == operation
            )
        )
        assert intent is not None
        assert intent.status == OperationStatus.INTENT_RECORDED.value
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        item.status = UnpackItemStatus.EXECUTING.value
        session.commit()

    monkeypatch.setattr(service, "_record_intent", real_record)
    retried = asyncio.run(service.advance_next_batch("execution-1"))
    assert retried.error_count == 1
    assert fake.calls == []
    with factory() as session:
        intent = session.scalar(
            select(UnpackExternalOperationJournal).where(
                UnpackExternalOperationJournal.operation_type == operation
            )
        )
        assert intent is not None
        assert intent.status == OperationStatus.RECONCILE_REQUIRED.value


@pytest.mark.parametrize("kind", ("qb", "transmission"))
def test_final_seeding_successful_add_followed_by_read_timeout_is_not_replayed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    service, factory, fake, _torrent_bytes = _fixture(tmp_path, kind=kind)
    operation = "UNPACK_EXEC_QBITTORRENT_ADD" if kind == "qb" else "UNPACK_EXEC_TRANSMISSION_ADD"
    real_get = fake.get_torrents
    attempts = 0

    async def disconnected_read(hashes: tuple[str, ...]) -> tuple[object, ...]:
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            raise TimeoutError("synthetic lost post-ADD state probe")
        return await real_get(hashes)

    monkeypatch.setattr(fake, "get_torrents", disconnected_read)
    first = asyncio.run(service.advance_next_batch("execution-1"))
    assert first.error_count == 1
    assert fake.calls == ["add"]
    with factory() as session:
        journal = session.scalar(
            select(UnpackExternalOperationJournal).where(
                UnpackExternalOperationJournal.operation_type == operation
            )
        )
        assert journal is not None
        assert journal.status == OperationStatus.RECONCILE_REQUIRED.value
    assert fake.state is not None


@pytest.mark.parametrize("kind", ("qb", "transmission"))
def test_final_seeding_native_start_timeout_requires_reconcile_without_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    service, factory, fake, _torrent_bytes = _fixture(tmp_path, kind=kind)
    operation = (
        "UNPACK_EXEC_QBITTORRENT_START" if kind == "qb" else "UNPACK_EXEC_TRANSMISSION_START"
    )

    async def unknown_start(_hash: str) -> None:
        fake._assert_intent(operation)
        fake.calls.append("start-native-unknown")
        raise TimeoutError("synthetic native START response lost")

    monkeypatch.setattr(fake, "start_torrent", unknown_start)
    if kind == "transmission":
        first = asyncio.run(service.advance_next_batch("execution-1"))
        assert first.client_verifying_count == 1

    pending = asyncio.run(service.advance_next_batch("execution-1"))
    assert pending.error_count == 1
    assert fake.calls.count("start-native-unknown") == 1
    with factory() as session:
        journal = session.scalar(
            select(UnpackExternalOperationJournal).where(
                UnpackExternalOperationJournal.operation_type == operation
            )
        )
        assert journal is not None
        assert journal.status == OperationStatus.RECONCILE_REQUIRED.value
        assert journal.last_error_code == "UNPACK_SEED_START_RESULT_UNKNOWN"
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        item.status = UnpackItemStatus.EXECUTING.value
        session.commit()

    retried = asyncio.run(service.advance_next_batch("execution-1"))
    assert retried.error_count == 1
    assert fake.calls.count("start-native-unknown") == 1


@pytest.mark.parametrize("kind", ("qb", "transmission"))
def test_final_seeding_native_verify_timeout_requires_reconcile_without_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    service, factory, fake, _torrent_bytes = _fixture(tmp_path, kind=kind, force_client_check=True)
    operation = (
        "UNPACK_EXEC_QBITTORRENT_RECHECK" if kind == "qb" else "UNPACK_EXEC_TRANSMISSION_VERIFY"
    )

    async def unknown_verify(_hash: str) -> None:
        fake._assert_intent(operation)
        fake.calls.append("verify-native-unknown")
        raise TimeoutError("synthetic native VERIFY response lost")

    monkeypatch.setattr(
        fake, "recheck_torrent" if kind == "qb" else "verify_torrent", unknown_verify
    )
    first = asyncio.run(service.advance_next_batch("execution-1"))
    assert first.client_verifying_count == 1
    pending = asyncio.run(service.advance_next_batch("execution-1"))
    assert pending.error_count == 1
    assert fake.calls.count("verify-native-unknown") == 1
    with factory() as session:
        journal = session.scalar(
            select(UnpackExternalOperationJournal).where(
                UnpackExternalOperationJournal.operation_type == operation
            )
        )
        assert journal is not None
        assert journal.status == OperationStatus.RECONCILE_REQUIRED.value
        assert journal.last_error_code == "UNPACK_SEED_VERIFY_RESULT_UNKNOWN"
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        item.status = UnpackItemStatus.CLIENT_VERIFYING.value
        session.commit()

    retried = asyncio.run(service.advance_next_batch("execution-1"))
    assert retried.error_count == 1
    assert fake.calls.count("verify-native-unknown") == 1


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
