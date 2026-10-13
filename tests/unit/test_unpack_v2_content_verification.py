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
                config_snapshot={"site_ids": ["site-config-1"]},
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
        execution.config_snapshot = {
            **execution.config_snapshot,
            "matching": {"auto_match_threshold_bps": threshold},
        }
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


@pytest.mark.parametrize(
    ("case", "expected_code"),
    [
        ("unselected_site", "UNPACK_VERIFY_CANDIDATE_SITE_NOT_SCANNED"),
        ("reference_site", "UNPACK_VERIFY_CANDIDATE_REF_INVALID"),
        ("reference_torrent", "UNPACK_VERIFY_CANDIDATE_REF_INVALID"),
    ],
)
def test_selected_candidate_identity_is_frozen_before_torrent_fetch(
    tmp_path: Path, case: str, expected_code: str
) -> None:
    """A selected row must not authorize a different site or torrent fetch."""
    source = b"abcdefgh"
    service, factory, _path = _service(
        tmp_path, source_content=source, torrent=_v1_single("movie.mkv", source)
    )
    with factory() as session:
        candidate = session.get(UnpackMatchCandidate, "candidate-1")
        assert candidate is not None
        if case == "unselected_site":
            candidate.site_id = "site-config-unselected"
        else:
            raw_ref = dict(candidate.raw_ref)
            if case == "reference_site":
                raw_ref["site_config_id"] = "site-config-unselected"
            else:
                raw_ref["torrent_id"] = "another-torrent"
            candidate.raw_ref = raw_ref
        session.commit()

    report = asyncio.run(service.verify_next_batch("execution-1", limit=1))

    assert report.processed_count == 0
    provider = cast(_FakeSiteProvider, service._site_provider)
    assert provider._adapter.fetched_ids == []
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        candidate = session.get(UnpackMatchCandidate, "candidate-1")
        assert item is not None and candidate is not None
        assert item.status == UnpackItemStatus.REVIEW_REQUIRED.value
        assert item.last_error_code == expected_code
        assert candidate.verification_status == UnpackCandidateVerificationStatus.NOT_CHECKED.value


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


@pytest.mark.parametrize(
    ("change_verified_sibling", "late_target_drift"),
    ((False, None), (True, None), (False, "remove"), (False, "replace"), (False, "mutate")),
)
def test_multifile_verified_content_plan_and_copy_materialization_bridge(
    tmp_path: Path, change_verified_sibling: bool, late_target_drift: str | None
) -> None:
    """Production verifier -> planner -> filesystem gateway on one isolated movie pack.

    Exact media siblings are found by the content stage, then only their
    persisted source snapshots and frozen metainfo may authorize materialize.
    No client writes, trackers, PT credentials or production paths are used.
    """
    from backend.app.application.downloaders import QbittorrentWriteBinding
    from backend.app.application.unpack_execution_plans import UnpackExecutionPlanService
    from backend.app.application.unpack_materialization import UnpackMaterializationService
    from backend.app.domain.downloader import PathMappingRule
    from backend.app.domain.unpack_execution_plan import unpack_execution_plan_from_payload
    from backend.app.infrastructure.adapters.downloaders import QbittorrentWriteAdapter

    movie_data = {
        "movie.mkv": b"abcdefgh",
        "part-two.mkv": b"ijklmnop",
        "part-three.mkv": b"qrstuvwx",
    }
    original = _v1_multi(tuple(movie_data.items()))
    verifier, factory, source = _service(
        tmp_path, source_content=movie_data["movie.mkv"], torrent=original
    )
    data_root = source.parent
    output = data_root / "isolated-seeding"
    output.mkdir()
    for name in ("part-two.mkv", "part-three.mkv"):
        (data_root / name).write_bytes(movie_data[name])
    with factory() as session:
        execution = session.get(UnpackExecution, "execution-1")
        item = session.get(UnpackExecutionItem, "item-1")
        assert execution is not None and item is not None
        # Drive the real matcher: no hand-injected selected candidate is
        # allowed to authorize later operations in this integration case.
        session.execute(delete(UnpackMatchCandidate))
        item.status = UnpackItemStatus.MATCH_PENDING.value
        item.selected_candidate_id = None
        item.match_origin = None
        item.candidate_generation = 0
        execution.status = UnpackExecutionStatus.MATCHING.value
        execution.config_snapshot = {
            "site_ids": ["site-config-1"],
            "matching": {"auto_match_threshold_bps": 0},
            "output_config": {
                "output_directory": output.as_posix(),
                "storage_mode": "COPY",
                "conflict_policy": "VERIFY_REUSE_OR_STOP",
                "target_downloader_id": "fake-qb",
            },
        }
        session.commit()

    class _SearchAndFetchAdapter(_FakeSiteAdapter):
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
                        total_size=len(movie_data["movie.mkv"]),
                        seeders=10,
                    ),
                ),
                False,
            )

    provider = _FakeSiteProvider(original)
    provider._adapter = _SearchAndFetchAdapter(original)
    matched = asyncio.run(UnpackMatchCoordinator(factory, provider).match_next_batch("execution-1"))
    assert matched.matched_auto_count == 1
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == UnpackItemStatus.MATCHED_AUTO.value
        assert item.selected_candidate_id is not None
        candidate = session.get(UnpackMatchCandidate, item.selected_candidate_id)
        assert candidate is not None
        assert candidate.generation == item.candidate_generation == 1
        assert candidate.evidence["hard_conflicts"] == []
        assert candidate.raw_ref["site_config_version"] == 7

    verifier = UnpackContentVerificationService(
        factory, provider, path_scope=AuthorizedPathScope.legacy_only(legacy_data_root=data_root)
    )
    verified = asyncio.run(verifier.verify_next_batch("execution-1"))
    assert verified.content_verified_count == 1
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None and item.selected_candidate_id is not None
        candidate = session.get(UnpackMatchCandidate, item.selected_candidate_id)
        assert candidate is not None
        mappings = candidate.evidence["content_verification"]["mappings"]
        assert len(mappings) == 3
        assert {Path(x["source_path"]).name for x in mappings} == set(movie_data)

    if change_verified_sibling:
        # A source in the same torrent changed *after* a full Piece proof.
        # Planning must not freeze stale evidence or create output.
        (data_root / "part-two.mkv").write_bytes(b"tampered!")
    binding = QbittorrentWriteBinding(
        downloader_id="fake-qb",
        downloader_version=3,
        binding_digest="b" * 64,
        path_mappings=(PathMappingRule("/downloads", data_root.as_posix()),),
        capabilities={"supports_skip_checking": False, "api_version": "2.15.1"},
        adapter=cast(QbittorrentWriteAdapter, object()),
        data_root=data_root,
    )

    class _BindingProvider:
        def write_binding(self, downloader_id: str) -> QbittorrentWriteBinding:
            assert downloader_id == "fake-qb"
            return binding

    scope = AuthorizedPathScope.legacy_only(legacy_data_root=data_root)
    planner = UnpackExecutionPlanService(
        factory,
        provider,
        _BindingProvider(),
        path_scope=scope,
    )
    planned = asyncio.run(planner.plan_next_batch("execution-1"))
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        if change_verified_sibling:
            # The planner persists a BLOCKED, non-executable plan rather
            # than dropping it. It must never authorize materialization.
            assert planned.ready_count == 0
            assert planned.blocked_count == 1
            assert item.status == UnpackItemStatus.PLAN_PENDING.value
            plan = unpack_execution_plan_from_payload(item.execution_plan)
            assert not plan.ready and plan.blocked_reasons
            assert item.last_error_code == "UNPACK_EXECUTION_PLAN_BLOCKED"
        else:
            assert planned.ready_count == 1
            assert item.status == UnpackItemStatus.PLAN_PENDING.value
            plan = unpack_execution_plan_from_payload(item.execution_plan)
            assert plan.ready and plan.client_check_required
        if not change_verified_sibling:
            assert len(plan.actions) == 3
            assert plan.target_remote_save_path == "/downloads/isolated-seeding"

    materializer = UnpackMaterializationService(
        factory,
        data_root=data_root,
        path_scope=scope,
    )
    result = materializer.materialize_next_batch("execution-1")
    if change_verified_sibling:
        assert result.error_count == 1
        assert not list(output.iterdir())
        with factory() as session:
            item = session.get(UnpackExecutionItem, "item-1")
            assert item is not None
            assert item.status == UnpackItemStatus.EXECUTION_ERROR.value
            assert not session.scalars(select(UnpackExternalOperationJournal)).all()
        return
    assert result.materialized_count == 1
    assert result.error_count == 0
    for name, expected in movie_data.items():
        target = output / "Pack" / name
        origin = data_root / name
        assert target.read_bytes() == expected
        assert target.stat().st_ino != origin.stat().st_ino
        assert origin.read_bytes() == expected
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.execution_state is not None
        assert item.execution_state["stage"] == "FILES_MATERIALIZED"
        journals = session.scalars(select(UnpackExternalOperationJournal)).all()
        assert len(journals) == 4
        assert all(row.status == "APPLIED" for row in journals)
    again = materializer.materialize_next_batch("execution-1")
    assert again.processed_count == 0
    assert again.materialized_count == 1
    with factory() as session:
        assert len(session.scalars(select(UnpackExternalOperationJournal)).all()) == 4

    # Final stage: consume the *same* database item and freshly frozen plan
    # with the real seeding service and a journal-aware synthetic QB adapter.
    # Its three calls must follow materialization, never before the source
    # snapshot and all target filesystem journals have been proven.
    from dataclasses import replace

    from backend.app.application.unpack_seeding import UnpackSeedingService
    from tests.unit.test_unpack_v2_seeding import _FakeQb

    client = _FakeQb(factory, original)
    binding = replace(binding, adapter=cast(QbittorrentWriteAdapter, client))
    seeding = UnpackSeedingService(factory, provider, _BindingProvider(), path_scope=scope)
    if late_target_drift is not None:
        copied = output / "Pack" / "part-two.mkv"
        if late_target_drift == "remove":
            copied.unlink()
        elif late_target_drift == "replace":
            copied.unlink()
            copied.write_bytes(movie_data["part-two.mkv"])
        else:
            copied.write_bytes(b"12345678")  # Same inode and length; changed content/mtime.
        failure = asyncio.run(seeding.advance_next_batch("execution-1"))
        assert failure.error_count == 1
        assert client.calls == []
        with factory() as session:
            item = session.get(UnpackExecutionItem, "item-1")
            assert item is not None
            assert item.status == UnpackItemStatus.EXECUTION_ERROR.value
            assert item.last_error_code == "UNPACK_EXEC_MATERIALIZATION_TARGET_DRIFT"
            records = session.scalars(select(UnpackExternalOperationJournal)).all()
            assert len(records) == 4
        return

    for _ in range(7):
        seeding_result = asyncio.run(seeding.advance_next_batch("execution-1"))
        if seeding_result.completed_count == 1:
            break
    else:
        pytest.fail("Full stage bridge did not complete final seeding")
    assert client.calls == ["add", "verify", "start"]
    assert client.state is not None and client.state.seeding
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None and item.status == UnpackItemStatus.COMPLETED.value
        records = session.scalars(select(UnpackExternalOperationJournal)).all()
        assert len(records) == 7
        assert all(row.status == "APPLIED" for row in records)
    assert asyncio.run(seeding.advance_next_batch("execution-1")).processed_count == 0
    assert client.calls == ["add", "verify", "start"]
