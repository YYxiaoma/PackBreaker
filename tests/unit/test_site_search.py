from dataclasses import replace
from datetime import UTC, datetime

import pytest

from backend.app.domain.media_matching import (
    EpisodeIdentity,
    EpisodeKind,
    ExternalMediaId,
    MediaFileSummary,
)
from backend.app.domain.site_search import (
    SearchMediaType,
    SearchPage,
    SearchQuery,
    SiteSearchCapabilities,
    build_search_queries,
    normalize_candidate_meta,
)
from backend.app.domain.task_units import SourceTaskFile, identify_task_units


def test_episode_search_plan_is_stable_and_progressively_relaxed() -> None:
    unit = identify_task_units((SourceTaskFile("Pack/Show.2024.S01E02.1080p.mkv", 100),))[0]
    descriptor = replace(
        unit.descriptor,
        external_ids=(ExternalMediaId("imdb", "tt1234567"),),
    )
    unit = replace(unit, descriptor=descriptor)

    queries = build_search_queries(unit)

    assert len(queries) == 3
    assert queries[0].media_type is SearchMediaType.TV
    assert queries[0].query_text == "show 2024 s01e02"
    assert queries[0].external_ids == (ExternalMediaId("imdb", "tt1234567"),)
    assert queries[1].query_text == "show 2024 s01e02"
    assert queries[1].external_ids == ()
    assert queries[2].query_text == "show"
    assert all("+" not in item.query_text and "%" not in item.query_text for item in queries)


def test_movie_search_query_uses_movie_type_and_has_bounded_fallbacks() -> None:
    unit = identify_task_units((SourceTaskFile("Pack/Movie.2024.2160p.mkv", 100),))[0]

    queries = build_search_queries(unit, max_queries=2)

    assert len(queries) == 2
    assert all(item.media_type is SearchMediaType.MOVIE for item in queries)
    assert queries[0].query_text == "movie 2024"
    assert queries[1].query_text == "movie"


def test_search_query_validates_bounds_and_deduplicates_external_ids() -> None:
    query = SearchQuery(
        (" Movie ",),
        SearchMediaType.MOVIE,
        external_ids=(ExternalMediaId("IMDB", "TT1234567"), ExternalMediaId("imdb", "tt1234567")),
    )

    assert query.keywords == ("movie",)
    assert query.external_ids == (ExternalMediaId("imdb", "tt1234567"),)
    with pytest.raises(ValueError):
        SearchQuery((), SearchMediaType.UNKNOWN)
    with pytest.raises(ValueError):
        SearchQuery(("movie",), SearchMediaType.MOVIE, page_size=101)
    with pytest.raises(ValueError):
        SearchQuery(("movie",), SearchMediaType.MOVIE, external_ids=(ExternalMediaId("", "1"),))


def test_malformed_episode_identity_is_not_silently_rendered_as_zero() -> None:
    unit = identify_task_units((SourceTaskFile("Pack/Show.S01E02.mkv", 100),))[0]
    unit = replace(
        unit,
        descriptor=replace(
            unit.descriptor,
            episode=EpisodeIdentity(EpisodeKind.SEASON_EPISODE, season=1, start=None),
        ),
    )

    with pytest.raises(ValueError):
        build_search_queries(unit)


def test_candidate_meta_normalizes_claims_without_inventing_missing_fields() -> None:
    published = datetime(2026, 9, 9, 12, 30, tzinfo=UTC)
    candidate = normalize_candidate_meta(
        site_id="mteam",
        torrent_id="123",
        display_name="Movie.2024.2160p.WEB-DL.H265",
        total_size=1234,
        published_at=published,
        seeders=8,
        external_ids=(ExternalMediaId("imdb", "tt1234567"),),
    )

    assert candidate.identity == ("mteam", "123")
    assert candidate.total_size == 1234
    assert candidate.seeders == 8
    assert candidate.leechers is None
    assert candidate.category is None
    assert candidate.descriptor.year == 2024
    assert candidate.descriptor.external_ids == (ExternalMediaId("imdb", "tt1234567"),)


def test_candidate_meta_rejects_negative_or_naive_claims() -> None:
    base = normalize_candidate_meta(site_id="site", torrent_id="1", display_name="Movie")
    with pytest.raises(ValueError):
        replace(base, seeders=-1)
    with pytest.raises(ValueError):
        replace(base, published_at=datetime(2026, 1, 1))


def test_search_page_cannot_mix_sites_or_duplicate_remote_ids() -> None:
    first = normalize_candidate_meta(site_id="site", torrent_id="1", display_name="Movie A")
    second = normalize_candidate_meta(site_id="other", torrent_id="2", display_name="Movie B")

    with pytest.raises(ValueError):
        SearchPage("site", 1, (first, second), False)
    with pytest.raises(ValueError):
        SearchPage("site", 1, (first, first), False)


def test_site_capabilities_reject_negative_rate_interval() -> None:
    with pytest.raises(ValueError):
        SiteSearchCapabilities(min_request_interval_seconds=-0.1)


def test_file_summary_is_kept_as_search_claim_not_verified_torrent_fact() -> None:
    summary = (MediaFileSummary("Movie.mkv", 100),)
    candidate = normalize_candidate_meta(
        site_id="site",
        torrent_id="7",
        display_name="Movie",
        file_summary=summary,
    )

    assert candidate.file_summary == summary
    assert candidate.descriptor.files == summary
