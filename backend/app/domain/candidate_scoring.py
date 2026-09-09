from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from math import isfinite

from backend.app.domain.media_matching import MediaDescriptor, MediaFileSummary, normalize_text

SCORING_ALGORITHM_VERSION = "packbreaker-candidate-score-v1"


class ScoreDimension(StrEnum):
    EXTERNAL_ID = "EXTERNAL_ID"
    TITLE = "TITLE"
    YEAR_EPISODE = "YEAR_EPISODE"
    RELEASE_ATTRIBUTES = "RELEASE_ATTRIBUTES"
    TOTAL_SIZE = "TOTAL_SIZE"
    FILE_LIST = "FILE_LIST"
    RELEASE_GROUP = "RELEASE_GROUP"


class HardConflict(StrEnum):
    EXTERNAL_ID = "EXTERNAL_ID_CONFLICT"
    YEAR = "YEAR_CONFLICT"
    EPISODE = "EPISODE_CONFLICT"


@dataclass(frozen=True, slots=True)
class ScoringConfig:
    version: str = "initial-100-v1"
    size_tolerance_ratio: float = 0.05

    def __post_init__(self) -> None:
        if not isfinite(self.size_tolerance_ratio) or self.size_tolerance_ratio <= 0:
            raise ValueError("size_tolerance_ratio 必须是有限正数")


@dataclass(frozen=True, slots=True)
class DimensionScore:
    dimension: ScoreDimension
    earned: float
    maximum: int
    evidence: str


@dataclass(frozen=True, slots=True)
class CandidateScore:
    score: float
    rejected: bool
    hard_conflicts: tuple[HardConflict, ...]
    dimensions: tuple[DimensionScore, ...]
    algorithm_version: str
    config_version: str
    automatic_action_allowed: bool = False


@dataclass(frozen=True, slots=True)
class RankedCandidate:
    candidate_id: str
    result: CandidateScore


def score_candidate(
    source: MediaDescriptor,
    candidate: MediaDescriptor,
    *,
    config: ScoringConfig | None = None,
) -> CandidateScore:
    resolved = config or ScoringConfig()
    conflicts = _hard_conflicts(source, candidate)
    dimensions = (
        _score_external_ids(source, candidate),
        _score_title(source, candidate),
        _score_year_episode(source, candidate),
        _score_release_attributes(source, candidate),
        _score_total_size(source, candidate, resolved),
        _score_file_list(source.files, candidate.files),
        _score_release_group(source, candidate),
    )
    score = 0.0 if conflicts else round(sum(item.earned for item in dimensions), 4)
    return CandidateScore(
        score=score,
        rejected=bool(conflicts),
        hard_conflicts=conflicts,
        dimensions=dimensions,
        algorithm_version=SCORING_ALGORITHM_VERSION,
        config_version=resolved.version,
    )


def rank_candidates(
    source: MediaDescriptor,
    candidates: tuple[tuple[str, MediaDescriptor], ...],
    *,
    config: ScoringConfig | None = None,
) -> tuple[RankedCandidate, ...]:
    scored = tuple(
        RankedCandidate(candidate_id, score_candidate(source, candidate, config=config))
        for candidate_id, candidate in candidates
    )
    return tuple(
        sorted(
            scored, key=lambda item: (item.result.rejected, -item.result.score, item.candidate_id)
        )
    )


def _hard_conflicts(
    source: MediaDescriptor, candidate: MediaDescriptor
) -> tuple[HardConflict, ...]:
    conflicts: list[HardConflict] = []
    left_ids = {item.namespace: item.value for item in source.external_ids}
    right_ids = {item.namespace: item.value for item in candidate.external_ids}
    if any(left_ids[key] != right_ids[key] for key in left_ids.keys() & right_ids.keys()):
        conflicts.append(HardConflict.EXTERNAL_ID)
    if source.year is not None and candidate.year is not None and source.year != candidate.year:
        conflicts.append(HardConflict.YEAR)
    if (
        source.episode is not None
        and candidate.episode is not None
        and source.episode != candidate.episode
    ):
        conflicts.append(HardConflict.EPISODE)
    return tuple(conflicts)


def _score_external_ids(source: MediaDescriptor, candidate: MediaDescriptor) -> DimensionScore:
    left = {(item.namespace, item.value) for item in source.external_ids}
    right = {(item.namespace, item.value) for item in candidate.external_ids}
    earned = 25.0 if left and right and left & right else 0.0
    return DimensionScore(
        ScoreDimension.EXTERNAL_ID,
        earned,
        25,
        "存在相同外部媒体 ID" if earned else "无共同外部媒体 ID",
    )


def _score_title(source: MediaDescriptor, candidate: MediaDescriptor) -> DimensionScore:
    left = (source.title_tokens, *source.alias_tokens)
    right = (candidate.title_tokens, *candidate.alias_tokens)
    similarity = max((_dice(a, b) for a in left for b in right), default=0.0)
    return DimensionScore(
        ScoreDimension.TITLE,
        round(20 * similarity, 4),
        20,
        f"token Dice={similarity:.4f}",
    )


def _score_year_episode(source: MediaDescriptor, candidate: MediaDescriptor) -> DimensionScore:
    comparisons: list[bool] = []
    if source.year is not None and candidate.year is not None:
        comparisons.append(source.year == candidate.year)
    if source.episode is not None and candidate.episode is not None:
        comparisons.append(source.episode == candidate.episode)
    ratio = sum(comparisons) / len(comparisons) if comparisons else 0.0
    return DimensionScore(
        ScoreDimension.YEAR_EPISODE,
        round(15 * ratio, 4),
        15,
        f"可比较字段={len(comparisons)}",
    )


def _score_release_attributes(
    source: MediaDescriptor, candidate: MediaDescriptor
) -> DimensionScore:
    fields = ("resolution", "release_source", "codec", "hdr", "audio")
    comparisons = [
        getattr(source, field) == getattr(candidate, field)
        for field in fields
        if getattr(source, field) is not None and getattr(candidate, field) is not None
    ]
    ratio = sum(comparisons) / len(comparisons) if comparisons else 0.0
    return DimensionScore(
        ScoreDimension.RELEASE_ATTRIBUTES,
        round(15 * ratio, 4),
        15,
        f"可比较属性={len(comparisons)}",
    )


def _score_total_size(
    source: MediaDescriptor,
    candidate: MediaDescriptor,
    config: ScoringConfig,
) -> DimensionScore:
    if source.total_size is None or candidate.total_size is None:
        return DimensionScore(ScoreDimension.TOTAL_SIZE, 0.0, 10, "缺少可比较总大小")
    denominator = max(source.total_size, candidate.total_size)
    if denominator == 0:
        earned = 10.0 if source.total_size == candidate.total_size else 0.0
        return DimensionScore(ScoreDimension.TOTAL_SIZE, earned, 10, "零长度总大小")
    difference = abs(source.total_size - candidate.total_size) / denominator
    ratio = max(0.0, 1.0 - difference / config.size_tolerance_ratio)
    return DimensionScore(
        ScoreDimension.TOTAL_SIZE,
        round(10 * ratio, 4),
        10,
        f"相对差异={difference:.6f}",
    )


def _score_file_list(
    source: tuple[MediaFileSummary, ...], candidate: tuple[MediaFileSummary, ...]
) -> DimensionScore:
    if not source or not candidate:
        return DimensionScore(ScoreDimension.FILE_LIST, 0.0, 10, "缺少文件列表摘要")
    count_score = 2.0 if len(source) == len(candidate) else 0.0
    left = {_file_signature(item) for item in source}
    right = {_file_signature(item) for item in candidate}
    similarity = len(left & right) / len(left | right) if left or right else 1.0
    return DimensionScore(
        ScoreDimension.FILE_LIST,
        round(count_score + 8 * similarity, 4),
        10,
        f"文件签名 Jaccard={similarity:.4f}",
    )


def _score_release_group(source: MediaDescriptor, candidate: MediaDescriptor) -> DimensionScore:
    if source.release_group is None or candidate.release_group is None:
        return DimensionScore(ScoreDimension.RELEASE_GROUP, 5.0, 5, "制作组缺失按中性处理")
    earned = 5.0 if source.release_group == candidate.release_group else 0.0
    return DimensionScore(
        ScoreDimension.RELEASE_GROUP,
        earned,
        5,
        "制作组相同" if earned else "制作组不同",
    )


def _dice(left: tuple[str, ...], right: tuple[str, ...]) -> float:
    left_set = set(left)
    right_set = set(right)
    if not left_set and not right_set:
        return 1.0
    if not left_set or not right_set:
        return 0.0
    return 2 * len(left_set & right_set) / (len(left_set) + len(right_set))


def _file_signature(item: MediaFileSummary) -> tuple[str, str, int]:
    return (normalize_text(item.basename), item.extension, item.length)
