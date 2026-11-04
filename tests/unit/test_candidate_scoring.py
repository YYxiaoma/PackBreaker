from backend.app.domain.candidate_scoring import (
    HardConflict,
    ScoreDimension,
    ScoringConfig,
    candidate_search_relevant,
    rank_candidates,
    score_candidate,
)
from backend.app.domain.media_matching import MediaDescriptor, MediaFileSummary, parse_media_name
from backend.app.domain.unpack_matching import assess_unpack_candidate


def _descriptor(name: str, *, size: int = 1000, group: str | None = "grp") -> MediaDescriptor:
    return parse_media_name(
        name,
        release_group=group,
        total_size=size,
        files=(MediaFileSummary("movie.mkv", size),),
    )


def test_perfect_known_candidate_scores_100_but_never_allows_action() -> None:
    source = _descriptor("Movie.2024.2160p.WEB-DL.H265.HDR10.Atmos.tt1234567")
    candidate = _descriptor("Movie.2024.4K.WEBDL.HEVC.HDR10.Atmos.tt1234567")

    result = score_candidate(source, candidate)

    assert result.score == 100.0
    assert result.rejected is False
    assert result.automatic_action_allowed is False
    assert result.algorithm_version
    assert result.config_version == "source-known-size-penalty-v3"


def test_external_id_year_and_episode_conflicts_are_hard_rejections() -> None:
    source = _descriptor("Show.2024.S01E02.tt1234567")
    candidate = _descriptor("Show.2023.S01E03.tt7654321")

    result = score_candidate(source, candidate)

    assert result.rejected is True
    assert result.score == 0
    assert set(result.hard_conflicts) == {
        HardConflict.EXTERNAL_ID,
        HardConflict.YEAR,
        HardConflict.EPISODE,
    }


def test_size_score_degrades_within_configured_tolerance() -> None:
    source = _descriptor("Movie.2024", size=1000)
    candidate = _descriptor("Movie.2024", size=1025)

    result = score_candidate(source, candidate, config=ScoringConfig(size_tolerance_ratio=0.05))

    size_dimension = next(
        item for item in result.dimensions if item.dimension is ScoreDimension.TOTAL_SIZE
    )
    assert 0 < size_dimension.earned < 10


def test_missing_candidate_size_is_zero_points_but_not_claimed_as_evidence() -> None:
    source = parse_media_name(
        "Movie.2024.1080p.WEB-DL.x265",
        total_size=1_000_000,
    )
    candidate = parse_media_name("Movie.2024.1080p.WEB-DL.x265")

    score = score_candidate(source, candidate)
    assessment = assess_unpack_candidate(source, candidate)
    dimensions = {item.dimension: item for item in score.dimensions}
    size_dimension = dimensions[ScoreDimension.TOTAL_SIZE]

    assert size_dimension.maximum == 10
    assert size_dimension.earned == 0
    assert size_dimension.coverage_maximum == 0
    assert score.score < 100
    assert assessment.score_bps < 10_000
    assert assessment.evidence["evidence_coverage_bps"] == 5_000
    persisted_size = next(
        item for item in assessment.evidence["dimensions"] if item["dimension"] == "TOTAL_SIZE"
    )
    assert persisted_size["maximum"] == 10
    assert persisted_size["coverage_maximum"] == 0
    assert "未提供可比较的影片大小" in persisted_size["evidence"]


def test_missing_release_group_is_not_counted_as_matching_evidence() -> None:
    source = _descriptor("Movie.2024", group="grp")
    candidate = _descriptor("Movie.2024", group=None)

    result = score_candidate(source, candidate)

    group_dimension = next(
        item for item in result.dimensions if item.dimension is ScoreDimension.RELEASE_GROUP
    )
    assert group_dimension.earned == 0
    assert group_dimension.maximum == 0


def test_ranking_is_deterministic_and_rejected_candidates_are_last() -> None:
    source = _descriptor("Movie.2024.tt1234567")
    good = _descriptor("Movie.2024.tt1234567")
    weaker = _descriptor("Movie Extended.2024.tt1234567", size=1040)
    rejected = _descriptor("Movie.2023.tt7654321")

    ranking = rank_candidates(
        source,
        (("z-rejected", rejected), ("b-weaker", weaker), ("a-good", good)),
    )

    assert [item.candidate_id for item in ranking] == ["a-good", "b-weaker", "z-rejected"]
    assert ranking[-1].result.rejected is True


def test_missing_external_ids_and_file_list_do_not_cap_match_score() -> None:
    source = parse_media_name(
        "The.Usual.Suspects.1995.BluRay.1080p.x265.10bit.2Audio.MNHD-FRDS",
        release_group="frds",
        total_size=10_000_000,
    )
    candidate = parse_media_name(
        "The Usual Suspects 1995 1080p BluRay x265 DDP5.1 2Audios-MNHD-FRDS",
        release_group="frds",
        total_size=9_995_100,
    )

    result = score_candidate(source, candidate)
    dimensions = {item.dimension: item for item in result.dimensions}

    assert result.score > 99.0
    assert dimensions[ScoreDimension.EXTERNAL_ID].maximum == 0
    assert dimensions[ScoreDimension.FILE_LIST].maximum == 0
    assert dimensions[ScoreDimension.RELEASE_GROUP].earned == 5


def test_search_relevance_rejects_unrelated_site_noise_before_review() -> None:
    source = _descriptor("The Great Movie.2024.1080p")
    relevant = _descriptor("The Great Movie Extended.2024.2160p")
    unrelated = _descriptor("Completely Different Documentary.2024.1080p")

    assert candidate_search_relevant(source, relevant) is True
    assert candidate_search_relevant(source, unrelated) is False


def test_search_relevance_accepts_exact_external_id_even_when_title_differs() -> None:
    source = _descriptor("The Great Movie.2024.tt1234567")
    localized = _descriptor("完全不同的本地化标题.2024.tt1234567")

    assert candidate_search_relevant(source, localized) is True


def test_matching_release_watermark_supplies_coverage_without_lowering_threshold() -> None:
    source = parse_media_name(
        "Movie.2024.1080p.WEB-DL.x265.10bit.AC3￡cXcY@FRDS",
        total_size=10_000_000,
    )
    candidate = parse_media_name(
        "Movie 2024 1080p WEB-DL x265 AC3￡cXcY@FRDS",
        total_size=9_999_000,
    )

    result = assess_unpack_candidate(source, candidate)

    assert result.score_bps >= 9900
    assert result.evidence["evidence_coverage_bps"] == 6500
    assert result.evidence["exact"]["release_group"] is True
    assert result.rejected is False
