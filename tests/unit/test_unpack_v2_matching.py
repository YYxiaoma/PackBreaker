import asyncio
import logging
from typing import cast

import httpx2
import pytest
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
from backend.app.infrastructure.adapters.rousi_pro import RousiProCandidateAdapter
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
    retry_enabled: bool = False,
    max_retries: int = 3,
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
                retry_enabled=retry_enabled,
                max_retries=max_retries,
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


def test_rate_limited_site_uses_broad_title_search_instead_of_false_no_match() -> None:
    factory = _factory(
        threshold=10_000,
        filename="Movie.2024.1080p.WEB-DL.x265.mkv",
    )

    class RateLimitedSite(_FakeSiteAdapter):
        async def capabilities(self) -> SiteSearchCapabilities:
            return SiteSearchCapabilities(min_request_interval_seconds=0.001)

        async def search(self, query: SearchQuery) -> SearchPage:
            self.queries.append(query)
            # A real site can return no results for a long "title year" query
            # while succeeding for the bare title.
            return SearchPage(
                site_id="fake-site",
                page=1,
                items=(_candidate("Movie.2024.1080p.WEB-DL.x265"),)
                if len(query.keywords) == 1
                else (),
                has_more=False,
            )

    adapter = RateLimitedSite()
    report = asyncio.run(
        UnpackMatchCoordinator(factory, _FakeSiteProvider(adapter)).match_next_batch("execution-1")
    )
    assert adapter.queries
    assert len(adapter.queries[0].keywords) == 1
    assert report.review_count + report.matched_auto_count == 1
    assert report.no_match_count == 0


@pytest.mark.parametrize("english_size", (1600, 1000))
def test_rate_limited_bilingual_fallback_checks_size_before_stopping(english_size: int) -> None:
    filename = "Hail.the.Judge.1994.1080p.BluRay.mkv"
    factory = _factory(threshold=0, filename=filename, size=1000)
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        snapshot = dict(item.source_snapshot)
        snapshot["path"] = f"/data/九品芝麻官.Hail.the.Judge.1994.1080p.BluRay/{filename}"
        item.source_snapshot = snapshot
        session.commit()

    class BilingualSearch(_FakeSiteAdapter):
        async def capabilities(self) -> SiteSearchCapabilities:
            return SiteSearchCapabilities(min_request_interval_seconds=0.001)

        async def search(self, query: SearchQuery) -> SearchPage:
            self.queries.append(query)
            items: tuple[CandidateMeta, ...]
            if query.query_text == "hail the judge":
                items = (
                    _candidate(
                        "Hail.the.Judge.1994.1080p.BluRay", torrent_id="english", size=english_size
                    ),
                )
            elif query.query_text == "九品芝麻官":
                items = (
                    _candidate("九品芝麻官.1994.1080p.BluRay", torrent_id="chinese", size=1000),
                )
            else:
                items = ()
            return SearchPage("fake-site", 1, items, False)

    adapter = BilingualSearch()
    report = asyncio.run(
        UnpackMatchCoordinator(
            factory, _FakeSiteProvider(adapter), max_queries_per_site=2
        ).match_next_batch("execution-1")
    )
    assert report.matched_auto_count == 1
    expected_queries = (
        ["hail the judge", "九品芝麻官"] if english_size == 1600 else ["hail the judge"]
    )
    assert [query.query_text for query in adapter.queries] == expected_queries
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None and item.selected_candidate_id
        selected = session.get(UnpackMatchCandidate, item.selected_candidate_id)
        assert selected is not None
        assert selected.candidate_key == ("chinese" if english_size == 1600 else "english")


def test_search_diagnostics_explain_each_title_stage_without_leaking_queries(
    caplog: pytest.LogCaptureFixture,
) -> None:
    filename = "Hail.the.Judge.1994.1080p.BluRay.mkv"
    factory = _factory(threshold=10_000, filename=filename, size=7_600_000_000)
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        snapshot = dict(item.source_snapshot)
        snapshot["path"] = "/data/九品芝麻官.Hail.the.Judge.1994.1080p.BluRay/" + filename
        item.source_snapshot = snapshot
        session.commit()

    class RousiStyleSearch(_FakeSiteAdapter):
        async def capabilities(self) -> SiteSearchCapabilities:
            return SiteSearchCapabilities(min_request_interval_seconds=0.001)

        async def search(self, query: SearchQuery) -> SearchPage:
            self.queries.append(query)
            candidates: tuple[CandidateMeta, ...]
            if query.query_text == "hail the judge":
                candidates = (_candidate("The.Little.Death.2014.1080p.BluRay", torrent_id="99"),)
            elif query.query_text == "九品芝麻官":
                candidates = (
                    _candidate("九品芝麻官.1994.1080p.BluRay", torrent_id="101"),
                    _candidate("九品芝麻官.2014.1080p.BluRay", torrent_id="102"),
                    _candidate("Unrelated.Movie.1994.1080p", torrent_id="103"),
                    _candidate("九品芝麻官.1994.720p.BluRay", torrent_id="104"),
                )
            else:
                candidates = ()
            return SearchPage("fake-site", 1, candidates, False, len(candidates))

    adapter = RousiStyleSearch()
    service = UnpackMatchCoordinator(factory, _FakeSiteProvider(adapter), max_queries_per_site=2)
    with caplog.at_level(logging.INFO, logger="packbreaker.unpack.search_diagnostics"):
        report = asyncio.run(service.match_next_batch("execution-1"))
    assert [q.query_text for q in adapter.queries] == ["hail the judge", "九品芝麻官"]
    assert report.review_count == 1
    records = [
        record
        for record in caplog.records
        if record.name == "packbreaker.unpack.search_diagnostics"
    ]
    assert [record.__dict__["fields"]["query_stage"] for record in records] == [
        "ENGLISH_TITLE",
        "CHINESE_TITLE",
    ]
    assert records[0].__dict__["fields"]["returned_count"] == 1
    assert records[1].__dict__["fields"]["returned_count"] == 4
    assert records[1].__dict__["fields"]["hard_conflict_count"] >= 1
    assert records[1].__dict__["fields"]["selectable_count"] >= 1
    assert records[1].__dict__["fields"]["title_filtered_count"] >= 1
    assert "九品芝麻官" not in caplog.text
    assert "hail the judge" not in caplog.text
    assert "SECRET_PRIVATE_KEY" not in caplog.text


def test_real_rousi_adapter_mock_transport_keeps_chinese_search_candidates(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No external traffic: exercise real query serialization and matcher together."""
    filename = "Hail.the.Judge.1994.1080p.BluRay.mkv"
    factory = _factory(threshold=10_000, filename=filename, size=7_600_000_000)
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        snapshot = dict(item.source_snapshot)
        snapshot["path"] = "/data/九品芝麻官.Hail.the.Judge.1994.1080p.BluRay/" + filename
        item.source_snapshot = snapshot
        session.commit()

    searched: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/v1/torrents"
        assert request.url.params["offset"] == "0"
        assert request.url.params["limit"] == "50"
        assert request.headers.get("api-token") is None
        assert request.headers.get("cookie") is None
        query = request.url.params["query"]
        searched.append(query)
        titles = (
            ["The Little Death 2014 1080p BluRay x265"]
            if query == "hail the judge"
            else [
                "九品芝麻官 1994 1080p BluRay",
                "九品芝麻官 2014 1080p BluRay",
                "Unrelated Movie 1994 WEB-DL",
                "九品芝麻官 1994 720p BluRay",
            ]
        )
        return httpx2.Response(
            200,
            json={
                "limit": 50,
                "offset": 0,
                "total": len(titles),
                "items": [
                    {
                        "id": i + (100 if query == "hail the judge" else 200),
                        "name": title,
                        "size_bytes": 7_600_000_000,
                    }
                    for i, title in enumerate(titles)
                ],
            },
        )

    adapter = RousiProCandidateAdapter(
        "synthetic-api-key-private", transport=httpx2.MockTransport(handler)
    )

    class Provider:
        def enabled_adapters(self) -> tuple[EnabledSiteAdapter, ...]:
            return (
                EnabledSiteAdapter(
                    config_id="site-config-1",
                    config_version=7,
                    site_id="rousi_pro",
                    adapter=cast(SiteAdapter, adapter),
                ),
            )

    with caplog.at_level(logging.INFO, logger="packbreaker.unpack.search_diagnostics"):
        result = asyncio.run(
            UnpackMatchCoordinator(factory, Provider(), max_queries_per_site=2).match_next_batch(
                "execution-1"
            )
        )
    assert searched == ["hail the judge", "九品芝麻官"]
    assert result.review_count == 1
    diagnostic = [
        r.__dict__["fields"]
        for r in caplog.records
        if r.name == "packbreaker.unpack.search_diagnostics"
    ]
    assert [item["returned_count"] for item in diagnostic] == [1, 4]
    assert diagnostic[1]["query_stage"] == "CHINESE_TITLE"
    assert diagnostic[1]["selectable_count"] >= 1
    assert "synthetic-api-key-private" not in caplog.text
    assert "九品芝麻官" not in caplog.text


def test_all_unmatched_movies_result_in_failed_not_partially_completed() -> None:
    factory = _factory(threshold=10_000)
    report = asyncio.run(
        UnpackMatchCoordinator(factory, _FakeSiteProvider(_FakeSiteAdapter())).match_next_batch(
            "execution-1"
        )
    )
    assert report.no_match_count == 1
    assert report.execution_status is UnpackExecutionStatus.FAILED
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.last_error_message is not None
        assert "未找到符合影片名称" in item.last_error_message


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
        assert item.last_error_message is not None
        assert "年份或身份冲突" in item.last_error_message
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
    assert report.execution_status is UnpackExecutionStatus.FAILED
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == UnpackItemStatus.MATCH_TIMEOUT.value
        assert item.last_error_code == "UNPACK_MATCH_TIMEOUT"


def test_nonretryable_site_failure_does_not_consume_automatic_retry_budget() -> None:
    factory = _factory(threshold=10_000, retry_enabled=True, max_retries=3)
    adapter = _FakeSiteAdapter(
        error=SiteAdapterError("SITE_AUTH_FAILED", "synthetic private reason", retryable=False)
    )
    coordinator = UnpackMatchCoordinator(factory, _FakeSiteProvider(adapter))

    report = asyncio.run(coordinator.match_next_batch("execution-1"))
    assert report.processed_count == 1
    assert report.execution_status is UnpackExecutionStatus.FAILED
    assert report.error_count == 1
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == UnpackItemStatus.MATCH_ERROR.value
        assert item.last_error_code == "SITE_AUTH_FAILED"
        assert item.retry_count == 0
        assert item.auxiliary_state is not None
        assert item.auxiliary_state["search_failures"][0]["retryable"] is False
    # A terminal credential error must not trigger new query variations or worker loops.
    assert len(adapter.queries) == 1
    assert asyncio.run(coordinator.match_next_batch("execution-1")).processed_count == 0


def test_retryable_site_unavailable_exhaustion_is_match_error_not_timeout() -> None:
    from datetime import timedelta

    factory = _factory(threshold=10_000, retry_enabled=True, max_retries=1)
    adapter = _FakeSiteAdapter(
        error=SiteAdapterError("SITE_UNAVAILABLE", "temporary outage", retryable=True)
    )
    coordinator = UnpackMatchCoordinator(factory, _FakeSiteProvider(adapter))
    first = asyncio.run(coordinator.match_next_batch("execution-1"))
    assert first.execution_status is UnpackExecutionStatus.MATCHING
    assert len(adapter.queries) == 1  # no keyword fanout after transient failure
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.retry_count == 1
        state = dict(item.auxiliary_state or {})
        state["auto_retry_not_before"] = (utc_now() - timedelta(seconds=5)).isoformat()
        item.auxiliary_state = state
        session.commit()

    final = asyncio.run(coordinator.match_next_batch("execution-1"))
    assert final.processed_count == 1
    assert final.execution_status is UnpackExecutionStatus.FAILED
    assert final.error_count == 1
    assert final.timeout_count == 0
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == UnpackItemStatus.MATCH_ERROR.value
        assert item.last_error_code == "SITE_UNAVAILABLE"
        assert item.retry_count == 1


def test_site_retry_after_extends_automatic_match_backoff() -> None:
    from datetime import datetime, timedelta

    factory = _factory(threshold=10_000, retry_enabled=True, max_retries=2)
    adapter = _FakeSiteAdapter(
        error=SiteAdapterError(
            "SITE_RATE_LIMITED",
            "synthetic throttle",
            retryable=True,
            retry_after_seconds=120.0,
        )
    )
    coordinator = UnpackMatchCoordinator(factory, _FakeSiteProvider(adapter))

    started = utc_now()
    report = asyncio.run(coordinator.match_next_batch("execution-1"))
    assert report.execution_status is UnpackExecutionStatus.MATCHING
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == UnpackItemStatus.MATCH_PENDING.value
        assert item.retry_count == 1
        state = item.auxiliary_state or {}
        due = datetime.fromisoformat(state["auto_retry_not_before"])
        assert due >= started + timedelta(seconds=120)
    assert len(adapter.queries) == 1
    assert asyncio.run(coordinator.match_next_batch("execution-1")).processed_count == 0


@pytest.mark.parametrize("wait", (float("inf"), float("nan"), -10.0, 86401.0))
def test_invalid_or_unbounded_site_retry_after_fails_closed(wait: float) -> None:
    factory = _factory(threshold=10_000, retry_enabled=True, max_retries=3)
    adapter = _FakeSiteAdapter(
        error=SiteAdapterError(
            "SITE_RATE_LIMITED",
            "synthetic invalid cooldown",
            retryable=True,
            retry_after_seconds=wait,
        )
    )
    report = asyncio.run(
        UnpackMatchCoordinator(factory, _FakeSiteProvider(adapter)).match_next_batch("execution-1")
    )
    assert report.execution_status is UnpackExecutionStatus.FAILED
    assert report.error_count == 1
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == UnpackItemStatus.MATCH_ERROR.value
        assert item.last_error_code == "SITE_RATE_LIMITED"
        assert item.retry_count == 0


def test_match_timeout_automatically_retries_with_backoff_then_needs_manual_action() -> None:
    from datetime import timedelta

    factory = _factory(threshold=10_000, retry_enabled=True, max_retries=2)
    adapter = _FakeSiteAdapter(
        error=SiteAdapterError("SITE_TIMEOUT", "synthetic timeout", retryable=True)
    )
    coordinator = UnpackMatchCoordinator(factory, _FakeSiteProvider(adapter))

    first = asyncio.run(coordinator.match_next_batch("execution-1"))
    assert first.processed_count == 1
    assert first.execution_status is UnpackExecutionStatus.MATCHING
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == "MATCH_PENDING"
        assert item.retry_count == 1
        assert "auto_retry_not_before" in (item.auxiliary_state or {})

    # Backoff prevents spinning all retries inside one worker tick.
    assert asyncio.run(coordinator.match_next_batch("execution-1")).processed_count == 0

    for attempt in (2, 3):
        with factory() as session:
            item = session.get(UnpackExecutionItem, "item-1")
            assert item is not None
            state = dict(item.auxiliary_state or {})
            state["auto_retry_not_before"] = (utc_now() - timedelta(seconds=5)).isoformat()
            item.auxiliary_state = state
            session.commit()
        report = asyncio.run(coordinator.match_next_batch("execution-1"))
        assert report.processed_count == 1
        with factory() as session:
            item = session.get(UnpackExecutionItem, "item-1")
            assert item is not None
            if attempt == 2:
                assert item.status == "MATCH_PENDING"
                assert item.retry_count == 2
                assert report.execution_status is UnpackExecutionStatus.MATCHING
            else:
                assert item.status == "MATCH_TIMEOUT"
                assert item.retry_count == 2
                assert report.execution_status is UnpackExecutionStatus.FAILED


def test_no_match_never_consumes_automatic_retry_budget() -> None:
    factory = _factory(
        threshold=10_000,
        filename="Movie.2024.1080p.WEB-DL.x265.mkv",
        retry_enabled=True,
        max_retries=5,
    )
    report = asyncio.run(
        UnpackMatchCoordinator(factory, _FakeSiteProvider(_FakeSiteAdapter())).match_next_batch(
            "execution-1"
        )
    )
    assert report.no_match_count == 1
    assert report.execution_status is UnpackExecutionStatus.FAILED
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None and item.status == "NO_MATCH"
        assert item.retry_count == 0


def test_rousi_suspicious_identical_unfiltered_pages_are_not_no_match(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from dataclasses import replace

    from backend.app.application.sites import EnabledSiteAdapter

    factory = _factory(threshold=10_000, filename="Hail.the.Judge.1994.1080p.BluRay.mkv")
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        snapshot = dict(item.source_snapshot)
        snapshot["path"] = (
            "/data/九品芝麻官.Hail.the.Judge.1994.1080p.BluRay/Hail.the.Judge.1994.1080p.BluRay.mkv"
        )
        item.source_snapshot = snapshot
        session.commit()

    candidates = tuple(
        replace(
            _candidate(f"Unrelated.Film.{2000 + i % 18}", torrent_id=str(i + 10)),
            site_id="rousi_pro",
        )
        for i in range(100)
    )

    class UnfilteredRousi(_FakeSiteAdapter):
        async def capabilities(self) -> SiteSearchCapabilities:
            return SiteSearchCapabilities(min_request_interval_seconds=0.001)

        async def search(self, query: SearchQuery) -> SearchPage:
            self.queries.append(query)
            return SearchPage("rousi_pro", 1, candidates, True, 8995)

    adapter = UnfilteredRousi()

    class RousiProvider:
        def enabled_adapters(self) -> tuple[EnabledSiteAdapter, ...]:
            return (
                EnabledSiteAdapter("site-config-1", 1, "rousi_pro", cast(SiteAdapter, adapter)),
            )

    with caplog.at_level(logging.INFO, logger="packbreaker.unpack.search_diagnostics"):
        report = asyncio.run(
            UnpackMatchCoordinator(
                factory, RousiProvider(), max_queries_per_site=4
            ).match_next_batch("execution-1")
        )
    assert [q.query_text for q in adapter.queries] == ["hail the judge", "九品芝麻官"]
    assert report.error_count == 1
    assert report.no_match_count == 0
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == "MATCH_ERROR"
        assert item.last_error_code == "SITE_SEARCH_FILTER_IGNORED_SUSPECTED"
    assert any(
        record.__dict__.get("fields", {}).get("outcome") == "QUERY_FILTER_IGNORED_SUSPECTED"
        for record in caplog.records
    )


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
    assert report.execution_status is UnpackExecutionStatus.FAILED
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
