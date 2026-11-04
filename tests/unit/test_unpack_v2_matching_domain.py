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


def test_unpack_queries_include_roman_numeral_and_chinese_title_fallbacks() -> None:
    rocky = parse_media_name("Rocky.II.1979.1080p.BluRay.x265.10bit.4Audio.MNHD-FRDS")
    rocky_queries = build_unpack_search_queries(
        rocky,
        unit_kind=TaskUnitKind.MOVIE,
        capabilities=SiteSearchCapabilities(supports_imdb_id=True),
    )
    rocky_texts = [query.query_text for query in rocky_queries]
    assert "rocky ii" in rocky_texts
    assert "rocky ii 1979" in rocky_texts
    assert "rocky 2" in rocky_texts
    assert "rocky 2 1979" in rocky_texts

    better_tomorrow = parse_media_name(
        "Top138.英雄本色(4K修复版).A.Better.Tomorrow.1986.REMASTERED."
        "Bluray.1080p.x265.AAC(5.1).2Audios.GREENOTEA"
    )
    queries = build_unpack_search_queries(
        better_tomorrow,
        unit_kind=TaskUnitKind.MOVIE,
        capabilities=SiteSearchCapabilities(),
    )
    texts = [query.query_text for query in queries]
    assert "a better tomorrow" in texts
    assert "a better tomorrow 1986" in texts
    assert "英雄本色" in texts
    assert "英雄本色 1986" in texts
    assert all("top138" not in text for text in texts)


def test_unpack_queries_add_sequel_number_fallback_without_audio_channel_noise() -> None:
    source = parse_media_name("Mad.Max.2.1981.BluRay.1080p.DTS-HD.MA.5.1.2Audios.MNHD-FRDS")
    queries = build_unpack_search_queries(
        source,
        unit_kind=TaskUnitKind.MOVIE,
        capabilities=SiteSearchCapabilities(),
    )
    texts = [query.query_text for query in queries]
    assert "mad max 2 1981" in texts
    assert "mad max 1981" in texts
    assert all("5 1" not in text for text in texts)

    numbered_part = parse_media_name(
        "Harry.Potter.and.the.Deathly.Hallows.Part.2.2011.BluRay.1080p.x265"
    )
    numbered_part_queries = build_unpack_search_queries(
        numbered_part,
        unit_kind=TaskUnitKind.MOVIE,
        capabilities=SiteSearchCapabilities(),
    )
    assert "harry potter and the deathly hallows part 2011" not in [
        query.query_text for query in numbered_part_queries
    ]


def test_single_word_english_title_uses_year_and_remains_search_relevant() -> None:
    from backend.app.domain.candidate_scoring import candidate_search_relevant

    source = parse_media_name(
        "Leon.1994.Extended.Remastered.BluRay.1080p.x265.10bit.2Audio.MNHD-FRDS",
        aliases=("这个杀手不太冷",),
    )
    candidate = parse_media_name(
        "Leon The Professional 1994 REMASTERED EXTENDED 1080p BluRay TrueHD "
        "7.1 Atmos 2Audio H.265 10bit-CrsS"
    )
    queries = build_unpack_search_queries(
        source,
        unit_kind=TaskUnitKind.MOVIE,
        capabilities=SiteSearchCapabilities(),
    )

    assert queries[0].query_text == "leon 1994"
    assert "这个杀手不太冷" in [query.query_text for query in queries]
    assert candidate_search_relevant(source, candidate)


def test_unknown_metadata_is_not_reported_as_matching_and_group_mismatch_is_explicit() -> None:
    source = parse_media_name(
        "Leon.1994.Extended.Remastered.BluRay.1080p.x265.10bit.2Audio.MNHD-FRDS",
        aliases=("这个杀手不太冷",),
        total_size=1000,
    )
    candidate = parse_media_name(
        "这个杀手不太冷 1994 Leon The Professional 1994 "
        "2in1 2160p UHD Blu-ray HEVC Atmos TrueHD 7.1-CrsS",
        total_size=9000,
    )

    exact = assess_unpack_candidate(source, candidate).evidence["exact"]

    assert exact["external_id"] is None
    assert exact["episode"] is None
    assert exact["release_group"] is False
    assert exact["resolution"] is False
    assert exact["size"] is False


def test_long_title_search_has_compact_stopword_free_fallback() -> None:
    source = parse_media_name(
        "Harry.Potter.and.the.Prisoner.of.Azkaban.2004.BluRay.1080p.x265.10bit.2Audio.MNHD-FRDS"
    )
    queries = build_unpack_search_queries(
        source,
        unit_kind=TaskUnitKind.MOVIE,
        capabilities=SiteSearchCapabilities(),
    )

    assert "harry potter prisoner azkaban 2004" in [query.query_text for query in queries]
