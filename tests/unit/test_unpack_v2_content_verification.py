import asyncio
import hashlib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.sites import EnabledSiteAdapter
from backend.app.application.unpack_content_verification import (
    UnpackContentVerificationService,
)
from backend.app.application.unpack_item_actions import UnpackItemActionService
from backend.app.domain.site_adapter import SiteAdapter, TorrentPayload
from backend.app.domain.unpack import (
    UnpackCandidateVerificationStatus,
    UnpackExecutionStatus,
    UnpackItemStatus,
    UnpackReviewDecision,
)
from backend.app.domain.verification import VerificationLevel
from backend.app.infrastructure.authorized_paths import AuthorizedPathScope
from backend.app.infrastructure.persistence.base import Base
from backend.app.infrastructure.persistence.models import (
    UnpackDefinition,
    UnpackExecution,
    UnpackExecutionItem,
    UnpackMatchCandidate,
    utc_now,
)
from backend.app.infrastructure.source_inventory import current_file_snapshot


class _FakeSiteAdapter:
    def __init__(self, torrent: bytes) -> None:
        self._torrent = torrent

    async def fetch_torrent(self, torrent_id: str) -> TorrentPayload:
        return TorrentPayload("fake-site", torrent_id, self._torrent)


class _FakeSiteProvider:
    def __init__(self, torrent: bytes) -> None:
        self._adapter = _FakeSiteAdapter(torrent)

    def enabled_adapters(self) -> tuple[EnabledSiteAdapter, ...]:
        return (
            EnabledSiteAdapter(
                config_id="site-config-1",
                config_version=7,
                site_id="fake-site",
                adapter=cast(SiteAdapter, self._adapter),
            ),
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


def _v1_single(name: str, content: bytes, *, expected: bytes | None = None) -> bytes:
    payload = expected if expected is not None else content
    piece_length = 4
    pieces = b"".join(
        hashlib.sha1(payload[offset : offset + piece_length]).digest()
        for offset in range(0, len(payload), piece_length)
    )
    return _bencode(
        {
            b"info": {
                b"length": len(payload),
                b"name": name.encode(),
                b"piece length": piece_length,
                b"pieces": pieces,
            }
        }
    )


def _v1_multi(files: tuple[tuple[str, bytes], ...]) -> bytes:
    piece_length = 4
    stream = b"".join(content for _name, content in files)
    pieces = b"".join(
        hashlib.sha1(stream[offset : offset + piece_length]).digest()
        for offset in range(0, len(stream), piece_length)
    )
    return _bencode(
        {
            b"info": {
                b"name": b"Pack",
                b"piece length": piece_length,
                b"pieces": pieces,
                b"files": [
                    {
                        b"length": len(content),
                        b"path": [name.encode()],
                    }
                    for name, content in files
                ],
            }
        }
    )


def _service(
    tmp_path: Path,
    *,
    source_content: bytes,
    torrent: bytes,
) -> tuple[
    UnpackContentVerificationService,
    sessionmaker[Session],
    Path,
]:
    data_root = tmp_path / "data"
    data_root.mkdir()
    source = data_root / "movie.mkv"
    source.write_bytes(source_content)
    snapshot = current_file_snapshot(source)

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
                name="内容验证测试",
                trigger_kind="MANUAL",
                status="PENDING_EXECUTION",
                source_kind="DIRECTORY",
                execution_scope_kind="ALL_MATCHING_MEDIA",
                source_config={"directory_path": data_root.as_posix()},
                file_filter={"extensions": [".mkv"]},
                site_ids=["site-config-1"],
                output_config={},
                retry_enabled=True,
                max_retries=3,
                auto_match_threshold_bps=10_000,
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
                config_snapshot={},
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
                    "relative_path": source.name,
                    "device": snapshot.device,
                    "inode": snapshot.inode,
                    "size": snapshot.size,
                    "mtime_ns": str(snapshot.mtime_ns),
                    "file_type": snapshot.file_type,
                },
                media_identity={},
                status=UnpackItemStatus.MATCHED_AUTO.value,
                selected_candidate_id="candidate-1",
                candidate_generation=1,
                retry_count=0,
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
                size_bytes=len(source_content),
                score_bps=10_000,
                is_exact_match=True,
                evidence={},
                verification_status=UnpackCandidateVerificationStatus.NOT_CHECKED.value,
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

    service = UnpackContentVerificationService(
        factory,
        _FakeSiteProvider(torrent),
        path_scope=AuthorizedPathScope.legacy_only(legacy_data_root=data_root),
    )
    return service, factory, source


def test_full_verified_single_file_candidate(tmp_path: Path) -> None:
    content = b"abcdefgh"
    service, factory, _source = _service(
        tmp_path,
        source_content=content,
        torrent=_v1_single("movie.mkv", content),
    )

    report = asyncio.run(service.verify_next_batch("execution-1"))

    assert report.processed_count == 1
    assert report.content_verified_count == 1
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        candidate = session.get(UnpackMatchCandidate, "candidate-1")
        assert item is not None
        assert candidate is not None
        assert item.status == UnpackItemStatus.CONTENT_VERIFIED.value
        assert item.content_verification_level == VerificationLevel.FULL_VERIFIED.value
        assert item.torrent_metainfo_digest
        assert candidate.verification_status == UnpackCandidateVerificationStatus.VERIFIED.value
        assert candidate.verification_level == VerificationLevel.FULL_VERIFIED.value


def test_review_during_read_only_verification_makes_old_claim_stale_without_overwrite(
    tmp_path: Path,
) -> None:
    content = b"abcdefgh"
    service, factory, _source = _service(
        tmp_path,
        source_content=content,
        torrent=_v1_single("movie.mkv", content),
    )
    claim = service._claim_next_item("execution-1")
    assert claim is not None

    review = UnpackItemActionService(factory).review(
        "item-1",
        decision=UnpackReviewDecision.APPROVE,
        candidate_id="candidate-1",
        generation=1,
        expected_item_version=2,
        idempotency_key="review-during-verification",
    )
    assert review.item_status is UnpackItemStatus.MATCHED_MANUAL

    asyncio.run(service._verify_claim(claim))

    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == UnpackItemStatus.MATCHED_MANUAL.value
        assert item.content_verification_level is None
        assert item.torrent_metainfo_digest is None


def test_piece_mismatch_blocks_candidate(tmp_path: Path) -> None:
    source_content = b"abcdefgh"
    expected = b"abcdWXYZ"
    service, factory, _source = _service(
        tmp_path,
        source_content=source_content,
        torrent=_v1_single("movie.mkv", source_content, expected=expected),
    )

    report = asyncio.run(service.verify_next_batch("execution-1"))

    assert report.content_mismatch_count == 1
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        candidate = session.get(UnpackMatchCandidate, "candidate-1")
        assert item is not None
        assert candidate is not None
        assert item.status == UnpackItemStatus.CONTENT_MISMATCH.value
        assert item.content_verification_level == VerificationLevel.BLOCKED.value
        assert item.last_error_code == "UNPACK_CONTENT_MISMATCH"
        assert candidate.verification_status == UnpackCandidateVerificationStatus.MISMATCH.value


def test_missing_auxiliary_files_are_pending_not_content_mismatch(tmp_path: Path) -> None:
    content = b"abcdefgh"
    torrent = _v1_multi(
        (
            ("movie.mkv", content),
            ("movie.nfo", b"nfo"),
            ("poster.jpg", b"jpg"),
        )
    )
    service, factory, _source = _service(
        tmp_path,
        source_content=content,
        torrent=torrent,
    )

    report = asyncio.run(service.verify_next_batch("execution-1"))

    assert report.auxiliary_pending_count == 1
    assert report.content_mismatch_count == 0
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        candidate = session.get(UnpackMatchCandidate, "candidate-1")
        assert item is not None
        assert candidate is not None
        assert item.status == UnpackItemStatus.AUXILIARY_FETCHING.value
        assert item.content_verification_level == VerificationLevel.CLIENT_CHECK_REQUIRED.value
        assert item.last_error_code == "UNPACK_AUXILIARY_FILES_REQUIRED"
        assert item.auxiliary_state is not None
        assert len(item.auxiliary_state["missing_paths"]) == 2
        assert candidate.verification_status == UnpackCandidateVerificationStatus.UNAVAILABLE.value


def test_missing_second_video_is_blocked_not_whitelisted_as_auxiliary(tmp_path: Path) -> None:
    content = b"abcdefgh"
    torrent = _v1_multi(
        (
            ("movie.mkv", content),
            ("movie-extra.mkv", b"more"),
        )
    )
    service, factory, _source = _service(
        tmp_path,
        source_content=content,
        torrent=torrent,
    )

    report = asyncio.run(service.verify_next_batch("execution-1"))

    assert report.content_mismatch_count == 1
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == UnpackItemStatus.CONTENT_MISMATCH.value
        assert item.last_error_code == "UNPACK_CONTENT_REQUIRED_FILE_MISSING"
        assert item.content_verification_level == VerificationLevel.BLOCKED.value


def test_source_snapshot_change_stops_before_piece_verification(tmp_path: Path) -> None:
    content = b"abcdefgh"
    service, factory, source = _service(
        tmp_path,
        source_content=content,
        torrent=_v1_single("movie.mkv", content),
    )
    source.write_bytes(b"changed!")

    report = asyncio.run(service.verify_next_batch("execution-1"))

    assert report.error_count == 1
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        candidate = session.get(UnpackMatchCandidate, "candidate-1")
        assert item is not None
        assert candidate is not None
        assert item.status == UnpackItemStatus.MATCH_ERROR.value
        assert item.last_error_code == "SOURCE_SNAPSHOT_CHANGED"
        assert candidate.verification_status == UnpackCandidateVerificationStatus.UNAVAILABLE.value


def test_auto_match_with_stale_candidate_returns_to_review(tmp_path: Path) -> None:
    content = b"abcdefgh"
    service, factory, _source = _service(
        tmp_path,
        source_content=content,
        torrent=_v1_single("movie.mkv", content),
    )
    with factory() as session:
        candidate = session.get(UnpackMatchCandidate, "candidate-1")
        assert candidate is not None
        candidate.generation = 0
        session.commit()

    report = asyncio.run(service.verify_next_batch("execution-1"))

    assert report.review_count == 1
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == UnpackItemStatus.REVIEW_REQUIRED.value
        assert item.last_error_code == "UNPACK_VERIFY_CANDIDATE_STALE"


def test_manual_match_with_stale_candidate_becomes_match_error(tmp_path: Path) -> None:
    content = b"abcdefgh"
    service, factory, _source = _service(
        tmp_path,
        source_content=content,
        torrent=_v1_single("movie.mkv", content),
    )
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        candidate = session.get(UnpackMatchCandidate, "candidate-1")
        assert item is not None
        assert candidate is not None
        item.status = UnpackItemStatus.MATCHED_MANUAL.value
        candidate.generation = 0
        session.commit()

    report = asyncio.run(service.verify_next_batch("execution-1"))

    assert report.error_count == 1
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == UnpackItemStatus.MATCH_ERROR.value
        assert item.last_error_code == "UNPACK_VERIFY_CANDIDATE_STALE"
