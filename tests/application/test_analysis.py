from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

import httpx2
import pytest
from sqlalchemy import func, select

from backend.app.application.analysis import AnalysisPolicy, AnalysisService
from backend.app.application.errors import ApplicationError
from backend.app.application.sites import EnabledSiteAdapter
from backend.app.domain.site_adapter import (
    SiteConnectionResult,
    SiteUserProfile,
    TorrentDetails,
    TorrentPayload,
)
from backend.app.domain.site_config import SiteCredentialKind, SiteKind, site_profile
from backend.app.domain.site_search import (
    SearchPage,
    SearchQuery,
    SiteSearchCapabilities,
    normalize_candidate_meta,
)
from backend.app.domain.task_units import SourceTaskFile, identify_task_units
from backend.app.domain.verification import VerificationLevel
from backend.app.infrastructure.adapters.rousi_pro import RousiProCandidateAdapter
from backend.app.infrastructure.adapters.site_errors import SiteAdapterError
from backend.app.infrastructure.adapters.sites import SiteAdapterFactory
from backend.app.infrastructure.persistence.base import Base
from backend.app.infrastructure.persistence.database import (
    create_session_factory,
    create_sqlite_engine,
)
from backend.app.infrastructure.persistence.models import (
    PreflightSnapshotRecord,
    TaskCandidateRecord,
)
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

    async def fetch_user_profile(self) -> SiteUserProfile:
        return SiteUserProfile(self.site_id, uid="1", username="synthetic")

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


class _CompleteSearchAdapter(_FakeAdapter):
    def __init__(self, site_id: str, torrent_bytes: bytes) -> None:
        super().__init__(site_id, torrent_bytes, min_interval=90.0)
        self.fetch_torrent_calls: list[str] = []

    async def capabilities(self) -> SiteSearchCapabilities:
        return SiteSearchCapabilities(
            supports_pagination=True,
            min_request_interval_seconds=90.0,
            search_results_are_complete=True,
            max_verification_candidates=1,
        )

    async def search(self, query: SearchQuery) -> SearchPage:
        self.search_calls.append(query)
        items = tuple(
            normalize_candidate_meta(
                site_id=self.site_id,
                torrent_id=str(index),
                display_name="Movie.2026",
                total_size=16,
            )
            for index in range(1, 4)
        )
        return SearchPage(self.site_id, query.page, items, False, len(items))

    async def fetch_details(self, torrent_id: str) -> TorrentDetails:
        raise AssertionError(f"完整搜索结果不应重复请求详情: {torrent_id}")

    async def fetch_torrent(self, torrent_id: str) -> TorrentPayload:
        self.fetch_torrent_calls.append(torrent_id)
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
        assert session.scalar(select(func.count()).select_from(TaskCandidateRecord)) == 2
    engine.dispose()


@pytest.mark.asyncio
async def test_analysis_uses_site_verification_budget_and_skips_redundant_details(
    tmp_path: Path,
) -> None:
    content = b"0123456789abcdef"
    source_root = tmp_path / "source"
    source_root.mkdir()
    source_file = source_root / "Movie.2026.mkv"
    source_file.write_bytes(content)
    unit = identify_task_units((SourceTaskFile(source_file.name, len(content)),))[0]
    torrent = _v1_torrent(source_file.name.encode(), content, piece_length=4)

    engine = create_sqlite_engine(tmp_path / "analysis-complete-search.db")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    with factory() as session:
        task, _ = TaskRepository(session).create_or_get(
            TaskCreate(
                "PACKAGE_UNPACK",
                "source",
                "complete-search",
                unit.normalized_unit_key,
                "trace",
            )
        )
        session.commit()
        task_id = task.id

    adapter = _CompleteSearchAdapter("complete", torrent)
    provider = _FakeSiteProvider((EnabledSiteAdapter("cfg-complete", 1, "complete", adapter),))
    service = AnalysisService(factory, provider, policy=AnalysisPolicy(max_candidates_to_verify=5))

    snapshot = await service.analyze(task_id=task_id, unit=unit, source_root=source_root)

    assert len(snapshot.candidates) == 3
    selected = [item for item in snapshot.candidates if item.selected_for_verification]
    assert len(selected) == 1
    assert selected[0].verification_level is VerificationLevel.FULL_VERIFIED
    assert len(adapter.fetch_torrent_calls) == 1
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind",
    (
        SiteKind.HDHOME,
        SiteKind.KEEPFRDS,
        SiteKind.UBITS,
        SiteKind.HDFANS,
        SiteKind.BTSCHOOL,
        SiteKind.PTTIME,
        SiteKind.LINGYIN_CLUB,
    ),
)
async def test_candidate_nexus_real_adapter_reaches_full_verified_preflight_without_client(
    tmp_path: Path, kind: SiteKind
) -> None:
    """Use actual candidate adapter + AnalysisService, but synthetic HTTP and source."""
    content = b"0123456789abcdef"
    source_root = tmp_path / "source"
    source_root.mkdir()
    source_file = source_root / "Movie.2026.mkv"
    source_file.write_bytes(content)
    unit = identify_task_units((SourceTaskFile(source_file.name, len(content)),))[0]
    torrent = _v1_torrent(source_file.name.encode(), content, piece_length=4)
    origin = site_profile(kind).base_url
    site_id = kind.value.lower()
    seen: list[str] = []
    secret = "synthetic-passkey-for-test-only"
    suffix = f"&passkey={secret}" if kind is SiteKind.PTTIME else ""
    columns = {
        SiteKind.HDHOME: (10, 7, 6, 5, 4),
        SiteKind.UBITS: (10, 7, 6, 5, 4),
        SiteKind.PTTIME: (12, 8, 7, 6, 5),
    }
    count, date_from_end, size_from_end, seeds_from_end, peers_from_end = columns.get(
        kind, (9, 6, 5, 4, 3)
    )
    cells = ["placeholder"] * count
    cells[0] = '<a href="/details.php?id=123" title="Movie.2026">Movie.2026</a>'
    cells[-date_from_end] = "2026-09-09 12:00:00"
    cells[-size_from_end] = "16 B"
    cells[-seeds_from_end] = "3"
    cells[-peers_from_end] = "0"
    search_html = "<table><tr>" + "".join(f"<td>{cell}</td>" for cell in cells) + "</tr></table>"

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.host == httpx2.URL(origin).host
        assert request.headers.get("cookie") == "synthetic-cookie-for-test-only"
        assert request.headers.get("api-token") is None
        seen.append(request.url.path)
        if request.url.path == "/torrents.php":
            return httpx2.Response(200, text=search_html)
        if request.url.path == "/details.php":
            assert request.url.params["id"] == "123"
            return httpx2.Response(
                200,
                text=f'<h1>Movie.2026</h1><a href="/download.php?id=123{suffix}">Get</a>',
            )
        assert request.url.path == "/download.php"
        assert request.url.params["id"] == "123"
        if kind is SiteKind.PTTIME:
            assert request.url.params["passkey"] == secret
        return httpx2.Response(200, content=torrent)

    adapter = SiteAdapterFactory(transport=httpx2.MockTransport(handler)).create(
        kind=kind,
        base_url=origin,
        credential_kind=SiteCredentialKind.COOKIE,
        credential="synthetic-cookie-for-test-only",
    )
    engine = create_sqlite_engine(tmp_path / "candidate-integration.db")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    try:
        with factory() as session:
            task, _ = TaskRepository(session).create_or_get(
                TaskCreate(
                    "PACKAGE_UNPACK",
                    "synthetic-source",
                    "synthetic-unit",
                    unit.normalized_unit_key,
                    "synthetic-trace",
                )
            )
            session.commit()
            task_id = task.id
        provider = _FakeSiteProvider((EnabledSiteAdapter("cfg-candidate", 1, site_id, adapter),))
        snapshot = await AnalysisService(factory, provider).analyze(
            task_id=task_id, unit=unit, source_root=source_root
        )
        assert len(snapshot.candidates) == 1
        evidence = snapshot.candidates[0]
        assert evidence.site_id == site_id
        assert evidence.selected_for_verification
        assert evidence.verification_level is VerificationLevel.FULL_VERIFIED
        assert evidence.error_code is None
        assert evidence.metainfo_digest
        assert seen.count("/torrents.php") == 1
        assert seen.count("/details.php") == 2
        assert seen.count("/download.php") == 1
        assert source_file.read_bytes() == content
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_rousi_factory_candidate_analysis_stops_at_unverified_torrent_details(
    tmp_path: Path,
) -> None:
    """Successful API search does not imply analysis may bypass pending details."""
    content = b"0123456789abcdef"
    source_root = tmp_path / "source"
    source_root.mkdir()
    source_file = source_root / "Movie.2026.mkv"
    source_file.write_bytes(content)
    unit = identify_task_units((SourceTaskFile(source_file.name, len(content)),))[0]
    origin = site_profile(SiteKind.ROUSI_PRO).base_url
    seen: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.url.path)
        assert request.url.host == httpx2.URL(origin).host
        assert request.headers.get("api-token") == "synthetic-api-key"
        assert request.headers.get("cookie") is None
        assert request.url.path == "/api/v1/torrents"
        return httpx2.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "torrents": [
                        {
                            "id": 123,
                            "title": "Movie.2026",
                            "size": 16,
                            "seeders": 3,
                            "leechers": 0,
                            "created_at": "2026-09-09T12:00:00+08:00",
                        }
                    ],
                    "page": 1,
                    "page_size": 100,
                    "total": 1,
                },
            },
        )

    adapter = SiteAdapterFactory(transport=httpx2.MockTransport(handler)).create(
        kind=SiteKind.ROUSI_PRO,
        base_url=origin,
        credential_kind=SiteCredentialKind.API_KEY,
        credential="synthetic-api-key",
    )
    engine = create_sqlite_engine(tmp_path / "rousi-candidate-integration.db")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    try:
        with factory() as session:
            task, _ = TaskRepository(session).create_or_get(
                TaskCreate(
                    "PACKAGE_UNPACK",
                    "synthetic-source",
                    "synthetic-unit",
                    unit.normalized_unit_key,
                    "synthetic-trace",
                )
            )
            session.commit()
            task_id = task.id
        provider = _FakeSiteProvider((EnabledSiteAdapter("cfg-rousi", 1, "rousi_pro", adapter),))
        snapshot = await AnalysisService(factory, provider).analyze(
            task_id=task_id, unit=unit, source_root=source_root
        )
        assert len(snapshot.candidates) == 1
        evidence = snapshot.candidates[0]
        assert evidence.site_id == "rousi_pro"
        assert evidence.selected_for_verification
        assert evidence.error_code == "SITE_ADAPTER_PENDING"
        assert evidence.verification_level is None
        assert evidence.metainfo_digest is None
        assert seen == ["/api/v1/torrents"]
        assert source_file.read_bytes() == content
    finally:
        engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("valid_metainfo", (True, False))
async def test_rousi_isolated_dual_credential_adapter_can_reach_verified_preflight(
    tmp_path: Path, valid_metainfo: bool
) -> None:
    """Real adapter+analysis logic, synthetic HTTP only, never a persisted site."""
    content = b"0123456789abcdef"
    source_root = tmp_path / "source"
    source_root.mkdir()
    source_file = source_root / "Movie.2026.mkv"
    source_file.write_bytes(content)
    unit = identify_task_units((SourceTaskFile(source_file.name, len(content)),))[0]
    torrent = _v1_torrent(source_file.name.encode(), content, piece_length=4)
    origin = site_profile(SiteKind.ROUSI_PRO).base_url
    requests: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.host == httpx2.URL(origin).host
        requests.append(request.url.path)
        if request.url.path == "/api/v1/torrents":
            assert request.headers.get("api-token") == "synthetic-api-key"
            assert request.headers.get("cookie") is None
            return httpx2.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "torrents": [
                            {
                                "id": 123,
                                "title": "Movie.2026",
                                "size": 16,
                                "seeders": 3,
                                "leechers": 0,
                            },
                            {
                                "id": 124,
                                "title": "Movie.2026.Extra",
                                "size": 16,
                                "seeders": 2,
                                "leechers": 0,
                            },
                        ],
                        "page": 1,
                        "page_size": 100,
                        "total": 2,
                    },
                },
            )
        assert request.url.path == "/api/v1/torrents/123/download"
        assert request.headers.get("cookie") == "synthetic-cookie-for-test-only"
        assert request.headers.get("api-token") is None
        return httpx2.Response(
            200,
            content=torrent if valid_metainfo else b"d3:foo3:bare",
            headers={"content-type": "application/x-bittorrent"},
        )

    adapter = RousiProCandidateAdapter(
        "synthetic-api-key",
        download_cookie="synthetic-cookie-for-test-only",
        transport=httpx2.MockTransport(handler),
    )
    capabilities = await adapter.capabilities()
    assert capabilities.search_results_are_complete
    assert capabilities.max_verification_candidates == 1
    engine = create_sqlite_engine(tmp_path / "rousi-dual-credentials.db")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    try:
        with factory() as session:
            task, _ = TaskRepository(session).create_or_get(
                TaskCreate(
                    "PACKAGE_UNPACK",
                    "synthetic-source",
                    "synthetic-unit",
                    unit.normalized_unit_key,
                    "synthetic-trace",
                )
            )
            session.commit()
            task_id = task.id
        provider = _FakeSiteProvider((EnabledSiteAdapter("cfg-rousi", 1, "rousi_pro", adapter),))
        snapshot = await AnalysisService(factory, provider).analyze(
            task_id=task_id, unit=unit, source_root=source_root
        )
        assert len(snapshot.candidates) == 2
        selected = [item for item in snapshot.candidates if item.selected_for_verification]
        assert len(selected) == 1
        evidence = selected[0]
        assert evidence.verification_level is (
            VerificationLevel.FULL_VERIFIED if valid_metainfo else None
        )
        assert evidence.error_code is (None if valid_metainfo else "SITE_INVALID_RESPONSE")
        assert bool(evidence.metainfo_digest) is valid_metainfo
        assert requests == [
            "/api/v1/torrents",
            "/api/v1/torrents/123/download",
        ]
        assert source_file.read_bytes() == content
    finally:
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
