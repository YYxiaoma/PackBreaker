import asyncio
import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.downloaders import QbittorrentWriteBinding
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
                    "output_config": {
                        "output_directory": output.as_posix(),
                        "storage_mode": "HARDLINK",
                        "conflict_policy": "VERIFY_REUSE_OR_STOP",
                        "target_downloader_id": "downloader-target",
                    }
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
