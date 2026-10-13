import asyncio
import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.application import unpack_auxiliary as auxiliary_module
from backend.app.application.downloaders import QbittorrentWriteBinding
from backend.app.application.errors import ApplicationError
from backend.app.application.sites import EnabledSiteAdapter
from backend.app.application.unpack_auxiliary import UnpackAuxiliaryStagingService
from backend.app.domain.downloader import PathMappingRule
from backend.app.domain.operation import OperationStatus
from backend.app.domain.site_adapter import SiteAdapter, TorrentPayload
from backend.app.domain.unpack import (
    UnpackCandidateVerificationStatus,
    UnpackExecutionStatus,
    UnpackItemStatus,
)
from backend.app.domain.verification import VerificationLevel
from backend.app.infrastructure.adapters.downloaders import (
    DownloaderAdapterError,
    QbittorrentAddRequest,
    QbittorrentAddResult,
    QbittorrentTorrentState,
    QbittorrentWriteAdapter,
)
from backend.app.infrastructure.adapters.site_errors import SiteAdapterError
from backend.app.infrastructure.authorized_paths import AuthorizedPathScope
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


def _torrent(movie: bytes, nfo: bytes) -> bytes:
    stream = movie + nfo
    piece_length = 6
    pieces = b"".join(
        hashlib.sha1(stream[offset : offset + piece_length]).digest()
        for offset in range(0, len(stream), piece_length)
    )
    return _bencode(
        {
            b"info": {
                b"files": [
                    {b"length": len(movie), b"path": [b"Movie.mkv"]},
                    {b"length": len(nfo), b"path": [b"Movie.nfo"]},
                ],
                b"name": b"Release",
                b"piece length": piece_length,
                b"pieces": pieces,
            }
        }
    )


class _FakeSite:
    def __init__(self, torrent: bytes) -> None:
        self._torrent = torrent
        self.transient_error = False
        self.delay_seconds = 0.0

    async def fetch_torrent(self, torrent_id: str) -> TorrentPayload:
        assert torrent_id == "torrent-1"
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        if self.transient_error:
            raise SiteAdapterError("SITE_UNAVAILABLE", "temporary server error", retryable=True)
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
        data_root: Path,
        torrent: bytes,
        file_bytes: dict[str, bytes],
    ) -> None:
        self._factory = factory
        self._data_root = data_root
        self._meta = parse_torrent(torrent)
        self._file_bytes = file_bytes
        self.state: QbittorrentTorrentState | None = None
        self.wanted: tuple[int, ...] = ()
        self.unwanted: tuple[int, ...] = ()
        self.calls: list[str] = []
        self.connection_unavailable = False
        self.remove_connection_unknown_once = False
        self.stop_connection_unknown_once = False
        self.stop_disconnect_before_status_once = False
        self.remove_disconnect_before_status_once = False
        self.stop_visible_after_queries = 0
        self._pending_stop_queries = 0
        self.remove_visible_after_queries = 0
        self._pending_remove_queries = 0

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
        self._assert_intent("UNPACK_AUX_TORRENT_ADD")
        self.calls.append("add")
        torrent_hash = self._meta.v1_info_hash
        assert torrent_hash is not None
        self.state = QbittorrentTorrentState(
            torrent_hash=torrent_hash,
            save_path=request.save_path,
            content_path=None,
            state="stoppedDL",
            tags=request.tags,
            progress=0.0,
        )
        return QbittorrentAddResult(1, 0, 0, (torrent_hash,), "2.11.0")

    async def get_torrents(
        self,
        torrent_hashes: tuple[str, ...],
    ) -> tuple[QbittorrentTorrentState, ...]:
        if self.connection_unavailable:
            raise DownloaderAdapterError("DOWNLOADER_UNAVAILABLE", "temporary RPC outage")
        if self._pending_stop_queries:
            self._pending_stop_queries -= 1
            if self._pending_stop_queries == 0 and self.state is not None:
                self.state = replace(self.state, state="stoppedDL")
        if self._pending_remove_queries:
            self._pending_remove_queries -= 1
            if self._pending_remove_queries == 0:
                self.state = None
        if self.state is None or self.state.torrent_hash not in torrent_hashes:
            return ()
        return (self.state,)

    async def set_file_selection(
        self,
        torrent_hash: str,
        *,
        wanted: tuple[int, ...],
        unwanted: tuple[int, ...],
    ) -> None:
        self._assert_intent("UNPACK_AUX_FILE_SELECTION")
        assert self.state is not None and torrent_hash == self.state.torrent_hash
        self.calls.append("select")
        self.wanted = wanted
        self.unwanted = unwanted

    async def start_torrent(self, torrent_hash: str) -> None:
        self._assert_intent("UNPACK_AUX_TORRENT_START")
        assert self.state is not None and torrent_hash == self.state.torrent_hash
        self.calls.append("start")
        local_save = self._container_path(self.state.save_path)
        for index in self.wanted:
            torrent_file = self._meta.files[index]
            target = local_save.joinpath(*torrent_file.path.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(self._file_bytes[torrent_file.path])
        self.state = replace(self.state, state="downloading", progress=0.5)

    async def stop_torrent(self, torrent_hash: str) -> None:
        self._assert_intent("UNPACK_AUX_TORRENT_STOP")
        assert self.state is not None and torrent_hash == self.state.torrent_hash
        self.calls.append("stop")
        if self.stop_disconnect_before_status_once:
            self.stop_disconnect_before_status_once = False
            self.connection_unavailable = True
            raise DownloaderAdapterError(
                "DOWNLOADER_UNAVAILABLE", "stop and status probe both lost"
            )
        if self.stop_connection_unknown_once:
            self.stop_connection_unknown_once = False
            raise DownloaderAdapterError("DOWNLOADER_UNAVAILABLE", "uncertain stop outcome")
        if self.stop_visible_after_queries:
            self._pending_stop_queries = self.stop_visible_after_queries
            return
        self.state = replace(self.state, state="stoppedDL")

    async def remove_torrent_keep_files(self, torrent_hash: str) -> None:
        self._assert_intent("UNPACK_AUX_TORRENT_REMOVE")
        assert self.state is not None and torrent_hash == self.state.torrent_hash
        self.calls.append("remove")
        if self.remove_disconnect_before_status_once:
            self.remove_disconnect_before_status_once = False
            self.connection_unavailable = True
            raise DownloaderAdapterError(
                "DOWNLOADER_UNAVAILABLE", "remove and status probe both lost"
            )
        if self.remove_connection_unknown_once:
            self.remove_connection_unknown_once = False
            raise DownloaderAdapterError("DOWNLOADER_UNAVAILABLE", "uncertain remove outcome")
        if self.remove_visible_after_queries:
            self._pending_remove_queries = self.remove_visible_after_queries
            return
        self.state = None

    def _container_path(self, remote: str) -> Path:
        prefix = "/downloads"
        assert remote == prefix or remote.startswith(prefix + "/")
        suffix = remote[len(prefix) :].lstrip("/")
        return self._data_root.joinpath(*suffix.split("/")) if suffix else self._data_root


class _DownloaderProvider:
    def __init__(self, binding: QbittorrentWriteBinding) -> None:
        self._binding = binding

    def write_binding(self, downloader_id: str) -> QbittorrentWriteBinding:
        assert downloader_id == "downloader-target"
        return self._binding


def _fixture(
    tmp_path: Path,
    *,
    mapped_root: Path | None = None,
) -> tuple[
    UnpackAuxiliaryStagingService,
    sessionmaker[Session],
    _FakeQb,
    Path,
]:
    data_root = tmp_path / "data"
    movies = data_root / "movies"
    output = data_root / "seeding"
    movies.mkdir(parents=True)
    output.mkdir()
    movie = b"abcdefgh"
    nfo = b"info"
    source = movies / "Movie.mkv"
    source.write_bytes(movie)
    snapshot = current_file_snapshot(source)
    torrent = _torrent(movie, nfo)
    meta = parse_torrent(torrent)

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory: sessionmaker[Session] = sessionmaker(
        bind=engine,
        expire_on_commit=False,
        autoflush=False,
    )
    now = utc_now()
    with factory() as session:
        session.add(
            UnpackDefinition(
                id="definition-1",
                name="辅助文件测试",
                trigger_kind="MANUAL",
                status="PENDING_EXECUTION",
                source_kind="DIRECTORY",
                execution_scope_kind="ALL_MATCHING_MEDIA",
                source_config={"directory_path": movies.as_posix()},
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
                status=UnpackExecutionStatus.CONTENT_VERIFYING.value,
                config_snapshot={
                    "site_ids": ["site-config-1"],
                    "output_config": {
                        "output_directory": output.as_posix(),
                        "storage_mode": "HARDLINK",
                        "conflict_policy": "VERIFY_REUSE_OR_STOP",
                        "target_downloader_id": "downloader-target",
                    },
                },
                discovery_complete=True,
                total_count=1,
                matched_auto_count=1,
                review_count=0,
                content_verified_count=0,
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
                source_snapshot={
                    "path": source.as_posix(),
                    "relative_path": "Movie.mkv",
                    "device": snapshot.device,
                    "inode": snapshot.inode,
                    "size": snapshot.size,
                    "mtime_ns": str(snapshot.mtime_ns),
                    "file_type": snapshot.file_type,
                },
                media_identity={},
                status=UnpackItemStatus.AUXILIARY_FETCHING.value,
                selected_candidate_id="candidate-1",
                candidate_generation=1,
                retry_count=0,
                content_verification_level=VerificationLevel.CLIENT_CHECK_REQUIRED.value,
                torrent_metainfo_digest=meta.metainfo_digest,
                auxiliary_state={
                    "state": "PENDING_FETCH",
                    "missing_paths": ["Release/Movie.nfo"],
                },
                last_error_code="UNPACK_AUXILIARY_FILES_REQUIRED",
                last_error_message="主影片未发现内容冲突，但候选 torrent 还缺少辅助文件",
                version=1,
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
                size_bytes=len(movie) + len(nfo),
                score_bps=10000,
                is_exact_match=True,
                evidence={"hard_conflicts": []},
                verification_status=UnpackCandidateVerificationStatus.UNAVAILABLE.value,
                verification_level=VerificationLevel.CLIENT_CHECK_REQUIRED.value,
                verification_error_code="UNPACK_AUXILIARY_FILES_REQUIRED",
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

    fake_qb = _FakeQb(
        factory,
        data_root,
        torrent,
        {"Release/Movie.nfo": nfo},
    )
    binding = QbittorrentWriteBinding(
        downloader_id="downloader-target",
        downloader_version=3,
        binding_digest="b" * 64,
        path_mappings=(
            PathMappingRule(
                "/downloads",
                (mapped_root if mapped_root is not None else data_root).as_posix(),
            ),
        ),
        capabilities={"supports_selective_files": True, "api_version": "2.11.0"},
        adapter=cast(QbittorrentWriteAdapter, fake_qb),
        data_root=data_root,
    )
    service = UnpackAuxiliaryStagingService(
        factory,
        _SiteProvider(_FakeSite(torrent)),
        _DownloaderProvider(binding),
        path_scope=AuthorizedPathScope.legacy_only(legacy_data_root=data_root),
    )
    return service, factory, fake_qb, data_root


@pytest.mark.parametrize("tamper", ("unselected_site", "reference_site", "reference_torrent"))
def test_auxiliary_candidate_identity_must_match_frozen_execution_before_staging(
    tmp_path: Path, tamper: str
) -> None:
    """A modified candidate must never fetch or write into auxiliary staging."""
    service, factory, fake_qb, data_root = _fixture(tmp_path)
    with factory() as session:
        candidate = session.get(UnpackMatchCandidate, "candidate-1")
        assert candidate is not None
        if tamper == "unselected_site":
            candidate.site_id = "site-not-scanned"
        else:
            raw_ref = dict(candidate.raw_ref)
            if tamper == "reference_site":
                raw_ref["site_config_id"] = "site-not-scanned"
            else:
                raw_ref["torrent_id"] = "torrent-not-selected"
            candidate.raw_ref = raw_ref
        session.commit()

    report = asyncio.run(service.advance_next_batch("execution-1"))

    assert report.error_count == 1
    assert fake_qb.calls == []
    assert not (data_root / ".packbreaker-staging").exists()
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == UnpackItemStatus.MATCH_ERROR.value
        assert session.scalar(select(UnpackExternalOperationJournal.id)) is None


@pytest.mark.parametrize(
    ("phase", "invalidation"),
    (
        ("PENDING_FETCH", "status"),
        ("DOWNLOADING", "status"),
        ("DOWNLOADING", "version"),
        ("DOWNLOADING", "candidate"),
    ),
)
def test_auxiliary_paused_site_fetch_cannot_write_after_item_is_invalidated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    invalidation: str,
) -> None:
    """A worker paused on a tracker read must not write for a changed item."""
    service, factory, fake_qb, data_root = _fixture(tmp_path)
    if phase == "DOWNLOADING":
        asyncio.run(service.advance_next_batch("execution-1"))
        assert fake_qb.calls == ["add", "select", "start"]
    old_calls = list(fake_qb.calls)
    with factory() as session:
        old_journal_count = len(session.scalars(select(UnpackExternalOperationJournal.id)).all())

    site = cast(_SiteProvider, service._site_provider)._site
    original_fetch = site.fetch_torrent
    entered = asyncio.Event()
    release = asyncio.Event()

    async def paused_fetch(torrent_id: str) -> TorrentPayload:
        entered.set()
        await release.wait()
        return await original_fetch(torrent_id)

    monkeypatch.setattr(site, "fetch_torrent", paused_fetch)

    async def interleave() -> None:
        worker = asyncio.create_task(service.advance_next_batch("execution-1"))
        await asyncio.wait_for(entered.wait(), timeout=3)
        with factory() as session:
            item = session.get(UnpackExecutionItem, "item-1")
            assert item is not None
            if invalidation == "status":
                item.status = UnpackItemStatus.REVIEW_REQUIRED.value
            elif invalidation == "version":
                item.version += 1
            else:
                item.selected_candidate_id = "candidate-changed"
            session.commit()
        release.set()
        await worker

    asyncio.run(interleave())
    assert fake_qb.calls == old_calls
    if phase == "PENDING_FETCH":
        assert not (data_root / ".packbreaker-staging").exists()
    with factory() as session:
        assert (
            len(session.scalars(select(UnpackExternalOperationJournal.id)).all())
            == old_journal_count
        )
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        if invalidation == "status":
            assert item.status == UnpackItemStatus.REVIEW_REQUIRED.value
        elif invalidation == "candidate":
            assert item.selected_candidate_id == "candidate-changed"


@pytest.mark.parametrize("invalidation", ("status", "candidate"))
def test_auxiliary_cancel_during_prewrite_downloader_probe_never_stops_old_torrent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalidation: str,
) -> None:
    """A status read before STOP must not authorize a now-invalidated item."""
    service, factory, fake_qb, _data_root = _fixture(tmp_path)
    asyncio.run(service.advance_next_batch("execution-1"))
    assert fake_qb.calls == ["add", "select", "start"]
    with factory() as session:
        original_journal_count = len(
            session.scalars(select(UnpackExternalOperationJournal.id)).all()
        )

    original_get = fake_qb.get_torrents
    count = 0
    entered = asyncio.Event()
    release = asyncio.Event()

    async def paused_get(hashes: tuple[str, ...]) -> tuple[QbittorrentTorrentState, ...]:
        nonlocal count
        count += 1
        if count == 2:  # STOP's safe read, after the initial poll read.
            entered.set()
            await release.wait()
        return await original_get(hashes)

    monkeypatch.setattr(fake_qb, "get_torrents", paused_get)

    async def interleave() -> None:
        worker = asyncio.create_task(service.advance_next_batch("execution-1"))
        await asyncio.wait_for(entered.wait(), timeout=3)
        with factory() as session:
            item = session.get(UnpackExecutionItem, "item-1")
            assert item is not None
            if invalidation == "status":
                item.status = UnpackItemStatus.REVIEW_REQUIRED.value
            else:
                item.selected_candidate_id = "candidate-changed"
            session.commit()
        release.set()
        await worker

    asyncio.run(interleave())
    assert fake_qb.calls == ["add", "select", "start"]
    with factory() as session:
        journals = session.scalars(select(UnpackExternalOperationJournal)).all()
        assert len(journals) == original_journal_count
        assert all(row.operation_type != "UNPACK_AUX_TORRENT_STOP" for row in journals)
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        if invalidation == "status":
            assert item.status == UnpackItemStatus.REVIEW_REQUIRED.value
        else:
            assert item.selected_candidate_id == "candidate-changed"


def test_auxiliary_staging_unmapped_path_fails_before_any_external_write(
    tmp_path: Path,
) -> None:
    """A stale path mapping must not create staging or dispatch a torrent."""
    service, factory, fake_qb, data_root = _fixture(
        tmp_path, mapped_root=tmp_path / "stale-container-path"
    )
    original = (data_root / "movies" / "Movie.mkv").read_bytes()

    report = asyncio.run(service.advance_next_batch("execution-1"))

    assert report.error_count == 1
    assert fake_qb.calls == []
    assert not (data_root / ".packbreaker-staging").exists()
    assert (data_root / "movies" / "Movie.mkv").read_bytes() == original
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == UnpackItemStatus.MATCH_ERROR.value
        assert item.last_error_code == "UNPACK_AUX_STAGING_PATH_UNMAPPED"
        assert session.scalar(select(UnpackExternalOperationJournal.id)) is None


def test_auxiliary_staging_journals_writes_and_never_places_main_movie_in_staging(
    tmp_path: Path,
) -> None:
    service, factory, fake_qb, data_root = _fixture(tmp_path)

    first = asyncio.run(service.advance_next_batch("execution-1"))

    assert first.downloading_count == 1
    assert fake_qb.calls == ["add", "select", "start"]
    assert fake_qb.wanted == (1,)
    assert fake_qb.unwanted == (0,)
    staging = data_root / ".packbreaker-staging" / "unpack" / "execution-1" / "item-1"
    assert (staging / "Release" / "Movie.nfo").read_bytes() == b"info"
    assert not (staging / "Release" / "Movie.mkv").exists()
    with factory() as session:
        journals = session.scalars(
            select(UnpackExternalOperationJournal).order_by(
                UnpackExternalOperationJournal.created_at,
                UnpackExternalOperationJournal.operation_type,
            )
        ).all()
        assert {item.operation_type for item in journals} == {
            "UNPACK_AUX_STAGING_DIR",
            "UNPACK_AUX_TORRENT_ADD",
            "UNPACK_AUX_FILE_SELECTION",
            "UNPACK_AUX_TORRENT_START",
        }
        assert all(item.status == OperationStatus.APPLIED.value for item in journals)


def test_auxiliary_staging_reverifies_cross_file_piece_then_removes_torrent_keep_files(
    tmp_path: Path,
) -> None:
    service, factory, fake_qb, data_root = _fixture(tmp_path)

    asyncio.run(service.advance_next_batch("execution-1"))
    second = asyncio.run(service.advance_next_batch("execution-1"))

    assert second.verified_count == 1
    assert second.mismatch_count == 0
    assert fake_qb.calls == ["add", "select", "start", "stop", "remove"]
    staging_nfo = (
        data_root
        / ".packbreaker-staging"
        / "unpack"
        / "execution-1"
        / "item-1"
        / "Release"
        / "Movie.nfo"
    )
    assert staging_nfo.read_bytes() == b"info"
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        candidate = session.get(UnpackMatchCandidate, "candidate-1")
        assert item is not None
        assert candidate is not None
        assert item.status == UnpackItemStatus.CONTENT_VERIFIED.value
        assert item.content_verification_level == VerificationLevel.FULL_VERIFIED.value
        assert item.auxiliary_state is not None
        assert item.auxiliary_state["state"] == "READY_FOR_FINALIZATION"
        assert item.auxiliary_state["staging_torrent_removed"] is True
        assert candidate.verification_status == UnpackCandidateVerificationStatus.VERIFIED.value
        assert candidate.verification_level == VerificationLevel.FULL_VERIFIED.value
        assert candidate.evidence["auxiliary_verification"]["piece_mismatch"] is False
        operations = {
            row.operation_type: row.status
            for row in session.scalars(select(UnpackExternalOperationJournal)).all()
        }
        assert operations["UNPACK_AUX_TORRENT_STOP"] == OperationStatus.APPLIED.value
        assert operations["UNPACK_AUX_TORRENT_REMOVE"] == OperationStatus.APPLIED.value


def test_auxiliary_stop_response_precedes_status_visibility_without_duplicate_stop(
    tmp_path: Path,
) -> None:
    service, factory, fake_qb, _data_root = _fixture(tmp_path)
    asyncio.run(service.advance_next_batch("execution-1"))
    fake_qb.stop_visible_after_queries = 4
    report = asyncio.run(service.advance_next_batch("execution-1"))
    assert report.verified_count == 1
    assert fake_qb.calls == ["add", "select", "start", "stop", "remove"]
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == UnpackItemStatus.CONTENT_VERIFIED.value
        stop = session.scalar(
            select(UnpackExternalOperationJournal).where(
                UnpackExternalOperationJournal.operation_type == "UNPACK_AUX_TORRENT_STOP"
            )
        )
        assert stop is not None
        assert stop.status == OperationStatus.APPLIED.value


def test_auxiliary_remove_response_precedes_status_visibility_without_duplicate_remove(
    tmp_path: Path,
) -> None:
    service, factory, fake_qb, _data_root = _fixture(tmp_path)
    asyncio.run(service.advance_next_batch("execution-1"))
    fake_qb.remove_visible_after_queries = 3
    report = asyncio.run(service.advance_next_batch("execution-1"))
    assert report.verified_count == 1
    assert fake_qb.calls == ["add", "select", "start", "stop", "remove"]
    with factory() as session:
        removal = session.scalar(
            select(UnpackExternalOperationJournal).where(
                UnpackExternalOperationJournal.operation_type == "UNPACK_AUX_TORRENT_REMOVE"
            )
        )
        assert removal is not None
        assert removal.status == OperationStatus.APPLIED.value


def test_auxiliary_remove_unknown_keeps_journal_and_prevents_duplicate_remove(
    tmp_path: Path,
) -> None:
    service, factory, fake_qb, _data_root = _fixture(tmp_path)
    asyncio.run(service.advance_next_batch("execution-1"))
    fake_qb.remove_connection_unknown_once = True

    first = asyncio.run(service.advance_next_batch("execution-1"))
    assert first.downloading_count == 1
    assert fake_qb.calls == ["add", "select", "start", "stop", "remove"]
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == UnpackItemStatus.AUXILIARY_FETCHING.value
        journal = session.scalar(
            select(UnpackExternalOperationJournal).where(
                UnpackExternalOperationJournal.operation_type == "UNPACK_AUX_TORRENT_REMOVE"
            )
        )
        assert journal is not None
        assert journal.status == OperationStatus.RECONCILE_REQUIRED.value

    second = asyncio.run(service.advance_next_batch("execution-1"))
    assert second.error_count == 1
    assert fake_qb.calls.count("remove") == 1
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.last_error_code == "UNPACK_AUX_REMOVE_RECONCILE_REQUIRED"


def test_auxiliary_stop_unknown_prevents_duplicate_stop(
    tmp_path: Path,
) -> None:
    service, factory, fake_qb, _data_root = _fixture(tmp_path)
    asyncio.run(service.advance_next_batch("execution-1"))
    fake_qb.stop_connection_unknown_once = True
    pending = asyncio.run(service.advance_next_batch("execution-1"))
    assert pending.downloading_count == 1
    assert fake_qb.calls.count("stop") == 1

    second = asyncio.run(service.advance_next_batch("execution-1"))
    assert second.error_count == 1
    assert fake_qb.calls.count("stop") == 1
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.last_error_code == "UNPACK_AUX_STOP_RECONCILE_REQUIRED"


def test_auxiliary_stop_and_state_probe_both_disconnect_marks_reconcile(
    tmp_path: Path,
) -> None:
    service, factory, fake_qb, _data_root = _fixture(tmp_path)
    asyncio.run(service.advance_next_batch("execution-1"))
    fake_qb.stop_disconnect_before_status_once = True
    first = asyncio.run(service.advance_next_batch("execution-1"))
    assert first.downloading_count == 1
    with factory() as session:
        journal = session.scalar(
            select(UnpackExternalOperationJournal).where(
                UnpackExternalOperationJournal.operation_type == "UNPACK_AUX_TORRENT_STOP"
            )
        )
        assert journal is not None
        assert journal.status == OperationStatus.RECONCILE_REQUIRED.value
    fake_qb.connection_unavailable = False
    second = asyncio.run(service.advance_next_batch("execution-1"))
    assert second.error_count == 1
    assert fake_qb.calls.count("stop") == 1


def test_auxiliary_remove_and_state_probe_both_disconnect_marks_reconcile(
    tmp_path: Path,
) -> None:
    service, factory, fake_qb, _data_root = _fixture(tmp_path)
    asyncio.run(service.advance_next_batch("execution-1"))
    fake_qb.remove_disconnect_before_status_once = True
    first = asyncio.run(service.advance_next_batch("execution-1"))
    assert first.downloading_count == 1
    with factory() as session:
        journal = session.scalar(
            select(UnpackExternalOperationJournal).where(
                UnpackExternalOperationJournal.operation_type == "UNPACK_AUX_TORRENT_REMOVE"
            )
        )
        assert journal is not None
        assert journal.status == OperationStatus.RECONCILE_REQUIRED.value
    fake_qb.connection_unavailable = False
    second = asyncio.run(service.advance_next_batch("execution-1"))
    assert second.error_count == 1
    assert fake_qb.calls.count("remove") == 1


def test_auxiliary_confirmed_stop_drifting_to_running_does_not_send_second_stop(
    tmp_path: Path,
) -> None:
    service, factory, fake_qb, _data_root = _fixture(tmp_path)
    asyncio.run(service.advance_next_batch("execution-1"))
    fake_qb.remove_connection_unknown_once = True
    asyncio.run(service.advance_next_batch("execution-1"))
    assert fake_qb.state is not None
    fake_qb.state = replace(fake_qb.state, state="downloading")
    report = asyncio.run(service.advance_next_batch("execution-1"))
    assert report.error_count == 1
    assert fake_qb.calls.count("stop") == 1
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.last_error_code == "UNPACK_AUX_STOP_STATE_DRIFTED"


def test_auxiliary_recovers_when_remote_remove_applied_but_verification_not_committed(
    tmp_path: Path,
) -> None:
    service, factory, fake_qb, _data_root = _fixture(tmp_path)
    asyncio.run(service.advance_next_batch("execution-1"))
    asyncio.run(service.advance_next_batch("execution-1"))
    assert fake_qb.state is None
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        item.status = UnpackItemStatus.AUXILIARY_FETCHING.value
        item.content_verification_level = None
        item.auxiliary_state = {
            **dict(item.auxiliary_state or {}),
            "state": "DOWNLOADING",
        }
        session.commit()

    resumed = asyncio.run(service.advance_next_batch("execution-1"))
    assert resumed.verified_count == 1
    assert fake_qb.calls == ["add", "select", "start", "stop", "remove"]


def test_auxiliary_poll_recovers_after_transient_downloader_unavailable(
    tmp_path: Path,
) -> None:
    service, factory, fake_qb, data_root = _fixture(tmp_path)
    asyncio.run(service.advance_next_batch("execution-1"))
    fake_qb.connection_unavailable = True

    pending = asyncio.run(service.advance_next_batch("execution-1"))
    assert pending.downloading_count == 1
    assert pending.error_count == 0
    assert fake_qb.calls == ["add", "select", "start"]
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == UnpackItemStatus.AUXILIARY_FETCHING.value
        assert item.auxiliary_state is not None
        assert item.auxiliary_state["state"] == "DOWNLOADING"
        assert item.last_error_code == "UNPACK_AUXILIARY_FILES_REQUIRED"

    fake_qb.connection_unavailable = False
    recovered = asyncio.run(service.advance_next_batch("execution-1"))
    assert recovered.verified_count == 1
    assert recovered.error_count == 0
    assert fake_qb.calls == ["add", "select", "start", "stop", "remove"]
    assert (data_root / "movies" / "Movie.mkv").read_bytes() == b"abcdefgh"


def test_auxiliary_poll_native_timeout_preserves_state_and_recovers_without_readd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A raw read-only RPC timeout must not crash the auxiliary worker."""
    service, factory, fake_qb, data_root = _fixture(tmp_path)
    asyncio.run(service.advance_next_batch("execution-1"))
    assert fake_qb.calls == ["add", "select", "start"]

    original_get = fake_qb.get_torrents
    read_calls = 0

    async def one_timeout(
        hashes: tuple[str, ...],
    ) -> tuple[QbittorrentTorrentState, ...]:
        nonlocal read_calls
        read_calls += 1
        if read_calls == 1:
            raise TimeoutError("synthetic read-only RPC timeout")
        return await original_get(hashes)

    monkeypatch.setattr(fake_qb, "get_torrents", one_timeout)
    pending = asyncio.run(service.advance_next_batch("execution-1"))
    assert pending.downloading_count == 1
    assert pending.error_count == 0
    assert fake_qb.calls == ["add", "select", "start"]
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == UnpackItemStatus.AUXILIARY_FETCHING.value
        assert item.auxiliary_state is not None
        assert item.auxiliary_state["state"] == "DOWNLOADING"

    recovered = asyncio.run(service.advance_next_batch("execution-1"))
    assert recovered.verified_count == 1
    assert recovered.error_count == 0
    assert fake_qb.calls == ["add", "select", "start", "stop", "remove"]
    assert (data_root / "movies" / "Movie.mkv").read_bytes() == b"abcdefgh"


def test_auxiliary_preadd_native_readonly_timeout_never_sends_add_until_recovered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, factory, fake_qb, data_root = _fixture(tmp_path)
    original_get = fake_qb.get_torrents
    probes = 0

    async def one_timeout(
        hashes: tuple[str, ...],
    ) -> tuple[QbittorrentTorrentState, ...]:
        nonlocal probes
        probes += 1
        if probes == 1:
            raise TimeoutError("synthetic pre-add read-only RPC timeout")
        return await original_get(hashes)

    monkeypatch.setattr(fake_qb, "get_torrents", one_timeout)
    pending = asyncio.run(service.advance_next_batch("execution-1"))
    assert pending.downloading_count == 1
    assert pending.error_count == 0
    assert fake_qb.calls == []
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        journal = session.scalar(
            select(UnpackExternalOperationJournal).where(
                UnpackExternalOperationJournal.operation_type == "UNPACK_AUX_TORRENT_ADD"
            )
        )
        assert item is not None
        assert item.status == UnpackItemStatus.AUXILIARY_FETCHING.value
        assert item.auxiliary_state is not None
        assert item.auxiliary_state["state"] == "PREPARING"
        # A read-only preflight timeout must not reserve an ADD intent;
        # no write was dispatched, so the next pass may safely start.
        assert journal is None

    resumed = asyncio.run(service.advance_next_batch("execution-1"))
    assert resumed.downloading_count == 1
    assert resumed.error_count == 0
    assert fake_qb.calls == ["add", "select", "start"]
    verified = asyncio.run(service.advance_next_batch("execution-1"))
    assert verified.verified_count == 1
    assert fake_qb.calls == ["add", "select", "start", "stop", "remove"]
    assert (data_root / "movies" / "Movie.mkv").read_bytes() == b"abcdefgh"


def test_auxiliary_poll_survives_temporary_site_download_500_without_readding(
    tmp_path: Path,
) -> None:
    service, factory, fake_qb, data_root = _fixture(tmp_path)
    asyncio.run(service.advance_next_batch("execution-1"))
    site = cast(_SiteProvider, service._site_provider)._site
    site.transient_error = True

    pending = asyncio.run(service.advance_next_batch("execution-1"))
    assert pending.downloading_count == 1
    assert pending.error_count == 0
    assert fake_qb.calls == ["add", "select", "start"]
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == UnpackItemStatus.AUXILIARY_FETCHING.value
        assert item.auxiliary_state is not None
        assert item.auxiliary_state["state"] == "DOWNLOADING"
        assert (
            session.scalar(
                select(UnpackExternalOperationJournal.id).where(
                    UnpackExternalOperationJournal.operation_type == "UNPACK_AUX_TORRENT_STOP"
                )
            )
            is None
        )

    site.transient_error = False
    resumed = asyncio.run(service.advance_next_batch("execution-1"))
    assert resumed.verified_count == 1
    assert fake_qb.calls == ["add", "select", "start", "stop", "remove"]
    assert (data_root / "movies" / "Movie.mkv").read_bytes() == b"abcdefgh"


@pytest.mark.parametrize("already_added", [False, True])
def test_auxiliary_fetch_deadline_preserves_state_and_retries_on_next_worker_pass(
    tmp_path: Path,
    already_added: bool,
) -> None:
    service, factory, fake_qb, data_root = _fixture(tmp_path)
    if already_added:
        asyncio.run(service.advance_next_batch("execution-1"))
        assert fake_qb.calls == ["add", "select", "start"]

    site = cast(_SiteProvider, service._site_provider)._site
    site.delay_seconds = 0.05
    service._fetch_timeout_seconds = 0.001

    # asyncio.timeout() raises builtin TimeoutError, not SiteAdapterError.
    # A single slow site must not crash the auxiliary execution worker.
    report = asyncio.run(service.advance_next_batch("execution-1"))
    assert report.error_count == 0
    assert fake_qb.calls == (["add", "select", "start"] if already_added else [])
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == UnpackItemStatus.AUXILIARY_FETCHING.value
        assert item.auxiliary_state is not None
        assert item.auxiliary_state["state"] == (
            "DOWNLOADING" if already_added else "PENDING_FETCH"
        )
        assert len(session.scalars(select(UnpackExternalOperationJournal)).all()) == (
            4 if already_added else 0
        )
    assert (data_root / "movies" / "Movie.mkv").read_bytes() == b"abcdefgh"

    site.delay_seconds = 0
    service._fetch_timeout_seconds = 30.0
    recovered = asyncio.run(service.advance_next_batch("execution-1"))
    assert recovered.error_count == 0
    if already_added:
        assert recovered.verified_count == 1
        assert fake_qb.calls == ["add", "select", "start", "stop", "remove"]
    else:
        assert recovered.downloading_count == 1
        assert fake_qb.calls == ["add", "select", "start"]
    assert (data_root / "movies" / "Movie.mkv").read_bytes() == b"abcdefgh"


@pytest.mark.parametrize(
    ("operation", "journal_type", "failure_code"),
    (
        ("select", "UNPACK_AUX_FILE_SELECTION", "UNPACK_AUX_FILE_SELECTION_RECONCILE_REQUIRED"),
        ("start", "UNPACK_AUX_TORRENT_START", "UNPACK_AUX_START_RECONCILE_REQUIRED"),
        ("stop", "UNPACK_AUX_TORRENT_STOP", "UNPACK_AUX_STOP_RECONCILE_REQUIRED"),
        ("remove", "UNPACK_AUX_TORRENT_REMOVE", "UNPACK_AUX_REMOVE_RECONCILE_REQUIRED"),
    ),
)
def test_auxiliary_crash_after_external_intent_never_replays_rpc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    journal_type: str,
    failure_code: str,
) -> None:
    """An interrupted external RPC must not dispatch a second write on resume."""
    service, factory, fake_qb, _data_root = _fixture(tmp_path)
    methods = {
        "select": "set_file_selection",
        "start": "start_torrent",
        "stop": "stop_torrent",
        "remove": "remove_torrent_keep_files",
    }
    if operation in {"stop", "remove"}:
        asyncio.run(service.advance_next_batch("execution-1"))
        assert fake_qb.calls == ["add", "select", "start"]
    method = methods[operation]
    original = getattr(fake_qb, method)

    async def interrupted(*_args: object, **_kwargs: object) -> None:
        fake_qb._assert_intent(journal_type)
        fake_qb.calls.append(f"{operation}-interrupted")
        raise asyncio.CancelledError()

    monkeypatch.setattr(fake_qb, method, interrupted)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(service.advance_next_batch("execution-1"))
    with factory() as session:
        journal = session.scalar(
            select(UnpackExternalOperationJournal).where(
                UnpackExternalOperationJournal.operation_type == journal_type
            )
        )
        assert journal is not None
        assert journal.status == OperationStatus.INTENT_RECORDED.value

    monkeypatch.setattr(fake_qb, method, original)
    resumed = asyncio.run(service.advance_next_batch("execution-1"))
    assert resumed.error_count == 1
    assert fake_qb.calls.count(f"{operation}-interrupted") == 1
    assert fake_qb.calls.count(operation) == 0
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        journal = session.scalar(
            select(UnpackExternalOperationJournal).where(
                UnpackExternalOperationJournal.operation_type == journal_type
            )
        )
        assert item is not None and journal is not None
        assert item.last_error_code == failure_code
        assert journal.status == OperationStatus.RECONCILE_REQUIRED.value


@pytest.mark.parametrize(
    ("operation", "preflight_read", "journal_type"),
    (
        ("select", 3, "UNPACK_AUX_FILE_SELECTION"),
        ("start", 4, "UNPACK_AUX_TORRENT_START"),
        ("stop", 2, "UNPACK_AUX_TORRENT_STOP"),
        ("remove", 4, "UNPACK_AUX_TORRENT_REMOVE"),
    ),
)
def test_auxiliary_readonly_preflight_timeout_does_not_reserve_write_intent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    preflight_read: int,
    journal_type: str,
) -> None:
    """Read-only outages can be retried without poisoning an external write journal."""
    service, factory, fake_qb, _data_root = _fixture(tmp_path)
    if operation in {"stop", "remove"}:
        asyncio.run(service.advance_next_batch("execution-1"))
        assert fake_qb.calls == ["add", "select", "start"]
    original_get = fake_qb.get_torrents
    counter = 0

    async def unavailable_once(
        torrent_hashes: tuple[str, ...],
    ) -> tuple[QbittorrentTorrentState, ...]:
        nonlocal counter
        counter += 1
        if counter == preflight_read:
            raise TimeoutError("synthetic pre-write read-only status timeout")
        return await original_get(torrent_hashes)

    monkeypatch.setattr(fake_qb, "get_torrents", unavailable_once)
    pending = asyncio.run(service.advance_next_batch("execution-1"))
    assert pending.error_count == 0
    with factory() as session:
        journal = session.scalar(
            select(UnpackExternalOperationJournal).where(
                UnpackExternalOperationJournal.operation_type == journal_type
            )
        )
        assert journal is None

    resumed = asyncio.run(service.advance_next_batch("execution-1"))
    assert resumed.error_count == 0
    if operation in {"select", "start"}:
        assert resumed.downloading_count == 1
        assert fake_qb.calls == ["add", "select", "start"]
    else:
        assert resumed.verified_count == 1
        assert fake_qb.calls == ["add", "select", "start", "stop", "remove"]


def test_auxiliary_stale_verification_cannot_persist_to_newer_item(tmp_path: Path) -> None:
    """A previous worker cannot persist content proof after its item version changed."""
    service, factory, _fake_qb, _data_root = _fixture(tmp_path)
    stale_context = service._load_context("item-1")
    asyncio.run(service.advance_next_batch("execution-1"))
    with factory() as session, pytest.raises(ApplicationError) as exc:
        auxiliary_module._current_records(session, stale_context)
    assert exc.value.code == "UNPACK_AUXILIARY_CONFLICT"


def test_auxiliary_stale_error_cannot_fail_newer_checkpoint(tmp_path: Path) -> None:
    """An in-flight worker error must not cancel another worker's progress."""
    service, factory, fake_qb, _data_root = _fixture(tmp_path)
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        stale_version = item.version

    result = asyncio.run(service.advance_next_batch("execution-1"))
    assert result.error_count == 0
    assert fake_qb.calls == ["add", "select", "start"]
    service._mark_error(
        "item-1",
        code="UNPACK_AUX_STALE_WORKER_ERROR",
        message="迟到的工作器失败",
        expected_version=stale_version,
    )
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == UnpackItemStatus.AUXILIARY_FETCHING.value
        assert item.auxiliary_state is not None
        assert item.auxiliary_state["state"] == "DOWNLOADING"
        assert item.last_error_code == "UNPACK_AUXILIARY_FILES_REQUIRED"


def test_auxiliary_stale_worker_cannot_rewind_downloading_checkpoint(tmp_path: Path) -> None:
    """A late PREPARING checkpoint must not overwrite a worker's DOWNLOADING."""
    service, factory, fake_qb, _data_root = _fixture(tmp_path)
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        stale_state = {**dict(item.auxiliary_state or {}), "state": "PREPARING"}
        stale_version = item.version

    first = asyncio.run(service.advance_next_batch("execution-1"))
    assert first.error_count == 0
    assert fake_qb.calls == ["add", "select", "start"]

    assert (
        service._set_auxiliary_state("item-1", stale_state, expected_version=stale_version) is None
    )
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.auxiliary_state is not None
        assert item.auxiliary_state["state"] == "DOWNLOADING"
        assert item.version > stale_version


@pytest.mark.parametrize(
    ("operation_type", "late_error_code"),
    (
        ("UNPACK_AUX_STAGING_DIR", "UNPACK_AUX_STAGING_INVALID"),
        ("UNPACK_AUX_TORRENT_ADD", "UNPACK_AUX_ADD_RESULT_UNKNOWN"),
        ("UNPACK_AUX_FILE_SELECTION", "UNPACK_AUX_FILE_SELECTION_RESULT_UNKNOWN"),
        ("UNPACK_AUX_TORRENT_START", "UNPACK_AUX_START_RESULT_UNKNOWN"),
    ),
)
def test_auxiliary_late_reconcile_cannot_demote_applied_journal(
    tmp_path: Path, operation_type: str, late_error_code: str
) -> None:
    """Late failure from another worker must not overwrite proof of success."""
    service, factory, fake_qb, _data_root = _fixture(tmp_path)
    asyncio.run(service.advance_next_batch("execution-1"))
    assert fake_qb.calls == ["add", "select", "start"]
    with factory() as session:
        journal = session.scalar(
            select(UnpackExternalOperationJournal).where(
                UnpackExternalOperationJournal.operation_type == operation_type
            )
        )
        assert journal is not None
        assert journal.status == OperationStatus.APPLIED.value
        journal_id = journal.id
        original_snapshot = journal.after_snapshot

    # Worker B already confirmed APPLIED; worker A now reports a stale failure.
    service._mark_reconcile(journal_id, late_error_code)
    with factory() as session:
        journal = session.get(UnpackExternalOperationJournal, journal_id)
        assert journal is not None
        assert journal.status == OperationStatus.APPLIED.value
        assert journal.after_snapshot == original_snapshot
        assert journal.last_error_code is None

    # Re-entering should still reuse all four applied intents, without writes.
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        item.auxiliary_state = {**dict(item.auxiliary_state or {}), "state": "PREPARING"}
        session.commit()
    resumed = asyncio.run(service.advance_next_batch("execution-1"))
    assert resumed.error_count == 0
    assert fake_qb.calls == ["add", "select", "start"]


def test_auxiliary_crash_after_add_intent_cannot_blindly_resend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An interrupted RPC leaves INTENT; no observed torrent means reconcile."""
    service, factory, fake_qb, data_root = _fixture(tmp_path)
    original_add = fake_qb.add_torrent

    async def interrupted_add(_request: QbittorrentAddRequest) -> QbittorrentAddResult:
        fake_qb._assert_intent("UNPACK_AUX_TORRENT_ADD")
        fake_qb.calls.append("uncertain-add")
        # Simulate process cancellation after RPC dispatch but before receipt.
        raise asyncio.CancelledError()

    monkeypatch.setattr(fake_qb, "add_torrent", interrupted_add)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(service.advance_next_batch("execution-1"))

    with factory() as session:
        journal = session.scalar(
            select(UnpackExternalOperationJournal).where(
                UnpackExternalOperationJournal.operation_type == "UNPACK_AUX_TORRENT_ADD"
            )
        )
        assert journal is not None
        assert journal.status == OperationStatus.INTENT_RECORDED.value
    assert fake_qb.state is None
    assert fake_qb.calls == ["uncertain-add"]
    monkeypatch.setattr(fake_qb, "add_torrent", original_add)

    report = asyncio.run(service.advance_next_batch("execution-1"))
    assert report.error_count == 1
    assert fake_qb.calls == ["uncertain-add"]
    assert (data_root / "movies" / "Movie.mkv").read_bytes() == b"abcdefgh"
    with factory() as session:
        journal = session.scalar(
            select(UnpackExternalOperationJournal).where(
                UnpackExternalOperationJournal.operation_type == "UNPACK_AUX_TORRENT_ADD"
            )
        )
        assert journal is not None
        assert journal.status == OperationStatus.RECONCILE_REQUIRED.value
        assert journal.last_error_code == "UNPACK_AUX_ADD_INTENT_UNCONFIRMED"


def test_auxiliary_unknown_add_timeout_requires_reconciliation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, factory, fake_qb, data_root = _fixture(tmp_path)

    async def uncertain_add(_request: QbittorrentAddRequest) -> QbittorrentAddResult:
        # The downloader may have received the write before its response was
        # lost. This cannot be silently classified as a site read timeout.
        fake_qb.calls.append("add-unknown")
        raise TimeoutError("synthetic unknown downloader write result")

    monkeypatch.setattr(fake_qb, "add_torrent", uncertain_add)
    first = asyncio.run(service.advance_next_batch("execution-1"))
    assert first.downloading_count == 1
    assert fake_qb.calls == ["add-unknown"]
    assert (data_root / "movies" / "Movie.mkv").read_bytes() == b"abcdefgh"
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        journal = session.scalar(
            select(UnpackExternalOperationJournal).where(
                UnpackExternalOperationJournal.operation_type == "UNPACK_AUX_TORRENT_ADD"
            )
        )
        assert item is not None
        assert item.status == UnpackItemStatus.AUXILIARY_FETCHING.value
        assert journal is not None
        assert journal.status == OperationStatus.RECONCILE_REQUIRED.value
        assert journal.last_error_code == "UNPACK_AUX_ADD_RESULT_UNKNOWN"
    followup = asyncio.run(service.advance_next_batch("execution-1"))
    assert followup.error_count == 1
    assert fake_qb.calls == ["add-unknown"]


@pytest.mark.parametrize("error_type", ["adapter", "native"])
def test_auxiliary_unknown_file_selection_is_not_replayed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_type: str
) -> None:
    service, factory, fake_qb, _data_root = _fixture(tmp_path)

    async def uncertain_select(
        _hash: str, *, wanted: tuple[int, ...], unwanted: tuple[int, ...]
    ) -> None:
        fake_qb.calls.append("select-unknown")
        assert wanted == (1,) and unwanted == (0,)
        if error_type == "native":
            raise TimeoutError("synthetic selection outcome unknown")
        raise DownloaderAdapterError(
            "DOWNLOADER_UNAVAILABLE", "synthetic selection outcome unknown"
        )

    monkeypatch.setattr(fake_qb, "set_file_selection", uncertain_select)
    first = asyncio.run(service.advance_next_batch("execution-1"))
    assert first.downloading_count == 1
    assert fake_qb.calls == ["add", "select-unknown"]
    with factory() as session:
        journal = session.scalar(
            select(UnpackExternalOperationJournal).where(
                UnpackExternalOperationJournal.operation_type == "UNPACK_AUX_FILE_SELECTION"
            )
        )
        assert journal is not None
        assert journal.status == OperationStatus.RECONCILE_REQUIRED.value
    second = asyncio.run(service.advance_next_batch("execution-1"))
    assert second.error_count == 1
    assert fake_qb.calls == ["add", "select-unknown"]


@pytest.mark.parametrize("error_type", ["adapter", "native"])
def test_auxiliary_unknown_start_is_not_replayed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_type: str
) -> None:
    service, factory, fake_qb, _data_root = _fixture(tmp_path)

    async def uncertain_start(_hash: str) -> None:
        fake_qb.calls.append("start-unknown")
        if error_type == "native":
            raise TimeoutError("synthetic start outcome unknown")
        raise DownloaderAdapterError("DOWNLOADER_UNAVAILABLE", "synthetic start outcome unknown")

    monkeypatch.setattr(fake_qb, "start_torrent", uncertain_start)
    first = asyncio.run(service.advance_next_batch("execution-1"))
    assert first.downloading_count == 1
    assert fake_qb.calls == ["add", "select", "start-unknown"]
    with factory() as session:
        journal = session.scalar(
            select(UnpackExternalOperationJournal).where(
                UnpackExternalOperationJournal.operation_type == "UNPACK_AUX_TORRENT_START"
            )
        )
        assert journal is not None
        assert journal.status == OperationStatus.RECONCILE_REQUIRED.value
    second = asyncio.run(service.advance_next_batch("execution-1"))
    assert second.error_count == 1
    assert fake_qb.calls == ["add", "select", "start-unknown"]


@pytest.mark.parametrize("operation", ["stop", "remove"])
def test_auxiliary_native_timeout_on_cleanup_never_replays_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    service, factory, fake_qb, _data_root = _fixture(tmp_path)
    asyncio.run(service.advance_next_batch("execution-1"))
    assert fake_qb.calls == ["add", "select", "start"]

    async def uncertain_write(_hash: str) -> None:
        fake_qb.calls.append(operation + "-unknown")
        raise TimeoutError("synthetic cleanup write outcome unknown")

    if operation == "stop":
        monkeypatch.setattr(fake_qb, "stop_torrent", uncertain_write)
        op_type = "UNPACK_AUX_TORRENT_STOP"
    else:
        monkeypatch.setattr(fake_qb, "remove_torrent_keep_files", uncertain_write)
        op_type = "UNPACK_AUX_TORRENT_REMOVE"

    first = asyncio.run(service.advance_next_batch("execution-1"))
    assert first.downloading_count == 1
    assert fake_qb.calls.count(operation + "-unknown") == 1
    with factory() as session:
        journal = session.scalar(
            select(UnpackExternalOperationJournal).where(
                UnpackExternalOperationJournal.operation_type == op_type
            )
        )
        assert journal is not None
        assert journal.status == OperationStatus.RECONCILE_REQUIRED.value

    second = asyncio.run(service.advance_next_batch("execution-1"))
    assert second.error_count == 1
    assert fake_qb.calls.count(operation + "-unknown") == 1


@pytest.mark.parametrize(
    ("probe_number", "operation_type"),
    (
        (2, "UNPACK_AUX_TORRENT_ADD"),
        (5, "UNPACK_AUX_TORRENT_START"),
    ),
)
def test_auxiliary_write_ack_followed_by_native_probe_timeout_requires_reconcile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe_number: int,
    operation_type: str,
) -> None:
    service, factory, fake_qb, _data_root = _fixture(tmp_path)
    original_get = fake_qb.get_torrents
    probes = 0

    async def timed_get(hashes: tuple[str, ...]) -> tuple[QbittorrentTorrentState, ...]:
        nonlocal probes
        probes += 1
        if probes == probe_number:
            raise TimeoutError("synthetic post-write status response unknown")
        return await original_get(hashes)

    monkeypatch.setattr(fake_qb, "get_torrents", timed_get)
    report = asyncio.run(service.advance_next_batch("execution-1"))
    assert report.downloading_count == 1
    with factory() as session:
        journal = session.scalar(
            select(UnpackExternalOperationJournal).where(
                UnpackExternalOperationJournal.operation_type == operation_type
            )
        )
        assert journal is not None
        assert journal.status == OperationStatus.RECONCILE_REQUIRED.value

    # The remote write did happen; a fresh owned state may safely reconcile
    # the existing journal without dispatching ADD/START a second time.
    resumed = asyncio.run(service.advance_next_batch("execution-1"))
    assert resumed.error_count == 0
    assert fake_qb.calls == ["add", "select", "start"]


def test_auxiliary_restart_reconciles_applied_operations_without_repeating_writes(
    tmp_path: Path,
) -> None:
    service, factory, fake_qb, _data_root = _fixture(tmp_path)

    asyncio.run(service.advance_next_batch("execution-1"))
    assert fake_qb.calls == ["add", "select", "start"]
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        item.auxiliary_state = {
            **dict(item.auxiliary_state or {}),
            "state": "PREPARING",
        }
        session.commit()

    report = asyncio.run(service.advance_next_batch("execution-1"))

    assert report.downloading_count == 1
    assert fake_qb.calls == ["add", "select", "start"]
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.auxiliary_state is not None
        assert item.auxiliary_state["state"] == "DOWNLOADING"


def test_applied_add_with_missing_external_torrent_requires_reconcile_without_readd(
    tmp_path: Path,
) -> None:
    service, factory, fake_qb, _data_root = _fixture(tmp_path)

    asyncio.run(service.advance_next_batch("execution-1"))
    assert fake_qb.calls == ["add", "select", "start"]
    fake_qb.state = None
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        item.auxiliary_state = {
            **dict(item.auxiliary_state or {}),
            "state": "PREPARING",
        }
        session.commit()

    report = asyncio.run(service.advance_next_batch("execution-1"))

    assert report.error_count == 1
    assert fake_qb.calls == ["add", "select", "start"]
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == UnpackItemStatus.MATCH_ERROR.value
        assert item.last_error_code == "UNPACK_AUX_APPLIED_TORRENT_MISSING"
        add_journal = session.scalar(
            select(UnpackExternalOperationJournal).where(
                UnpackExternalOperationJournal.operation_type == "UNPACK_AUX_TORRENT_ADD"
            )
        )
        assert add_journal is not None
        assert add_journal.status == OperationStatus.RECONCILE_REQUIRED.value


def test_auxiliary_applied_staging_directory_swapped_in_place_requires_reconcile(
    tmp_path: Path,
) -> None:
    service, factory, fake_qb, _data_root = _fixture(tmp_path)
    asyncio.run(service.advance_next_batch("execution-1"))
    assert fake_qb.calls == ["add", "select", "start"]
    with factory() as session:
        journal = session.scalar(
            select(UnpackExternalOperationJournal).where(
                UnpackExternalOperationJournal.operation_type == "UNPACK_AUX_STAGING_DIR"
            )
        )
        assert journal is not None
        path = Path(journal.target["path"])
        original_stat = path.lstat()
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        item.auxiliary_state = {**dict(item.auxiliary_state or {}), "state": "PREPARING"}
        session.commit()
    original = path.with_name(path.name + "-original-preserved")
    path.rename(original)
    path.mkdir()
    assert path.stat().st_ino != original_stat.st_ino

    result = asyncio.run(service.advance_next_batch("execution-1"))
    assert result.error_count == 1
    assert fake_qb.calls == ["add", "select", "start"]
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == UnpackItemStatus.MATCH_ERROR.value
        journal = session.scalar(
            select(UnpackExternalOperationJournal).where(
                UnpackExternalOperationJournal.operation_type == "UNPACK_AUX_STAGING_DIR"
            )
        )
        assert journal is not None
        assert journal.status == OperationStatus.RECONCILE_REQUIRED.value
        assert journal.last_error_code == "UNPACK_AUX_STAGING_IDENTITY_CHANGED"


def test_auxiliary_existing_staging_directory_with_unconfirmed_intent_stops(
    tmp_path: Path,
) -> None:
    service, factory, fake_qb, _data_root = _fixture(tmp_path)
    asyncio.run(service.advance_next_batch("execution-1"))
    with factory() as session:
        journal = session.scalar(
            select(UnpackExternalOperationJournal).where(
                UnpackExternalOperationJournal.operation_type == "UNPACK_AUX_STAGING_DIR"
            )
        )
        assert journal is not None
        # Crash after mkdir before APPLIED: cannot prove creator of the
        # directory merely from the pre-write INTENT journal.
        journal.status = OperationStatus.INTENT_RECORDED.value
        journal.after_snapshot = None
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        item.auxiliary_state = {**dict(item.auxiliary_state or {}), "state": "PREPARING"}
        session.commit()

    result = asyncio.run(service.advance_next_batch("execution-1"))
    assert result.error_count == 1
    assert fake_qb.calls == ["add", "select", "start"]
    with factory() as session:
        journal = session.scalar(
            select(UnpackExternalOperationJournal).where(
                UnpackExternalOperationJournal.operation_type == "UNPACK_AUX_STAGING_DIR"
            )
        )
        assert journal is not None
        assert journal.status == OperationStatus.RECONCILE_REQUIRED.value
        assert journal.last_error_code == "UNPACK_AUX_STAGING_CREATION_UNCONFIRMED"


def test_auxiliary_legacy_applied_directory_without_inode_cannot_be_adopted(
    tmp_path: Path,
) -> None:
    service, factory, fake_qb, _data_root = _fixture(tmp_path)
    asyncio.run(service.advance_next_batch("execution-1"))
    with factory() as session:
        journal = session.scalar(
            select(UnpackExternalOperationJournal).where(
                UnpackExternalOperationJournal.operation_type == "UNPACK_AUX_STAGING_DIR"
            )
        )
        assert journal is not None
        assert isinstance(journal.after_snapshot, dict)
        # A legacy record only attests a pathname and cannot establish
        # directory ownership after restarts or external replacement.
        journal.after_snapshot = {"path": journal.target["path"]}
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        item.auxiliary_state = {**dict(item.auxiliary_state or {}), "state": "PREPARING"}
        session.commit()

    report = asyncio.run(service.advance_next_batch("execution-1"))
    assert report.error_count == 1
    assert fake_qb.calls == ["add", "select", "start"]
    with factory() as session:
        journal = session.scalar(
            select(UnpackExternalOperationJournal).where(
                UnpackExternalOperationJournal.operation_type == "UNPACK_AUX_STAGING_DIR"
            )
        )
        assert journal is not None
        assert journal.status == OperationStatus.RECONCILE_REQUIRED.value
        assert journal.last_error_code == "UNPACK_AUX_STAGING_IDENTITY_CHANGED"
