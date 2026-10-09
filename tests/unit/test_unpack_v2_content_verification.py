import asyncio
import hashlib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.application import unpack_content_verification as verification_module
from backend.app.application.sites import EnabledSiteAdapter
from backend.app.application.unpack_content_verification import (
    UnpackContentVerificationService,
)
from backend.app.application.unpack_item_actions import UnpackItemActionService
from backend.app.application.unpack_matching import UnpackMatchCoordinator
from backend.app.domain.site_adapter import SiteAdapter, TorrentPayload
from backend.app.domain.site_search import (
    SearchPage,
    SearchQuery,
    SiteSearchCapabilities,
    normalize_candidate_meta,
)
from backend.app.domain.unpack import (
    UnpackCandidateVerificationStatus,
    UnpackExecutionStatus,
    UnpackItemStatus,
    UnpackReviewDecision,
)
from backend.app.domain.verification import VerificationLevel
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


class _FakeSiteAdapter:
    def __init__(self, torrent: bytes | dict[str, bytes]) -> None:
        self._torrent = torrent
        self.fetched_ids: list[str] = []

    async def fetch_torrent(self, torrent_id: str) -> TorrentPayload:
        self.fetched_ids.append(torrent_id)
        content = self._torrent[torrent_id] if isinstance(self._torrent, dict) else self._torrent
        return TorrentPayload("fake-site", torrent_id, content)


class _FakeSiteProvider:
    def __init__(self, torrent: bytes | dict[str, bytes]) -> None:
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
    torrent: bytes | dict[str, bytes],
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


def _add_auto_alternatives(
    factory: sessionmaker[Session],
    *,
    count: int = 1,
    threshold: int = 8000,
    score: int = 9000,
    conflicts: list[str] | None = None,
) -> None:
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        execution = session.get(UnpackExecution, "execution-1")
        assert item is not None and execution is not None
        item.match_origin = "AUTO"
        execution.config_snapshot = {"matching": {"auto_match_threshold_bps": threshold}}
        for index in range(count):
            session.add(
                UnpackMatchCandidate(
                    id=f"candidate-{index + 2}",
                    item_id=item.id,
                    generation=item.candidate_generation,
                    site_id="site-config-1",
                    candidate_key=f"torrent-{index + 2}",
                    title="Movie",
                    size_bytes=8,
                    score_bps=score - index,
                    is_exact_match=False,
                    evidence={
                        "hard_conflicts": list(conflicts or []),
                        "exact": {"title": True, "size": True},
                    },
                    verification_status=UnpackCandidateVerificationStatus.NOT_CHECKED.value,
                    raw_ref={
                        "torrent_id": f"torrent-{index + 2}",
                        "adapter_site_id": "fake-site",
                        "site_config_id": "site-config-1",
                        "site_config_version": 7,
                    },
                    created_at=utc_now(),
                )
            )
        session.commit()


def test_content_mismatch_automatically_verifies_next_safe_candidate(tmp_path: Path) -> None:
    source = b"abcdefgh"
    service, factory, _path = _service(
        tmp_path,
        source_content=source,
        torrent={
            "torrent-1": _v1_single("movie.mkv", source, expected=b"abcdWXYZ"),
            "torrent-2": _v1_single("movie.mkv", source),
        },
    )
    _add_auto_alternatives(factory)

    report = asyncio.run(service.verify_next_batch("execution-1", limit=5))

    assert report.processed_count == 2
    assert report.content_verified_count == 1
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        first = session.get(UnpackMatchCandidate, "candidate-1")
        second = session.get(UnpackMatchCandidate, "candidate-2")
        assert item is not None and first is not None and second is not None
        assert item.status == UnpackItemStatus.CONTENT_VERIFIED.value
        assert item.content_verification_level == VerificationLevel.FULL_VERIFIED.value
        assert item.selected_candidate_id == second.id
        assert item.match_origin == "AUTO"
        assert item.candidate_generation == 1
        assert first.verification_status == UnpackCandidateVerificationStatus.MISMATCH.value
        assert first.verification_error_code == "UNPACK_CONTENT_MISMATCH"
        assert second.verification_status == UnpackCandidateVerificationStatus.VERIFIED.value


def test_real_matching_service_evidence_drives_verified_alternative(
    tmp_path: Path,
) -> None:
    """Exercise production search → persisted evidence → hash → auto fallback."""
    movie_bytes = b"abcdefgh"
    torrent_data = {
        "torrent-1": _v1_single("movie.mkv", movie_bytes, expected=b"abcdWXYZ"),
        "torrent-2": _v1_single("movie.mkv", movie_bytes),
    }
    _initial_verifier, factory, source = _service(
        tmp_path, source_content=movie_bytes, torrent=torrent_data
    )
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        execution = session.get(UnpackExecution, "execution-1")
        assert item is not None and execution is not None
        # Reset the fixture to the actual discovery output. The matcher must
        # produce its own persisted records and evidence; no hand-built backup.
        session.execute(delete(UnpackMatchCandidate))
        item.status = UnpackItemStatus.MATCH_PENDING.value
        item.selected_candidate_id = None
        item.match_origin = None
        item.candidate_generation = 0
        execution.status = UnpackExecutionStatus.MATCHING.value
        execution.config_snapshot = {
            "site_ids": ["site-config-1"],
            "matching": {"auto_match_threshold_bps": 0},
        }
        session.commit()

    class ProductionSearchAdapter(_FakeSiteAdapter):
        async def capabilities(self) -> SiteSearchCapabilities:
            return SiteSearchCapabilities()

        async def search(self, _query: SearchQuery) -> SearchPage:
            return SearchPage(
                "fake-site",
                1,
                (
                    normalize_candidate_meta(
                        site_id="fake-site",
                        torrent_id="torrent-1",
                        display_name="Movie",
                        total_size=len(movie_bytes),
                        seeders=10,
                    ),
                    normalize_candidate_meta(
                        site_id="fake-site",
                        torrent_id="torrent-2",
                        display_name="Movie",
                        total_size=len(movie_bytes),
                        seeders=8,
                    ),
                ),
                False,
            )

    provider = _FakeSiteProvider(torrent_data)
    adapter = ProductionSearchAdapter(torrent_data)
    provider._adapter = adapter
    matched = asyncio.run(UnpackMatchCoordinator(factory, provider).match_next_batch("execution-1"))
    assert matched.matched_auto_count == 1
    with factory() as session:
        candidate_records = tuple(
            session.scalars(
                select(UnpackMatchCandidate).order_by(UnpackMatchCandidate.candidate_key)
            )
        )
        assert len(candidate_records) == 2
        assert all(record.evidence["hard_conflicts"] == [] for record in candidate_records)
        assert all(record.evidence["exact"]["title"] for record in candidate_records)
        assert all(record.evidence["exact"]["size"] for record in candidate_records)
        assert all(record.generation == 1 for record in candidate_records)

    verifier = UnpackContentVerificationService(
        factory,
        provider,
        path_scope=AuthorizedPathScope.legacy_only(legacy_data_root=source.parent),
    )
    verified = asyncio.run(verifier.verify_next_batch("execution-1", limit=5))
    assert verified.processed_count == 2
    assert verified.content_verified_count == 1
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None and item.selected_candidate_id is not None
        chosen = session.get(UnpackMatchCandidate, item.selected_candidate_id)
        assert chosen is not None
        assert chosen.candidate_key == "torrent-2"
        assert chosen.verification_status == UnpackCandidateVerificationStatus.VERIFIED.value
        assert item.content_verification_level == VerificationLevel.FULL_VERIFIED.value
    assert adapter.fetched_ids == ["torrent-1", "torrent-2"]
    assert source.read_bytes() == movie_bytes


def test_manual_choice_never_automatically_falls_back_on_hash_mismatch(tmp_path: Path) -> None:
    source = b"abcdefgh"
    service, factory, _path = _service(
        tmp_path,
        source_content=source,
        torrent={"torrent-1": _v1_single("movie.mkv", source, expected=b"abcdWXYZ")},
    )
    _add_auto_alternatives(factory)
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        item.status = UnpackItemStatus.MATCHED_MANUAL.value
        item.match_origin = "MANUAL"
        session.commit()

    report = asyncio.run(service.verify_next_batch("execution-1", limit=5))

    assert report.processed_count == 1
    assert report.content_mismatch_count == 1
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        untouched = session.get(UnpackMatchCandidate, "candidate-2")
        assert item is not None and untouched is not None
        assert item.selected_candidate_id == "candidate-1"
        assert item.match_origin == "MANUAL"
        assert untouched.verification_status == UnpackCandidateVerificationStatus.NOT_CHECKED.value


@pytest.mark.parametrize("threshold, conflicts", ((9500, None), (8000, ["YEAR"])))
def test_untrusted_or_below_threshold_backup_cannot_auto_select(
    tmp_path: Path, threshold: int, conflicts: list[str] | None
) -> None:
    source = b"abcdefgh"
    service, factory, _path = _service(
        tmp_path,
        source_content=source,
        torrent={"torrent-1": _v1_single("movie.mkv", source, expected=b"abcdWXYZ")},
    )
    _add_auto_alternatives(factory, threshold=threshold, conflicts=conflicts)

    report = asyncio.run(service.verify_next_batch("execution-1", limit=5))

    assert report.processed_count == 1
    assert report.content_mismatch_count == 1
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None and item.selected_candidate_id == "candidate-1"


def test_auto_fallback_attempts_are_bounded_even_with_extra_candidates(tmp_path: Path) -> None:
    source = b"abcdefgh"
    incorrect = _v1_single("movie.mkv", source, expected=b"abcdWXYZ")
    service, factory, _path = _service(
        tmp_path,
        source_content=source,
        torrent={
            "torrent-1": incorrect,
            "torrent-2": incorrect,
            "torrent-3": incorrect,
            "torrent-4": _v1_single("movie.mkv", source),
        },
    )
    _add_auto_alternatives(factory, count=3)

    report = asyncio.run(service.verify_next_batch("execution-1", limit=10))

    assert report.processed_count == 3
    assert report.content_mismatch_count == 1
    assert report.content_verified_count == 0
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        fourth = session.get(UnpackMatchCandidate, "candidate-4")
        assert item is not None and fourth is not None
        assert item.selected_candidate_id == "candidate-3"
        assert fourth.verification_status == UnpackCandidateVerificationStatus.NOT_CHECKED.value


def test_site_fetch_timeout_does_not_switch_to_another_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = b"abcdefgh"
    service, factory, _path = _service(
        tmp_path,
        source_content=source,
        torrent=_v1_single("movie.mkv", source),
    )
    _add_auto_alternatives(factory)

    async def offline(_torrent_id: str) -> TorrentPayload:
        raise SiteAdapterError("SITE_TIMEOUT", "synthetic failure", retryable=True)

    adapter = service._site_provider.enabled_adapters()[0].adapter
    monkeypatch.setattr(adapter, "fetch_torrent", offline)

    report = asyncio.run(service.verify_next_batch("execution-1", limit=10))

    assert report.processed_count == 1
    assert report.error_count == 1
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        second = session.get(UnpackMatchCandidate, "candidate-2")
        assert item is not None and second is not None
        assert item.selected_candidate_id == "candidate-1"
        assert item.last_error_code == "SITE_TIMEOUT"
        assert second.verification_status == UnpackCandidateVerificationStatus.NOT_CHECKED.value


def test_existing_auxiliary_operation_disables_automatic_candidate_switch(tmp_path: Path) -> None:
    source = b"abcdefgh"
    service, factory, _path = _service(
        tmp_path,
        source_content=source,
        torrent={"torrent-1": _v1_single("movie.mkv", source, expected=b"abcdWXYZ")},
    )
    _add_auto_alternatives(factory)
    with factory() as session:
        session.add(
            UnpackExternalOperationJournal(
                id="synthetic-aux-operation",
                item_id="item-1",
                idempotency_key="b" * 64,
                operation_type="UNPACK_AUX_TORRENT_ADD",
                target={"downloader_id": "synthetic-only"},
                intent={"reason": "previous-auxiliary-operation"},
                status="APPLIED",
                created_at=utc_now(),
                updated_at=utc_now(),
            )
        )
        session.commit()

    report = asyncio.run(service.verify_next_batch("execution-1", limit=5))

    assert report.processed_count == 1
    assert report.content_mismatch_count == 1
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        second = session.get(UnpackMatchCandidate, "candidate-2")
        assert item is not None and second is not None
        assert item.selected_candidate_id == "candidate-1"
        assert second.verification_status == UnpackCandidateVerificationStatus.NOT_CHECKED.value


def test_human_review_during_fallback_prevents_stale_verification_from_overwriting(
    tmp_path: Path,
) -> None:
    source = b"abcdefgh"
    service, factory, _path = _service(
        tmp_path,
        source_content=source,
        torrent=_v1_single("movie.mkv", source, expected=b"abcdWXYZ"),
    )
    _add_auto_alternatives(factory)
    claim = service._claim_next_item("execution-1")
    assert claim is not None
    result = UnpackItemActionService(factory).review(
        "item-1",
        decision=UnpackReviewDecision.APPROVE,
        candidate_id="candidate-2",
        generation=claim.generation,
        expected_item_version=claim.item_version,
        idempotency_key="manual-choice-during-auto-candidate-check",
    )
    assert result.item_status is UnpackItemStatus.MATCHED_MANUAL

    asyncio.run(service._verify_claim(claim))

    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        second = session.get(UnpackMatchCandidate, "candidate-2")
        assert item is not None and second is not None
        assert item.selected_candidate_id == second.id
        assert item.match_origin == "MANUAL"
        assert item.status == UnpackItemStatus.MATCHED_MANUAL.value
        assert second.verification_status == UnpackCandidateVerificationStatus.NOT_CHECKED.value


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


def test_real_multifile_torrent_maps_existing_same_directory_movie_siblings(
    tmp_path: Path,
) -> None:
    """One discovered video may belong to a torrent with two more real videos."""
    content = b"abcdefgh"
    second = b"ijklmnop"
    third = b"qrstuvwx"
    torrent = _v1_multi(
        (("movie.mkv", content), ("part-two.mkv", second), ("part-three.mkv", third))
    )
    service, factory, source = _service(
        tmp_path,
        source_content=content,
        torrent=torrent,
    )
    (source.parent / "part-two.mkv").write_bytes(second)
    (source.parent / "part-three.mkv").write_bytes(third)

    report = asyncio.run(service.verify_next_batch("execution-1"))

    assert report.content_verified_count == 1
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        candidate = session.get(UnpackMatchCandidate, "candidate-1")
        assert item is not None and candidate is not None
        assert item.status == UnpackItemStatus.CONTENT_VERIFIED.value
        assert item.content_verification_level == VerificationLevel.FULL_VERIFIED.value
        assert candidate.verification_status == UnpackCandidateVerificationStatus.VERIFIED.value
        mappings = candidate.evidence["content_verification"]["mappings"]
        assert len(mappings) == 3
        assert all(mapping["state"] == "MAPPED" for mapping in mappings)
        assert {Path(mapping["source_path"]).name for mapping in mappings} == {
            "movie.mkv",
            "part-two.mkv",
            "part-three.mkv",
        }
    assert source.read_bytes() == content
    assert (source.parent / "part-two.mkv").read_bytes() == second


def test_multifile_torrent_never_follows_symlinked_movie_sibling(tmp_path: Path) -> None:
    content = b"abcdefgh"
    second = b"ijklmnop"
    torrent = _v1_multi((("movie.mkv", content), ("part-two.mkv", second)))
    service, factory, source = _service(
        tmp_path,
        source_content=content,
        torrent=torrent,
    )
    external = tmp_path / "outside.mkv"
    external.write_bytes(second)
    (source.parent / "part-two.mkv").symlink_to(external)

    report = asyncio.run(service.verify_next_batch("execution-1"))

    assert report.content_mismatch_count == 1
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.last_error_code == "UNPACK_CONTENT_REQUIRED_FILE_MISSING"
    assert external.read_bytes() == second


def test_multifile_sibling_scan_is_bounded_and_fail_closed(tmp_path: Path) -> None:
    content = b"abcdefgh"
    torrent = _v1_single("movie.mkv", content)
    service, factory, source = _service(
        tmp_path,
        source_content=content,
        torrent=torrent,
    )
    for index in range(2048):
        (source.parent / f"unrelated-{index}.txt").touch()

    report = asyncio.run(service.verify_next_batch("execution-1"))

    assert report.error_count == 1
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.last_error_code == "TORRENT_LIMIT_EXCEEDED"
    assert source.read_bytes() == content


def test_existing_auxiliary_sibling_avoids_unnecessary_fetch(tmp_path: Path) -> None:
    content = b"abcdefgh"
    auxiliary = b"nfo"
    torrent = _v1_multi((("movie.mkv", content), ("movie.nfo", auxiliary)))
    service, factory, source = _service(tmp_path, source_content=content, torrent=torrent)
    (source.parent / "movie.nfo").write_bytes(auxiliary)

    result = asyncio.run(service.verify_next_batch("execution-1"))

    assert result.content_verified_count == 1
    assert result.auxiliary_pending_count == 0
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == UnpackItemStatus.CONTENT_VERIFIED.value
        assert item.auxiliary_state is None


def test_multifile_source_change_during_hashing_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = b"abcdefgh"
    second = b"ijklmnop"
    service, factory, source = _service(
        tmp_path,
        source_content=content,
        torrent=_v1_multi((("movie.mkv", content), ("part-two.mkv", second))),
    )
    sibling = source.parent / "part-two.mkv"
    sibling.write_bytes(second)
    original_verify = verification_module._verify_torrent

    def verify_then_change(*args: object) -> object:
        outcome = original_verify(*args)  # type: ignore[arg-type]
        sibling.write_bytes(b"replaced-content")
        return outcome

    monkeypatch.setattr(verification_module, "_verify_torrent", verify_then_change)

    result = asyncio.run(service.verify_next_batch("execution-1"))

    assert result.review_count == 1
    assert result.content_verified_count == 0
    assert result.execution_status is UnpackExecutionStatus.REVIEW_REQUIRED
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        candidate = session.get(UnpackMatchCandidate, "candidate-1")
        assert item is not None and candidate is not None
        assert item.last_error_code == "SOURCE_SNAPSHOT_CHANGED"
        assert candidate.verification_status == UnpackCandidateVerificationStatus.UNAVAILABLE.value


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
