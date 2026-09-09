from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select

from backend.app.application.analysis import AnalysisPolicy, AnalysisService
from backend.app.application.errors import ApplicationError
from backend.app.application.sites import EnabledSiteAdapter
from backend.app.domain.site_adapter import SiteConnectionResult, TorrentDetails, TorrentPayload
from backend.app.domain.site_search import (
    SearchPage,
    SearchQuery,
    SiteSearchCapabilities,
    normalize_candidate_meta,
)
from backend.app.domain.task_units import SourceTaskFile, identify_task_units
from backend.app.domain.verification import VerificationLevel
from backend.app.infrastructure.adapters.site_errors import SiteAdapterError
from backend.app.infrastructure.persistence.base import Base
from backend.app.infrastructure.persistence.database import (
    create_session_factory,
    create_sqlite_engine,
)
from backend.app.infrastructure.persistence.models import PreflightSnapshotRecord
from backend.app.infrastructure.persistence.repositories import TaskCreate, TaskRepository


class _FakeAdapter:
    def __init__(
        self,
        site_id: str,
        torrent_bytes: bytes,
        *,
        min_interval: float = 0.0,
        fail_search: bool = False,
        on_fetch: Callable[[], None] | None = None,
    ) -> None:
        self.site_id = site_id
        self.torrent_bytes = torrent_bytes
        self.min_interval = min_interval
        self.fail_search = fail_search
        self.on_fetch = on_fetch
        self.search_calls: list[SearchQuery] = []

    async def capabilities(self) -> SiteSearchCapabilities:
        return SiteSearchCapabilities(
            supports_pagination=True,
            min_request_interval_seconds=self.min_interval,
        )

    async def test_connection(self) -> SiteConnectionResult:
        return SiteConnectionResult(self.site_id)

    async def search(self, query: SearchQuery) -> SearchPage:
        self.search_calls.append(query)
        if self.fail_search:
            raise SiteAdapterError("SITE_UNAVAILABLE", "synthetic unavailable", retryable=True)
        candidate = normalize_candidate_meta(
            site_id=self.site_id,
            torrent_id="42",
            display_name="Movie.2026",
            total_size=16,
        )
        return SearchPage(self.site_id, query.page, (candidate,), False, 1)

    async def fetch_details(self, torrent_id: str) -> TorrentDetails:
        return TorrentDetails(
            normalize_candidate_meta(
                site_id=self.site_id,
                torrent_id=torrent_id,
                display_name="Movie.2026",
                total_size=16,
            )
        )

    async def fetch_torrent(self, torrent_id: str) -> TorrentPayload:
        if self.on_fetch is not None:
            self.on_fetch()
        return TorrentPayload(self.site_id, torrent_id, self.torrent_bytes, datetime.now(UTC))


class _FakeSiteProvider:
    def __init__(
        self,
        bindings: tuple[EnabledSiteAdapter, ...],
        *,
        final_versions: tuple[tuple[str, int], ...] | None = None,
    ) -> None:
        self.bindings = bindings
        self.final_versions = final_versions

    def enabled_adapters(self) -> tuple[EnabledSiteAdapter, ...]:
        return self.bindings

    def enabled_site_versions(self) -> tuple[tuple[str, int], ...]:
        if self.final_versions is not None:
            return self.final_versions
        return tuple((binding.config_id, binding.config_version) for binding in self.bindings)


@pytest.mark.asyncio
async def test_analysis_builds_stable_multi_site_preflight_and_persists_once(
    tmp_path: Path,
) -> None:
    content = b"0123456789abcdef"
    source_root = tmp_path / "source"
    source_root.mkdir()
    source_file = source_root / "Movie.2026.mkv"
    source_file.write_bytes(content)
    unit = identify_task_units((SourceTaskFile(source_file.name, len(content)),))[0]
    torrent = _v1_torrent(source_file.name.encode(), content, piece_length=4)

    engine = create_sqlite_engine(tmp_path / "analysis.db")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    with factory() as session:
        task, created = TaskRepository(session).create_or_get(
            TaskCreate(
                task_type="PACKAGE_UNPACK",
                source_downloader_id="source",
                source_hash="synthetic-source-hash",
                normalized_unit_key=unit.normalized_unit_key,
                trace_id="00000000-0000-0000-0000-000000000001",
            )
        )
        assert created
        session.commit()
        task_id = task.id

    fast = _FakeAdapter("fast", torrent)
    limited = _FakeAdapter("limited", torrent, min_interval=90.0)
    failing = _FakeAdapter("failing", torrent, fail_search=True)
    provider = _FakeSiteProvider(
        (
            EnabledSiteAdapter("cfg-fast", 1, "fast", fast),
            EnabledSiteAdapter("cfg-limited", 3, "limited", limited),
            EnabledSiteAdapter("cfg-failing", 2, "failing", failing),
        )
    )
    service = AnalysisService(factory, provider, policy=AnalysisPolicy(max_candidates_to_verify=5))

    first = await service.analyze(task_id=task_id, unit=unit, source_root=source_root)
    second = await service.analyze(task_id=task_id, unit=unit, source_root=source_root)

    assert first.snapshot_digest == second.snapshot_digest
    assert len(first.planned_queries) >= 2
    assert len(fast.search_calls) == len(first.planned_queries) * 2
    assert len(limited.search_calls) == 2
    assert [item.site_id for item in first.candidates] == ["fast", "limited"]
    assert all(
        item.verification_level is VerificationLevel.FULL_VERIFIED for item in first.candidates
    )
    assert all(item.selected_for_verification for item in first.candidates)
    failing_evidence = next(item for item in first.sites if item.site_id == "failing")
    assert failing_evidence.error_codes == ("SITE_UNAVAILABLE",) * len(first.planned_queries)

    with factory() as session:
        count = session.scalar(select(func.count()).select_from(PreflightSnapshotRecord))
        stored = session.scalar(select(PreflightSnapshotRecord))
        assert count == 1
        assert stored is not None
        assert stored.snapshot_digest == first.snapshot_digest
        assert stored.payload["snapshot_digest"] == first.snapshot_digest
    engine.dispose()


@pytest.mark.asyncio
async def test_analysis_rejects_site_version_change_before_preflight_write(tmp_path: Path) -> None:
    content = b"0123456789abcdef"
    source_root = tmp_path / "source"
    source_root.mkdir()
    source_file = source_root / "Movie.2026.mkv"
    source_file.write_bytes(content)
    unit = identify_task_units((SourceTaskFile(source_file.name, len(content)),))[0]
    torrent = _v1_torrent(source_file.name.encode(), content, piece_length=4)
    engine = create_sqlite_engine(tmp_path / "analysis-version.db")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    with factory() as session:
        task, _ = TaskRepository(session).create_or_get(
            TaskCreate("PACKAGE_UNPACK", "source", "hash", unit.normalized_unit_key, "trace")
        )
        session.commit()
        task_id = task.id

    adapter = _FakeAdapter("fake", torrent)
    provider = _FakeSiteProvider(
        (EnabledSiteAdapter("cfg", 1, "fake", adapter),),
        final_versions=(("cfg", 2),),
    )
    service = AnalysisService(factory, provider)

    with pytest.raises(ApplicationError) as failure:
        await service.analyze(task_id=task_id, unit=unit, source_root=source_root)
    assert failure.value.code == "ANALYSIS_SITE_CONFIG_CHANGED"
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(PreflightSnapshotRecord)) == 0
    engine.dispose()


@pytest.mark.asyncio
async def test_analysis_rejects_source_change_during_read_only_verification(tmp_path: Path) -> None:
    content = b"0123456789abcdef"
    source_root = tmp_path / "source"
    source_root.mkdir()
    source_file = source_root / "Movie.2026.mkv"
    source_file.write_bytes(content)
    unit = identify_task_units((SourceTaskFile(source_file.name, len(content)),))[0]
    torrent = _v1_torrent(source_file.name.encode(), content, piece_length=4)
    engine = create_sqlite_engine(tmp_path / "analysis-source.db")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    with factory() as session:
        task, _ = TaskRepository(session).create_or_get(
            TaskCreate("PACKAGE_UNPACK", "source", "hash", unit.normalized_unit_key, "trace")
        )
        session.commit()
        task_id = task.id

    changed = False

    def mutate_source_once() -> None:
        nonlocal changed
        if not changed:
            source_file.write_bytes(b"fedcba9876543210")
            changed = True

    adapter = _FakeAdapter("fake", torrent, on_fetch=mutate_source_once)
    provider = _FakeSiteProvider((EnabledSiteAdapter("cfg", 1, "fake", adapter),))
    service = AnalysisService(factory, provider)

    with pytest.raises(ApplicationError) as failure:
        await service.analyze(task_id=task_id, unit=unit, source_root=source_root)
    assert failure.value.code == "ANALYSIS_SOURCE_CHANGED"
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(PreflightSnapshotRecord)) == 0
    engine.dispose()


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
