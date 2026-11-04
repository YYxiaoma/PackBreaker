from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from backend.app.domain.candidate_scoring import CandidateScore, score_candidate
from backend.app.domain.media_matching import EpisodeIdentity, MediaDescriptor, normalize_text
from backend.app.domain.site_search import (
    SearchMediaType,
    SearchQuery,
    SiteSearchCapabilities,
)
from backend.app.domain.task_units import TaskUnitKind

EXACT_SIZE_TOLERANCE_RATIO = 0.001

_QUERY_STOP_WORDS = frozenset(
    {"a", "an", "and", "at", "by", "for", "from", "in", "of", "on", "the", "to", "with"}
)
_QUERY_NOISE_TOKENS = frozenset(
    {
        "1080p",
        "1080i",
        "720p",
        "2160p",
        "4k",
        "uhd",
        "bluray",
        "webrip",
        "webdl",
        "hdtv",
        "remastered",
        "restored",
        "extended",
        "unrated",
        "theatrical",
        "limited",
        "proper",
        "repack",
        "internal",
        "hybrid",
        "muhd",
        "mnhd",
        "frds",
        "aac",
        "flac",
        "ac3",
        "eac3",
        "ddp",
        "dts",
        "truehd",
        "atmos",
        "hdr",
        "hdr10",
        "dovi",
        "dolby",
        "vision",
        "x265",
        "x264",
        "h265",
        "h264",
        "hevc",
        "avc",
        "audio",
        "audios",
        "2audio",
        "3audio",
        "4audio",
        "5audio",
        "2audios",
        "3audios",
        "4audios",
        "5audios",
        "mkv",
        "mp4",
        "m2ts",
        "avi",
        "mov",
        "aac2",
        "greenotea",
    }
)
_CJK_QUERY_NOISE_TOKENS = frozenset(
    {
        "修复版",
        "修复",
        "重制版",
        "重制",
        "高清",
        "高清版",
        "蓝光",
        "原盘",
        "中字",
        "字幕",
        "国语",
        "粤语",
        "国粤双语",
        "完整版",
        "未删减",
        "导演剪辑版",
        "加长版",
        "典藏版",
        "纪念版",
        "合集",
        "源码",
        "翡翠台源码",
        "港版",
        "台版",
        "美版",
        "英版",
        "日版",
        "中英字幕",
        "特效字幕",
        "4k修复版",
        "双语",
        "简繁中字",
    }
)
_ROMAN_NUMERAL_VALUES = {
    "I": "1",
    "II": "2",
    "III": "3",
    "IV": "4",
    "V": "5",
    "VI": "6",
    "VII": "7",
    "VIII": "8",
    "IX": "9",
    "X": "10",
}


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
    max_queries: int = 8,
) -> tuple[SearchQuery, ...]:
    """Build diverse title/alias queries without letting IDs crowd out fallbacks."""
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

    if not title_tokens:
        return tuple(queries[:max_queries])

    ascii_aliases, cjk_aliases = _query_aliases(source)
    clean_title = _clean_query_tokens(title_tokens) or title_tokens

    # Keep the legacy bare one-word search first on rate-limited trackers. Some
    # sites return no results for a single-word title when a year is appended.
    if (
        capabilities.min_request_interval_seconds > 0
        and len(title_tokens) == 1
        and not any(alias != title_tokens for alias in source.alias_tokens)
    ):
        _append_title_query(
            queries,
            _clean_query_tokens(title_tokens) or title_tokens,
            media_type=media_type,
            episode=source.episode,
        )

    # First pass: each independently useful title alias without release metadata.
    # Single-word English titles get their year first to reduce broad false hits.
    for alias in ascii_aliases:
        if len(alias) == 1 and structured:
            _append_title_query(
                queries, alias, media_type=media_type, episode=source.episode, structured=structured
            )
        else:
            _append_title_query(queries, alias, media_type=media_type, episode=source.episode)
    for alias in cjk_aliases:
        _append_title_query(queries, alias, media_type=media_type, episode=source.episode)

    known_cjk = {token for alias in cjk_aliases for token in alias}
    extra_cjk_titles = tuple(
        dict.fromkeys(
            token
            for token in clean_title
            if _contains_cjk(token)
            and token not in _CJK_QUERY_NOISE_TOKENS
            and token not in known_cjk
        )
    )
    for token in extra_cjk_titles:
        _append_title_query(queries, (token,), media_type=media_type, episode=source.episode)

    # Second pass: alias+year/episode and compact stop-word-free variants.
    # This catches sites whose search treats every token as mandatory.
    for alias in ascii_aliases:
        if len(alias) == 1 and structured:
            _append_title_query(queries, alias, media_type=media_type, episode=source.episode)
        else:
            _append_title_query(
                queries, alias, media_type=media_type, episode=source.episode, structured=structured
            )
        compact_alias = _remove_stop_words(alias)
        if compact_alias and compact_alias != alias:
            _append_title_query(
                queries,
                compact_alias,
                media_type=media_type,
                episode=source.episode,
                structured=structured,
            )
            _append_title_query(
                queries, compact_alias, media_type=media_type, episode=source.episode
            )

    for alias in cjk_aliases:
        _append_title_query(
            queries, alias, media_type=media_type, episode=source.episode, structured=structured
        )
    for token in extra_cjk_titles:
        _append_title_query(
            queries, (token,), media_type=media_type, episode=source.episode, structured=structured
        )

    # Trackers index sequels inconsistently (e.g. "Rocky II" vs "Rocky 2",
    # or "Saw X" vs "Saw 10"). Add a search-only numeric spelling; scoring and
    # hard-conflict checks still use the original media identity.
    roman_sources = (*ascii_aliases, clean_title)
    for alias in roman_sources:
        numeric_alias = _roman_numeral_variant(alias)
        if numeric_alias is None:
            continue
        _append_title_query(queries, numeric_alias, media_type=media_type, episode=source.episode)
        _append_title_query(
            queries,
            numeric_alias,
            media_type=media_type,
            episode=source.episode,
            structured=structured,
        )

    # A trailing sequel number is often absent from tracker titles (e.g.
    # "Mad Max 2" is indexed as "Mad Max / The Road Warrior"). Search without
    # that number as a fallback, but keep the source year for conflict checking.
    for alias in (*ascii_aliases, clean_title):
        sequel_base = _numeric_sequel_base(alias)
        if sequel_base is None:
            continue
        _append_title_query(
            queries,
            sequel_base,
            media_type=media_type,
            episode=source.episode,
            structured=structured,
        )
        _append_title_query(queries, sequel_base, media_type=media_type, episode=source.episode)

    core_title = _remove_stop_words(clean_title)
    if core_title:
        _append_title_query(
            queries,
            core_title,
            media_type=media_type,
            episode=source.episode,
            structured=structured,
        )
    _append_title_query(
        queries, clean_title, media_type=media_type, episode=source.episode, structured=structured
    )
    if core_title:
        _append_title_query(queries, core_title, media_type=media_type, episode=source.episode)
    _append_title_query(queries, clean_title, media_type=media_type, episode=source.episode)
    return tuple(queries[:max_queries])


def assess_unpack_candidate(
    source: MediaDescriptor,
    candidate: MediaDescriptor,
) -> UnpackCandidateAssessment:
    score = score_candidate(source, candidate)
    score_bps = max(0, min(10_000, round(score.score * 100)))
    exact_evidence = _exact_evidence(source, candidate, score)
    available_weight = sum(
        item.maximum if item.coverage_maximum is None else item.coverage_maximum
        for item in score.dimensions
    )
    evidence_coverage_bps = round(available_weight * 100)
    return UnpackCandidateAssessment(
        score_bps=score_bps,
        is_exact_match=all(value is not False for value in exact_evidence.values())
        and not score.rejected,
        evidence={
            "algorithm_version": score.algorithm_version,
            "config_version": score.config_version,
            "evidence_coverage_bps": evidence_coverage_bps,
            "hard_conflicts": [item.value for item in score.hard_conflicts],
            "dimensions": [
                {
                    "dimension": item.dimension.value,
                    "earned": item.earned,
                    "maximum": item.maximum,
                    "coverage_maximum": (
                        item.maximum if item.coverage_maximum is None else item.coverage_maximum
                    ),
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
) -> dict[str, bool | None]:
    source_titles = (source.title_tokens, *source.alias_tokens)
    candidate_titles = (candidate.title_tokens, *candidate.alias_tokens)
    title_exact = any(
        left and right and left == right for left in source_titles for right in candidate_titles
    )

    source_ids = {(item.namespace, item.value) for item in source.external_ids}
    candidate_ids = {(item.namespace, item.value) for item in candidate.external_ids}
    external_id_ok: bool | None = None if not source_ids else bool(source_ids & candidate_ids)

    year_ok: bool | None = None if source.year is None else candidate.year == source.year
    if source.episode is None and candidate.episode is None:
        episode_ok: bool | None = None
    elif source.episode is None or candidate.episode is None:
        episode_ok = False
    else:
        episode_ok = source.episode == candidate.episode
    resolution_ok: bool | None = (
        None if source.resolution is None else candidate.resolution == source.resolution
    )
    source_medium_ok: bool | None = (
        None if source.release_source is None else candidate.release_source == source.release_source
    )
    codec_ok: bool | None = None if source.codec is None else candidate.codec == source.codec
    hdr_ok: bool | None = None if source.hdr is None else candidate.hdr == source.hdr
    audio_ok: bool | None = None if source.audio is None else candidate.audio == source.audio
    release_group_ok: bool | None = (
        None
        if source.release_group is None
        else candidate.release_group is not None
        and normalize_text(candidate.release_group) == normalize_text(source.release_group)
    )
    size_ok: bool | None = (
        None if source.total_size is None else _size_exact(source.total_size, candidate.total_size)
    )
    return {
        "no_hard_conflict": not score.rejected,
        "external_id": external_id_ok,
        "title": title_exact,
        "year": year_ok,
        "episode": episode_ok,
        "resolution": resolution_ok,
        "source_medium": source_medium_ok,
        "codec": codec_ok,
        "hdr": hdr_ok,
        "audio": audio_ok,
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


def _query_aliases(
    source: MediaDescriptor,
) -> tuple[tuple[tuple[str, ...], ...], tuple[tuple[str, ...], ...]]:
    ascii_aliases: list[tuple[str, ...]] = []
    cjk_aliases: list[tuple[str, ...]] = []
    for alias in source.alias_tokens:
        cleaned = _clean_query_tokens(alias)
        if not cleaned:
            continue
        ascii_tokens = tuple(token for token in cleaned if token.isascii())
        cjk_tokens = tuple(
            token
            for token in cleaned
            if _contains_cjk(token) and token not in _CJK_QUERY_NOISE_TOKENS
        )
        if ascii_tokens:
            _append_tokens(ascii_aliases, ascii_tokens)
        if cjk_tokens:
            if len(cjk_tokens) > 1:
                _append_tokens(cjk_aliases, cjk_tokens)
            for token in cjk_tokens:
                _append_tokens(cjk_aliases, (token,))
    return tuple(ascii_aliases), tuple(cjk_aliases)


def _clean_query_tokens(tokens: tuple[str, ...]) -> tuple[str, ...]:
    normalized = [normalize_text(raw) for raw in tokens]
    cleaned: list[str] = []
    for token in normalized:
        if not token:
            continue
        if token in _QUERY_NOISE_TOKENS or (token.startswith("top") and token[3:].isdigit()):
            continue
        if _contains_cjk(token) and token in _CJK_QUERY_NOISE_TOKENS:
            continue
        cleaned.append(token)
    # Release parsers can leave channel counts (5.1 / 7.1) inside the title
    # tokens, before a trailing audio-track marker. Remove them after other
    # release-noise tokens have been discarded, without dropping sequel numbers.
    if len(cleaned) >= 3 and tuple(cleaned[-2:]) in {("5", "1"), ("7", "1"), ("2", "0")}:
        cleaned = cleaned[:-2]
    return tuple(cleaned[:16])


def _contains_cjk(token: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in token)


def _remove_stop_words(tokens: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(token for token in tokens if token not in _QUERY_STOP_WORDS)


def _roman_numeral_variant(tokens: tuple[str, ...]) -> tuple[str, ...] | None:
    if len(tokens) < 2 or not tokens[-1].isascii():
        return None
    number = _ROMAN_NUMERAL_VALUES.get(tokens[-1].upper())
    if number is None:
        return None
    return (*tokens[:-1], number)


def _numeric_sequel_base(tokens: tuple[str, ...]) -> tuple[str, ...] | None:
    if len(tokens) < 2 or not tokens[-1].isascii() or not tokens[-1].isdigit():
        return None
    number = int(tokens[-1])
    if not 1 <= number <= 20 or tokens[-2] in {
        "part",
        "chapter",
        "volume",
        "vol",
        "episode",
        "season",
        "disc",
        "disk",
    }:
        return None
    return tokens[:-1]


def _append_tokens(target: list[tuple[str, ...]], value: tuple[str, ...]) -> None:
    if value and value not in target:
        target.append(value)


def _append_title_query(
    queries: list[SearchQuery],
    tokens: tuple[str, ...],
    *,
    media_type: SearchMediaType,
    episode: EpisodeIdentity | None,
    structured: tuple[str, ...] = (),
) -> None:
    if not tokens:
        return
    if structured:
        prefix = tokens[: max(0, 16 - len(structured))]
        keywords = (*prefix, *structured)
    else:
        keywords = tokens[:16]
    if keywords:
        _append_query(
            queries,
            SearchQuery(keywords=keywords, media_type=media_type, episode=episode),
        )


def _append_query(queries: list[SearchQuery], query: SearchQuery) -> None:
    if query not in queries:
        queries.append(query)
