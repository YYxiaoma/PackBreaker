from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum

from backend.app.domain.media_matching import (
    EpisodeIdentity,
    EpisodeKind,
    ExternalMediaId,
    MediaDescriptor,
    MediaFileSummary,
    normalize_text,
    parse_media_name,
)
from backend.app.domain.task_units import TaskUnit, TaskUnitKind


class SearchMediaType(StrEnum):
    MOVIE = "MOVIE"
    TV = "TV"
    UNKNOWN = "UNKNOWN"


class SearchSortHint(StrEnum):
    RELEVANCE = "RELEVANCE"
    SEEDERS = "SEEDERS"
    NEWEST = "NEWEST"


@dataclass(frozen=True, slots=True)
class SearchQuery:
    keywords: tuple[str, ...]
    media_type: SearchMediaType
    episode: EpisodeIdentity | None = None
    external_ids: tuple[ExternalMediaId, ...] = ()
    page: int = 1
    page_size: int = 50
    sort: SearchSortHint = SearchSortHint.RELEVANCE

    def __post_init__(self) -> None:
        normalized_keywords = tuple(
            token for value in self.keywords if (token := normalize_text(value))
        )
        if len(normalized_keywords) > 16:
            raise ValueError("搜索关键词最多允许 16 个 token")
        if not normalized_keywords and not self.external_ids:
            raise ValueError("搜索查询必须包含关键词或外部媒体 ID")
        if self.page < 1 or not 1 <= self.page_size <= 100:
            raise ValueError("搜索分页参数超出允许范围")
        object.__setattr__(self, "keywords", normalized_keywords)
        object.__setattr__(self, "external_ids", _normalize_external_ids(self.external_ids))

    @property
    def query_text(self) -> str:
        """返回未 URL 编码的站点无关关键词；编码责任属于具体适配器。"""

        return " ".join(self.keywords)


@dataclass(frozen=True, slots=True)
class SiteSearchCapabilities:
    supports_imdb_id: bool = False
    supports_douban_id: bool = False
    supports_exact_phrase: bool = False
    supports_category: bool = False
    supports_pagination: bool = True
    supports_detail_file_list: bool = False
    requires_download_token: bool = False
    min_request_interval_seconds: float = 0.0

    def __post_init__(self) -> None:
        if self.min_request_interval_seconds < 0:
            raise ValueError("站点最小请求间隔不能为负数")


@dataclass(frozen=True, slots=True)
class CandidateMeta:
    site_id: str
    torrent_id: str
    display_name: str
    descriptor: MediaDescriptor
    total_size: int | None = None
    published_at: datetime | None = None
    category: str | None = None
    seeders: int | None = None
    leechers: int | None = None
    file_summary: tuple[MediaFileSummary, ...] = ()

    def __post_init__(self) -> None:
        if not self.site_id.strip() or not self.torrent_id.strip() or not self.display_name.strip():
            raise ValueError("候选必须包含站点 ID、远程 torrent ID 和标题")
        for value in (self.total_size, self.seeders, self.leechers):
            if value is not None and value < 0:
                raise ValueError("候选数值字段不能为负数")
        if self.published_at is not None:
            if self.published_at.tzinfo is None or self.published_at.utcoffset() is None:
                raise ValueError("候选发布时间必须带时区")
            object.__setattr__(self, "published_at", self.published_at.astimezone(UTC))

    @property
    def identity(self) -> tuple[str, str]:
        return (self.site_id, self.torrent_id)


@dataclass(frozen=True, slots=True)
class SearchPage:
    site_id: str
    page: int
    items: tuple[CandidateMeta, ...]
    has_more: bool
    total_hint: int | None = None

    def __post_init__(self) -> None:
        if not self.site_id.strip() or self.page < 1:
            raise ValueError("搜索页必须包含有效站点 ID 和页码")
        if self.total_hint is not None and self.total_hint < 0:
            raise ValueError("搜索总数提示不能为负数")
        identities: set[tuple[str, str]] = set()
        for item in self.items:
            if item.site_id != self.site_id:
                raise ValueError("搜索页不能混入其他站点候选")
            if item.identity in identities:
                raise ValueError("搜索页包含重复远程候选")
            identities.add(item.identity)


def build_search_queries(unit: TaskUnit, *, max_queries: int = 3) -> tuple[SearchQuery, ...]:
    if not 1 <= max_queries <= 8:
        raise ValueError("max_queries 必须位于 1..8")
    descriptor = unit.descriptor
    media_type = _media_type(unit.kind)
    base = list(descriptor.title_tokens)
    structured = _structured_tokens(descriptor)
    variants: list[SearchQuery] = []

    if base or descriptor.external_ids:
        _append_query(
            variants,
            SearchQuery(
                tuple((*base, *structured)),
                media_type,
                descriptor.episode,
                descriptor.external_ids,
            ),
        )
    if base:
        _append_query(
            variants,
            SearchQuery(tuple((*base, *structured)), media_type, descriptor.episode),
        )
        _append_query(variants, SearchQuery(tuple(base), media_type, descriptor.episode))
    return tuple(variants[:max_queries])


def normalize_candidate_meta(
    *,
    site_id: str,
    torrent_id: str,
    display_name: str,
    total_size: int | None = None,
    published_at: datetime | None = None,
    category: str | None = None,
    seeders: int | None = None,
    leechers: int | None = None,
    external_ids: tuple[ExternalMediaId, ...] = (),
    file_summary: tuple[MediaFileSummary, ...] = (),
    release_group: str | None = None,
    language: str | None = None,
) -> CandidateMeta:
    descriptor = parse_media_name(
        display_name,
        release_group=release_group,
        language=language,
        total_size=total_size,
        files=file_summary,
    )
    merged_ids = _normalize_external_ids((*descriptor.external_ids, *external_ids))
    descriptor = replace(descriptor, external_ids=merged_ids)
    return CandidateMeta(
        site_id=site_id,
        torrent_id=torrent_id,
        display_name=display_name,
        descriptor=descriptor,
        total_size=total_size,
        published_at=published_at,
        category=category,
        seeders=seeders,
        leechers=leechers,
        file_summary=file_summary,
    )


def _structured_tokens(descriptor: MediaDescriptor) -> tuple[str, ...]:
    result: list[str] = []
    if descriptor.year is not None:
        result.append(str(descriptor.year))
    if descriptor.episode is not None:
        result.append(_episode_token(descriptor.episode))
    return tuple(result)


def _episode_token(episode: EpisodeIdentity) -> str:
    if episode.kind is EpisodeKind.SEASON_RANGE:
        if episode.season is None or episode.start is None or episode.end is None:
            raise ValueError("范围集标识缺少 season/start/end")
        return f"s{episode.season:02d}e{episode.start:02d}-e{episode.end:02d}"
    if episode.kind is EpisodeKind.SEASON_EPISODE:
        if episode.season is None or episode.start is None:
            raise ValueError("季集标识缺少 season/start")
        return f"s{episode.season:02d}e{episode.start:02d}"
    if episode.kind is EpisodeKind.SPECIALS:
        if episode.season is None and episode.start is None and episode.end is None:
            return "specials"
        if episode.season is None or episode.start is None:
            raise ValueError("Specials 标识字段不完整")
        return f"s{episode.season:02d}e{episode.start:02d}"
    if episode.kind is EpisodeKind.EPISODE:
        if episode.start is None:
            raise ValueError("EP 标识缺少 episode number")
        return f"ep{episode.start:02d}"
    if episode.kind is EpisodeKind.ABSOLUTE:
        if episode.start is None:
            raise ValueError("绝对集标识缺少 episode number")
        return f"abs{episode.start:02d}"
    raise ValueError("未知季集标识类型")


def _media_type(kind: TaskUnitKind) -> SearchMediaType:
    if kind is TaskUnitKind.EPISODE:
        return SearchMediaType.TV
    if kind is TaskUnitKind.MOVIE:
        return SearchMediaType.MOVIE
    return SearchMediaType.UNKNOWN


def _normalize_external_ids(values: tuple[ExternalMediaId, ...]) -> tuple[ExternalMediaId, ...]:
    by_namespace: dict[str, ExternalMediaId] = {}
    for item in values:
        namespace = normalize_text(item.namespace)
        value = normalize_text(item.value)
        if not namespace or not value:
            raise ValueError("外部媒体 ID 的 namespace/value 不能为空")
        existing = by_namespace.get(namespace)
        if existing is not None and existing.value != value:
            raise ValueError("同一外部 ID namespace 出现冲突值")
        by_namespace[namespace] = ExternalMediaId(namespace, value)
    return tuple(sorted(by_namespace.values(), key=lambda item: (item.namespace, item.value)))


def _append_query(target: list[SearchQuery], query: SearchQuery) -> None:
    identity = (query.keywords, query.media_type, query.episode, query.external_ids)
    if all(
        (item.keywords, item.media_type, item.episode, item.external_ids) != identity
        for item in target
    ):
        target.append(query)
