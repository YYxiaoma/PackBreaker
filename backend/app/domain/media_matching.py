from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath


class EpisodeKind(StrEnum):
    SEASON_EPISODE = "SEASON_EPISODE"
    SEASON_RANGE = "SEASON_RANGE"
    EPISODE = "EPISODE"
    ABSOLUTE = "ABSOLUTE"
    SPECIALS = "SPECIALS"


@dataclass(frozen=True, slots=True)
class EpisodeIdentity:
    kind: EpisodeKind
    season: int | None = None
    start: int | None = None
    end: int | None = None


@dataclass(frozen=True, slots=True)
class ExternalMediaId:
    namespace: str
    value: str


@dataclass(frozen=True, slots=True)
class MediaFileSummary:
    basename: str
    length: int

    def __post_init__(self) -> None:
        if self.length < 0:
            raise ValueError("媒体文件长度不能为负数")

    @property
    def extension(self) -> str:
        return PurePosixPath(self.basename).suffix.casefold()


@dataclass(frozen=True, slots=True)
class MediaDescriptor:
    raw_name: str
    title_tokens: tuple[str, ...]
    alias_tokens: tuple[tuple[str, ...], ...] = ()
    year: int | None = None
    episode: EpisodeIdentity | None = None
    resolution: str | None = None
    release_source: str | None = None
    codec: str | None = None
    hdr: str | None = None
    audio: str | None = None
    language: str | None = None
    release_group: str | None = None
    version: str | None = None
    external_ids: tuple[ExternalMediaId, ...] = ()
    total_size: int | None = None
    files: tuple[MediaFileSummary, ...] = ()


_TOKEN_RE = re.compile(r"[^\W_]+", flags=re.UNICODE)
_YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)", flags=re.IGNORECASE)
_RANGE_RE = re.compile(
    r"(?<![a-z0-9])s(\d{1,2})[ ._-]*e(\d{1,3})[ ._-]*(?:-|~|to)[ ._-]*e?(\d{1,3})(?!\d)",
    flags=re.IGNORECASE,
)
_SEASON_EP_RE = re.compile(r"(?<![a-z0-9])s(\d{1,2})[ ._-]*e(\d{1,3})(?!\d)", re.IGNORECASE)
_EP_RE = re.compile(r"(?<![a-z0-9])ep[ ._-]*(\d{1,4})(?!\d)", re.IGNORECASE)
_ABS_RE = re.compile(r"(?<![a-z0-9])abs(?:olute)?[ ._-]*(\d{1,4})(?!\d)", re.IGNORECASE)
_SPECIALS_RE = re.compile(r"(?<![a-z0-9])specials?(?![a-z0-9])", re.IGNORECASE)
_IMDB_RE = re.compile(r"(?<![a-z0-9])(tt\d{5,10})(?!\d)", re.IGNORECASE)
_DOUBAN_RE = re.compile(r"(?:douban|豆瓣)[ :._-]*(\d{3,12})", re.IGNORECASE)

_KNOWN_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("resolution", re.compile(r"(?<![a-z0-9])(?:2160p|4k)(?![a-z0-9])", re.I), "2160p"),
    ("resolution", re.compile(r"(?<![a-z0-9])1080[pi](?![a-z0-9])", re.I), "1080p"),
    ("resolution", re.compile(r"(?<![a-z0-9])720p(?![a-z0-9])", re.I), "720p"),
    ("release_source", re.compile(r"(?<![a-z0-9])web[ ._-]*dl(?![a-z0-9])", re.I), "web-dl"),
    ("release_source", re.compile(r"(?<![a-z0-9])web[ ._-]*rip(?![a-z0-9])", re.I), "webrip"),
    ("release_source", re.compile(r"(?<![a-z0-9])blu[ ._-]*ray(?![a-z0-9])", re.I), "bluray"),
    ("release_source", re.compile(r"(?<![a-z0-9])remux(?![a-z0-9])", re.I), "remux"),
    ("release_source", re.compile(r"(?<![a-z0-9])hdtv(?![a-z0-9])", re.I), "hdtv"),
    ("codec", re.compile(r"(?<![a-z0-9])(?:x265|h[ ._-]*265|hevc)(?![a-z0-9])", re.I), "hevc"),
    ("codec", re.compile(r"(?<![a-z0-9])(?:x264|h[ ._-]*264|avc)(?![a-z0-9])", re.I), "avc"),
    ("codec", re.compile(r"(?<![a-z0-9])av1(?![a-z0-9])", re.I), "av1"),
    (
        "hdr",
        re.compile(r"(?<![a-z0-9])(?:dolby[ ._-]*vision|dovi)(?![a-z0-9])", re.I),
        "dolby-vision",
    ),
    ("hdr", re.compile(r"(?<![a-z0-9])hdr10[ ._-]*\+(?![a-z0-9])", re.I), "hdr10+"),
    ("hdr", re.compile(r"(?<![a-z0-9])hdr10(?![a-z0-9])", re.I), "hdr10"),
    ("hdr", re.compile(r"(?<![a-z0-9])hdr(?![a-z0-9])", re.I), "hdr"),
    ("audio", re.compile(r"(?<![a-z0-9])atmos(?![a-z0-9])", re.I), "atmos"),
    ("audio", re.compile(r"(?<![a-z0-9])true[ ._-]*hd(?![a-z0-9])", re.I), "truehd"),
    ("audio", re.compile(r"(?<![a-z0-9])dts[ ._-]*hd(?:[ ._-]*ma)?(?![a-z0-9])", re.I), "dts-hd"),
    ("audio", re.compile(r"(?<![a-z0-9])(?:ddp|eac3)(?![a-z0-9])", re.I), "eac3"),
    ("language", re.compile(r"(?<![a-z0-9])(?:chs|zh[ ._-]*cn)(?![a-z0-9])", re.I), "zh-cn"),
    ("language", re.compile(r"(?<![a-z0-9])(?:cht|zh[ ._-]*tw)(?![a-z0-9])", re.I), "zh-tw"),
    ("language", re.compile(r"(?<![a-z0-9])(?:eng|english)(?![a-z0-9])", re.I), "en"),
    ("language", re.compile(r"(?<![a-z0-9])(?:jpn|japanese)(?![a-z0-9])", re.I), "ja"),
    ("language", re.compile(r"(?<![a-z0-9])(?:kor|korean)(?![a-z0-9])", re.I), "ko"),
    ("version", re.compile(r"(?<![a-z0-9])repack(?![a-z0-9])", re.I), "repack"),
    ("version", re.compile(r"(?<![a-z0-9])proper(?![a-z0-9])", re.I), "proper"),
)


def normalize_text(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold().strip()


def tokenize_title(value: str) -> tuple[str, ...]:
    return tuple(_TOKEN_RE.findall(normalize_text(value)))


def media_file_token_signature(filename: str) -> tuple[str, ...]:
    """生成保守的文件名 token 序列；不按连字符截断标题。"""

    name = PurePosixPath(filename).name
    suffix = PurePosixPath(name).suffix
    stem = name[: -len(suffix)] if suffix else name
    normalized = normalize_text(stem)
    for _, pattern, canonical in _KNOWN_PATTERNS:
        replacement = canonical.replace("-", "").replace("+", "plus")
        normalized = pattern.sub(f" {replacement} ", normalized)
    return tuple(_TOKEN_RE.findall(normalized))


def parse_media_name(
    raw_name: str,
    *,
    aliases: tuple[str, ...] = (),
    release_group: str | None = None,
    language: str | None = None,
    total_size: int | None = None,
    files: tuple[MediaFileSummary, ...] = (),
) -> MediaDescriptor:
    """从发布名提取明确 token；不猜测裸集数，也不按任意分隔符截断标题。"""

    working = normalize_text(raw_name)
    values: dict[str, str] = {}
    external_ids: list[ExternalMediaId] = []

    imdb = _IMDB_RE.search(working)
    if imdb is not None:
        external_ids.append(ExternalMediaId("imdb", imdb.group(1).casefold()))
        working = _blank_span(working, imdb.span())
    douban = _DOUBAN_RE.search(working)
    if douban is not None:
        external_ids.append(ExternalMediaId("douban", douban.group(1)))
        working = _blank_span(working, douban.span())

    episode, episode_span = _extract_episode(working)
    if episode_span is not None:
        working = _blank_span(working, episode_span)

    year_match = _YEAR_RE.search(working)
    year = int(year_match.group(1)) if year_match is not None else None
    if year_match is not None:
        working = _blank_span(working, year_match.span())

    for field, pattern, canonical in _KNOWN_PATTERNS:
        match = pattern.search(working)
        if match is None or field in values:
            continue
        values[field] = canonical
        working = _blank_span(working, match.span())

    alias_tokens = tuple(tokens for alias in aliases if (tokens := tokenize_title(alias)))
    return MediaDescriptor(
        raw_name=raw_name,
        title_tokens=tuple(_TOKEN_RE.findall(working)),
        alias_tokens=alias_tokens,
        year=year,
        episode=episode,
        resolution=values.get("resolution"),
        release_source=values.get("release_source"),
        codec=values.get("codec"),
        hdr=values.get("hdr"),
        audio=values.get("audio"),
        language=normalize_text(language) if language else values.get("language"),
        release_group=normalize_text(release_group) if release_group else None,
        version=values.get("version"),
        external_ids=tuple(sorted(external_ids, key=lambda item: (item.namespace, item.value))),
        total_size=total_size,
        files=files,
    )


def _extract_episode(value: str) -> tuple[EpisodeIdentity | None, tuple[int, int] | None]:
    match = _RANGE_RE.search(value)
    if match is not None:
        season, start, end = (int(item) for item in match.groups())
        kind = EpisodeKind.SPECIALS if season == 0 else EpisodeKind.SEASON_RANGE
        return EpisodeIdentity(kind, season, start, end), match.span()
    match = _SEASON_EP_RE.search(value)
    if match is not None:
        season, episode = (int(item) for item in match.groups())
        kind = EpisodeKind.SPECIALS if season == 0 else EpisodeKind.SEASON_EPISODE
        return EpisodeIdentity(kind, season, episode, episode), match.span()
    match = _EP_RE.search(value)
    if match is not None:
        episode = int(match.group(1))
        return EpisodeIdentity(EpisodeKind.EPISODE, None, episode, episode), match.span()
    match = _ABS_RE.search(value)
    if match is not None:
        episode = int(match.group(1))
        return EpisodeIdentity(EpisodeKind.ABSOLUTE, None, episode, episode), match.span()
    match = _SPECIALS_RE.search(value)
    if match is not None:
        return EpisodeIdentity(EpisodeKind.SPECIALS), match.span()
    return None, None


def _blank_span(value: str, span: tuple[int, int]) -> str:
    return value[: span[0]] + " " * (span[1] - span[0]) + value[span[1] :]
