from backend.app.domain.media_matching import MediaFileSummary, parse_media_name
from backend.app.domain.site_search import SiteSearchCapabilities
from backend.app.domain.task_units import TaskUnitKind
from backend.app.domain.unpack_matching import (
    assess_unpack_candidate,
    build_unpack_search_queries,
)


def test_unpack_query_strategy_prioritizes_imdb_then_douban_then_title() -> None:
    source = parse_media_name(
        "Movie.2024.1080p.tt1234567.douban123456",
        total_size=1000,
    )
    queries = build_unpack_search_queries(
        source,
        unit_kind=TaskUnitKind.MOVIE,
        capabilities=SiteSearchCapabilities(
            supports_imdb_id=True,
            supports_douban_id=True,
        ),
    )

    assert len(queries) == 4
    assert [(item.namespace, item.value) for item in queries[0].external_ids] == [
        ("imdb", "tt1234567")
    ]
    assert [(item.namespace, item.value) for item in queries[1].external_ids] == [
        ("douban", "123456")
    ]
    assert queries[2].query_text == "movie 2024"
    assert queries[3].query_text == "movie"


def test_unpack_query_strategy_respects_site_id_capabilities() -> None:
    source = parse_media_name("Movie.2024.tt1234567.douban123456")
    queries = build_unpack_search_queries(
        source,
        unit_kind=TaskUnitKind.MOVIE,
        capabilities=SiteSearchCapabilities(
            supports_imdb_id=False,
            supports_douban_id=True,
        ),
    )

    assert queries[0].external_ids[0].namespace == "douban"
    assert all(
        not any(item.namespace == "imdb" for item in query.external_ids) for query in queries
    )


def test_display_score_100_does_not_imply_exact_match() -> None:
    source = parse_media_name(
        "Movie.2024.1080p.WEB-DL.x265.tt1234567",
        release_group="GROUP",
        total_size=1000,
        files=(MediaFileSummary("Movie.mkv", 1000),),
    )
    candidate = parse_media_name(
        "Movie.2024.1080p.WEB-DL.x265.tt1234567",
        release_group=None,
        total_size=1000,
        files=(MediaFileSummary("Movie.mkv", 1000),),
    )

    assessment = assess_unpack_candidate(source, candidate)

    assert assessment.score_bps == 10_000
    assert assessment.is_exact_match is False
    assert assessment.evidence["exact"]["release_group"] is False


def test_exact_match_requires_explicit_metadata_evidence() -> None:
    source = parse_media_name(
        "Movie.2024.2160p.WEB-DL.x265.tt1234567",
        release_group="GROUP",
        total_size=1000,
        files=(MediaFileSummary("Movie.mkv", 1000),),
    )
    candidate = parse_media_name(
        "Movie.2024.2160p.WEB-DL.x265.tt1234567",
        release_group="group",
        total_size=1000,
        files=(MediaFileSummary("Movie.mkv", 1000),),
    )

    assessment = assess_unpack_candidate(source, candidate)

    assert assessment.score_bps == 10_000
    assert assessment.is_exact_match is True
    assert assessment.rejected is False


def test_bilingual_chinese_movie_keeps_short_rousi_search_and_candidate_relevance() -> None:
    from backend.app.domain.candidate_scoring import candidate_search_relevant
    from backend.app.domain.media_matching import parse_media_name
    from backend.app.domain.site_search import SiteSearchCapabilities
    from backend.app.domain.task_units import TaskUnitKind
    from backend.app.domain.unpack_matching import build_unpack_search_queries

    source = parse_media_name(
        "九品芝麻官.Hail.the.Judge.1994.BluRay.1080p.x265.10bit.2Audio.MNHD-FRDS"
    )
    candidate = parse_media_name("九品芝麻官.1994.1080p.BluRay.x265")
    queries = build_unpack_search_queries(
        source,
        unit_kind=TaskUnitKind.MOVIE,
        capabilities=SiteSearchCapabilities(min_request_interval_seconds=2),
    )
    assert queries[0].query_text == "hail the judge"
    assert queries[1].query_text == "九品芝麻官"
    assert candidate_search_relevant(source, candidate)


def test_unpack_source_directory_title_enables_english_then_chinese_fallback() -> None:
    from types import SimpleNamespace
    from typing import cast

    from backend.app.application.unpack_matching import _task_unit_from_item
    from backend.app.domain.candidate_scoring import candidate_search_relevant
    from backend.app.infrastructure.persistence.models import UnpackExecutionItem

    filename = "Hail.the.Judge.1994.BluRay.1080p.x265.10bit.2Audio.MNHD-FRDS.mkv"
    parent = "九品芝麻官." + filename.removesuffix(".mkv")
    source = cast(
        UnpackExecutionItem,
        SimpleNamespace(
            source_object_key="source-one",
            source_snapshot={
                "path": f"/data/{parent}/{filename}",
                "relative_path": filename,
                "size": 4_000_000_000,
            },
        ),
    )
    unit = _task_unit_from_item(source)
    queries = build_unpack_search_queries(
        unit.descriptor,
        unit_kind=unit.kind,
        capabilities=SiteSearchCapabilities(min_request_interval_seconds=2),
    )
    assert [query.query_text for query in queries[:2]] == ["hail the judge", "九品芝麻官"]
    assert candidate_search_relevant(
        unit.descriptor,
        parse_media_name("九品芝麻官.1994.1080p.BluRay.x265"),
    )
    other = cast(
        UnpackExecutionItem,
        SimpleNamespace(
            source_object_key="source-other",
            source_snapshot={
                "path": f"/data/不相关的目录/{filename}",
                "relative_path": filename,
                "size": 4_000_000_000,
            },
        ),
    )
    assert ("九品芝麻官",) not in _task_unit_from_item(other).descriptor.alias_tokens
