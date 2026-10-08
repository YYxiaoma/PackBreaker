from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from backend.app.domain.candidate_scoring import CandidateScore, score_candidate
from backend.app.domain.media_matching import MediaDescriptor, normalize_text
from backend.app.domain.site_search import (
    SearchMediaType,
    SearchQuery,
    SiteSearchCapabilities,
)
from backend.app.domain.task_units import TaskUnitKind

EXACT_SIZE_TOLERANCE_RATIO = 0.001


@dataclass(frozen=True, slots=True)
class UnpackCandidateAssessment:
    score_bps: int
    is_exact_match: bool
    evidence: dict[str, Any]
    rejected: bool


def build_unpack_search_queries(
    source: MediaDescriptor,
    *,
    unit_kind: TaskUnitKind,
    capabilities: SiteSearchCapabilities,
    max_queries: int = 4,
) -> tuple[SearchQuery, ...]:
    if not 1 <= max_queries <= 8:
        raise ValueError("max_queries 必须位于 1..8")
    media_type = _media_type(unit_kind)
    title_tokens = source.title_tokens
    structured = tuple(
        token
        for token in (
            str(source.year) if source.year is not None else None,
            _episode_token(source),
        )
        if token is not None
    )
    queries: list[SearchQuery] = []

    imdb = next((item for item in source.external_ids if item.namespace == "imdb"), None)
    if imdb is not None and capabilities.supports_imdb_id:
        _append_query(
            queries,
            SearchQuery(
                keywords=(),
                media_type=media_type,
                episode=source.episode,
                external_ids=(imdb,),
            ),
        )

    douban = next((item for item in source.external_ids if item.namespace == "douban"), None)
    if douban is not None and capabilities.supports_douban_id:
        _append_query(
            queries,
            SearchQuery(
                keywords=(),
                media_type=media_type,
                episode=source.episode,
                external_ids=(douban,),
            ),
        )

    if title_tokens:
        _append_query(
            queries,
            SearchQuery(
                keywords=tuple((*title_tokens, *structured)),
                media_type=media_type,
                episode=source.episode,
            ),
        )
        _append_query(
            queries,
            SearchQuery(
                keywords=title_tokens,
                media_type=media_type,
                episode=source.episode,
            ),
        )
    return tuple(queries[:max_queries])


def assess_unpack_candidate(
    source: MediaDescriptor,
    candidate: MediaDescriptor,
) -> UnpackCandidateAssessment:
    score = score_candidate(source, candidate)
    score_bps = max(0, min(10_000, round(score.score * 100)))
    exact_evidence = _exact_evidence(source, candidate, score)
    return UnpackCandidateAssessment(
        score_bps=score_bps,
        is_exact_match=all(exact_evidence.values()) and not score.rejected,
        evidence={
            "algorithm_version": score.algorithm_version,
            "config_version": score.config_version,
            "hard_conflicts": [item.value for item in score.hard_conflicts],
            "dimensions": [
                {
                    "dimension": item.dimension.value,
                    "earned": item.earned,
                    "maximum": item.maximum,
                    "evidence": item.evidence,
                }
                for item in score.dimensions
            ],
            "exact": exact_evidence,
        },
        rejected=score.rejected,
    )


def _exact_evidence(
    source: MediaDescriptor,
    candidate: MediaDescriptor,
    score: CandidateScore,
) -> dict[str, bool]:
    source_titles = (source.title_tokens, *source.alias_tokens)
    candidate_titles = (candidate.title_tokens, *candidate.alias_tokens)
    title_exact = any(
        left and right and left == right for left in source_titles for right in candidate_titles
    )

    source_ids = {(item.namespace, item.value) for item in source.external_ids}
    candidate_ids = {(item.namespace, item.value) for item in candidate.external_ids}
    external_id_ok = not source_ids or bool(source_ids & candidate_ids)

    year_ok = source.year is None or candidate.year == source.year
    episode_ok = source.episode is None or candidate.episode == source.episode
    resolution_ok = source.resolution is None or candidate.resolution == source.resolution
    source_medium_ok = (
        source.release_source is None or candidate.release_source == source.release_source
    )

    if source.release_group is None:
        release_group_ok = True
    else:
        release_group_ok = candidate.release_group is not None and normalize_text(
            candidate.release_group
        ) == normalize_text(source.release_group)

    size_ok = _size_exact(source.total_size, candidate.total_size)
    return {
        "no_hard_conflict": not score.rejected,
        "external_id": external_id_ok,
        "title": title_exact,
        "year": year_ok,
        "episode": episode_ok,
        "resolution": resolution_ok,
        "source_medium": source_medium_ok,
        "release_group": release_group_ok,
        "size": size_ok,
    }


def _size_exact(left: int | None, right: int | None) -> bool:
    if left is None or right is None:
        return False
    denominator = max(left, right)
    if denominator == 0:
        return left == right
    return abs(left - right) / denominator <= EXACT_SIZE_TOLERANCE_RATIO


def _media_type(kind: TaskUnitKind) -> SearchMediaType:
    if kind is TaskUnitKind.EPISODE:
        return SearchMediaType.TV
    if kind is TaskUnitKind.MOVIE:
        return SearchMediaType.MOVIE
    return SearchMediaType.UNKNOWN


def _episode_token(descriptor: MediaDescriptor) -> str | None:
    episode = descriptor.episode
    if episode is None:
        return None
    season = episode.season
    start = episode.start
    end = episode.end
    if season is not None and start is not None and end is not None and start != end:
        return f"s{season:02d}e{start:02d}-e{end:02d}"
    if season is not None and start is not None:
        return f"s{season:02d}e{start:02d}"
    if start is not None:
        return f"ep{start:02d}"
    return None


def _append_query(queries: list[SearchQuery], query: SearchQuery) -> None:
    if query not in queries:
        queries.append(query)
