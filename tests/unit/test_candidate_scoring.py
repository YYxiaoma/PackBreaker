from backend.app.domain.candidate_scoring import (
    HardConflict,
    ScoreDimension,
    ScoringConfig,
    rank_candidates,
    score_candidate,
)
from backend.app.domain.media_matching import MediaDescriptor, MediaFileSummary, parse_media_name


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
    assert result.config_version == "initial-100-v1"


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


def test_missing_release_group_is_neutral() -> None:
    source = _descriptor("Movie.2024", group="grp")
    candidate = _descriptor("Movie.2024", group=None)

    result = score_candidate(source, candidate)

    group_dimension = next(
        item for item in result.dimensions if item.dimension is ScoreDimension.RELEASE_GROUP
    )
    assert group_dimension.earned == 5


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
