import asyncio
from typing import cast

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.sites import EnabledSiteAdapter
from backend.app.application.unpack_matching import UnpackMatchCoordinator
from backend.app.domain.media_matching import MediaFileSummary, parse_media_name
from backend.app.domain.site_adapter import SiteAdapter
from backend.app.domain.site_search import (
    CandidateMeta,
    SearchPage,
    SearchQuery,
    SiteSearchCapabilities,
)
from backend.app.domain.unpack import UnpackExecutionStatus, UnpackItemStatus
from backend.app.infrastructure.adapters.site_errors import SiteAdapterError
from backend.app.infrastructure.persistence.base import Base
from backend.app.infrastructure.persistence.models import (
    UnpackDefinition,
    UnpackExecution,
    UnpackExecutionItem,
    UnpackMatchCandidate,
    utc_now,
)


class _FakeSiteAdapter:
    def __init__(
        self,
        candidates: tuple[CandidateMeta, ...] = (),
        *,
        error: SiteAdapterError | None = None,
    ) -> None:
        self._candidates = candidates
        self._error = error
        self.queries: list[SearchQuery] = []

    async def capabilities(self) -> SiteSearchCapabilities:
        return SiteSearchCapabilities(
            supports_imdb_id=True,
            supports_douban_id=True,
        )

    async def search(self, query: SearchQuery) -> SearchPage:
        self.queries.append(query)
        if self._error is not None:
            raise self._error
        return SearchPage(
            site_id="fake-site",
            page=1,
            items=self._candidates,
            has_more=False,
        )


class _FakeSiteProvider:
    def __init__(self, adapter: _FakeSiteAdapter | None) -> None:
        self._adapter = adapter

    def enabled_adapters(self) -> tuple[EnabledSiteAdapter, ...]:
        if self._adapter is None:
            return ()
        return (
            EnabledSiteAdapter(
                config_id="site-config-1",
                config_version=7,
                site_id="fake-site",
                adapter=cast(SiteAdapter, self._adapter),
            ),
        )


def _factory(
    *,
    threshold: int,
    filename: str = "Movie.2024.1080p.WEB-DL.x265.tt1234567.mkv",
    size: int = 1000,
) -> sessionmaker[Session]:
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
                name="匹配测试",
                trigger_kind="MANUAL",
                status="PENDING_EXECUTION",
                source_kind="DIRECTORY",
                execution_scope_kind="ALL_MATCHING_MEDIA",
                source_config={"directory_path": "/data"},
                file_filter={"extensions": [".mkv"]},
                site_ids=["site-config-1"],
                output_config={},
                retry_enabled=True,
                max_retries=3,
                auto_match_threshold_bps=threshold,
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
                status=UnpackExecutionStatus.MATCHING.value,
                config_snapshot={
                    "site_ids": ["site-config-1"],
                    "matching": {"auto_match_threshold_bps": threshold},
                },
                discovery_complete=True,
                total_count=1,
                matched_auto_count=0,
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
                    "path": f"/data/{filename}",
                    "relative_path": filename,
                    "size": size,
                    "file_type": "regular",
                },
                media_identity={},
                status=UnpackItemStatus.MATCH_PENDING.value,
                candidate_generation=0,
                retry_count=0,
                version=1,
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()
    return factory


def _candidate(
    name: str,
    *,
    torrent_id: str = "torrent-1",
    size: int = 1000,
    imdb_id: str = "tt1234567",
) -> CandidateMeta:
    descriptor = parse_media_name(
        f"{name}.{imdb_id}",
        total_size=size,
        files=(MediaFileSummary(f"{name}.mkv", size),),
    )
    return CandidateMeta(
        site_id="fake-site",
        torrent_id=torrent_id,
        display_name=f"{name}.{imdb_id}",
        descriptor=descriptor,
        total_size=size,
        seeders=12,
        file_summary=descriptor.files,
    )


def test_match_threshold_can_auto_select_non_exact_candidate() -> None:
    factory = _factory(threshold=7000)
    adapter = _FakeSiteAdapter((_candidate("Movie.Extended.2024.1080p.WEB-DL.x265"),))
    service = UnpackMatchCoordinator(factory, _FakeSiteProvider(adapter))

    report = asyncio.run(service.match_next_batch("execution-1", limit=10))

    assert report.processed_count == 1
    assert report.matched_auto_count == 1
    assert report.execution_status is UnpackExecutionStatus.CONTENT_VERIFYING
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == UnpackItemStatus.MATCHED_AUTO.value
        assert item.selected_candidate_id is not None
        assert item.candidate_generation == 1
        candidate = session.scalar(select(UnpackMatchCandidate))
        assert candidate is not None
        assert candidate.is_exact_match is False
        assert candidate.score_bps >= 7000


def test_high_threshold_sends_candidate_to_review_without_persisting_approval() -> None:
    factory = _factory(threshold=10_000)
    adapter = _FakeSiteAdapter((_candidate("Movie.Extended.2024.1080p.WEB-DL.x265"),))
    service = UnpackMatchCoordinator(factory, _FakeSiteProvider(adapter))

    report = asyncio.run(service.match_next_batch("execution-1"))

    assert report.review_count == 1
    assert report.execution_status is UnpackExecutionStatus.REVIEW_REQUIRED
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == UnpackItemStatus.REVIEW_REQUIRED.value
        assert item.selected_candidate_id is None
        assert session.scalar(select(UnpackMatchCandidate)) is not None


def test_hard_conflict_candidate_is_not_auto_selected() -> None:
    factory = _factory(threshold=0)
    adapter = _FakeSiteAdapter(
        (
            _candidate(
                "Movie.2023.1080p.WEB-DL.x265",
                imdb_id="tt7654321",
            ),
        )
    )
    service = UnpackMatchCoordinator(factory, _FakeSiteProvider(adapter))

    report = asyncio.run(service.match_next_batch("execution-1"))

    assert report.no_match_count == 1
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == UnpackItemStatus.NO_MATCH.value
        candidate = session.scalar(select(UnpackMatchCandidate))
        assert candidate is not None
        assert candidate.evidence["hard_conflicts"]


def test_retryable_site_failure_becomes_match_timeout() -> None:
    factory = _factory(threshold=10_000)
    adapter = _FakeSiteAdapter(
        error=SiteAdapterError(
            "SITE_TIMEOUT",
            "synthetic timeout",
            retryable=True,
        )
    )
    service = UnpackMatchCoordinator(factory, _FakeSiteProvider(adapter))

    report = asyncio.run(service.match_next_batch("execution-1"))

    assert report.timeout_count == 1
    assert report.execution_status is UnpackExecutionStatus.COMPLETED_WITH_ERRORS
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == UnpackItemStatus.MATCH_TIMEOUT.value
        assert item.last_error_code == "UNPACK_MATCH_TIMEOUT"


def test_selected_site_not_enabled_becomes_match_error() -> None:
    factory = _factory(threshold=10_000)
    service = UnpackMatchCoordinator(factory, _FakeSiteProvider(None))

    report = asyncio.run(service.match_next_batch("execution-1"))

    assert report.error_count == 1
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == UnpackItemStatus.MATCH_ERROR.value
        assert item.last_error_code == "UNPACK_MATCH_SITE_UNAVAILABLE"


def test_custom_media_suffix_is_not_rejected_by_legacy_task_unit_extensions() -> None:
    factory = _factory(
        threshold=0,
        filename="Movie.2024.1080p.WEB-DL.x265.tt1234567.customvideo",
    )
    adapter = _FakeSiteAdapter((_candidate("Movie.2024.1080p.WEB-DL.x265"),))
    service = UnpackMatchCoordinator(factory, _FakeSiteProvider(adapter))

    report = asyncio.run(service.match_next_batch("execution-1"))

    assert report.matched_auto_count == 1
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.media_identity["raw_name"].startswith("Movie.2024")


def test_unparseable_media_identity_becomes_terminal_match_error_for_this_generation() -> None:
    factory = _factory(threshold=10_000, filename="---.mkv")
    service = UnpackMatchCoordinator(factory, _FakeSiteProvider(None))

    report = asyncio.run(service.match_next_batch("execution-1"))

    assert report.processed_count == 0
    assert report.error_count == 1
    assert report.execution_status is UnpackExecutionStatus.COMPLETED_WITH_ERRORS
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == UnpackItemStatus.MATCH_ERROR.value
        assert item.last_error_code == "UNPACK_MEDIA_IDENTITY_INVALID"


def test_season_directory_context_recovers_bare_episode_identity() -> None:
    factory = _factory(
        threshold=0,
        filename="Show.2024/Season 01/01.customvideo",
    )
    adapter = _FakeSiteAdapter(
        (
            _candidate(
                "Show.2024.S01E01.1080p.WEB-DL.x265",
                imdb_id="tt1234567",
            ),
        )
    )
    service = UnpackMatchCoordinator(factory, _FakeSiteProvider(adapter))

    report = asyncio.run(service.match_next_batch("execution-1"))

    assert report.matched_auto_count == 1
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.media_identity["title_tokens"] == ["show"]
        assert item.media_identity["year"] == 2024
        assert item.media_identity["episode"] == {
            "kind": "SEASON_EPISODE",
            "season": 1,
            "start": 1,
            "end": 1,
        }
