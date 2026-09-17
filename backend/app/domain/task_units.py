from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, replace
from enum import StrEnum
from hashlib import sha256
from pathlib import PurePosixPath

from backend.app.domain.errors import DomainViolation, ErrorCode
from backend.app.domain.media_matching import (
    EpisodeIdentity,
    EpisodeKind,
    MediaDescriptor,
    MediaFileSummary,
    parse_media_name,
)

_UNIT_KEY_VERSION = "packbreaker-task-unit-v1"
_EPISODE_GROUP_KEY_VERSION = "packbreaker-episode-group-v1"
_EPISODE_VARIANT_KEY_VERSION = "packbreaker-episode-variant-v1"
_WINDOWS_DRIVE_RE = re.compile(r"^[a-zA-Z]:")
_VIDEO_EXTENSIONS = frozenset({".avi", ".m4v", ".mkv", ".mov", ".mp4", ".m2ts", ".ts", ".wmv"})
_DISC_STRUCTURE_COMPONENTS = frozenset({"bdmv", "video_ts"})
_SEASON_COMPONENT_RE = re.compile(r"^(?:season[ ._-]*|s)(\d{1,2})$", re.IGNORECASE)
_SPECIALS_COMPONENT_RE = re.compile(r"^specials?$", re.IGNORECASE)
_CONTEXT_EPISODE_RANGE_RE = re.compile(
    r"(?<![a-z0-9])(?:e|ep|episode)[ ._-]*(\d{1,3})[ ._-]*(?:-|~|to)[ ._-]*"
    r"(?:e|ep|episode)?[ ._-]*(\d{1,3})(?!\d)",
    re.IGNORECASE,
)
_CONTEXT_EPISODE_SINGLE_RE = re.compile(
    r"(?<![a-z0-9])(?:e|ep|episode)[ ._-]*(\d{1,3})(?!\d)",
    re.IGNORECASE,
)
_CONTEXT_BARE_RANGE_RE = re.compile(
    r"^(\d{1,3})[ ._-]*(?:-|~|to)[ ._-]*(\d{1,3})(?:[ ._-]|$)", re.IGNORECASE
)
_CONTEXT_BARE_SINGLE_RE = re.compile(r"^(\d{1,3})(?:[ ._-]|$)", re.IGNORECASE)


class TaskUnitKind(StrEnum):
    MOVIE = "MOVIE"
    EPISODE = "EPISODE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class SourceTaskFile:
    relative_path: str
    length: int

    def __post_init__(self) -> None:
        if self.length < 0:
            raise DomainViolation(ErrorCode.SOURCE_UNIT_INVALID, "源文件长度不能为负数")
        object.__setattr__(
            self, "relative_path", normalize_source_relative_path(self.relative_path)
        )


@dataclass(frozen=True, slots=True)
class TaskUnit:
    normalized_unit_key: str
    kind: TaskUnitKind
    source_relative_path: str
    length: int
    descriptor: MediaDescriptor


@dataclass(frozen=True, slots=True)
class EpisodeUnitMetadata:
    kind: EpisodeKind
    season: int | None
    start: int | None
    end: int | None
    label: str
    group_key: str
    variant_key: str


def identify_task_units(
    files: tuple[SourceTaskFile, ...],
    *,
    episode_context: str | None = None,
) -> tuple[TaskUnit, ...]:
    """从文件型媒体包中确定性识别处理单元；不读取文件内容，也不猜测光盘目录结构。"""

    by_path: dict[str, SourceTaskFile] = {}
    for source_file in files:
        key = source_file.relative_path
        if key in by_path:
            raise DomainViolation(ErrorCode.SOURCE_UNIT_INVALID, "源文件清单存在重复逻辑路径")
        by_path[key] = source_file

    units: list[TaskUnit] = []
    for source_file in sorted(
        by_path.values(), key=lambda item: (item.relative_path.casefold(), item.relative_path)
    ):
        path = PurePosixPath(source_file.relative_path)
        if (
            path.suffix.casefold() not in _VIDEO_EXTENSIONS
            or source_file.length == 0
            or any(part.casefold() in _DISC_STRUCTURE_COMPONENTS for part in path.parts[:-1])
        ):
            continue
        display_name = path.stem
        descriptor = parse_media_name(
            display_name,
            total_size=source_file.length,
            files=(MediaFileSummary(path.name, source_file.length),),
        )
        if episode_context is not None:
            descriptor = _apply_episode_directory_context(
                descriptor,
                display_name=display_name,
                episode_context=episode_context,
            )
        kind = _classify_unit(descriptor)
        units.append(
            TaskUnit(
                normalized_unit_key=_unit_key(source_file),
                kind=kind,
                source_relative_path=source_file.relative_path,
                length=source_file.length,
                descriptor=descriptor,
            )
        )
    return tuple(units)


def episode_unit_metadata(unit: TaskUnit) -> EpisodeUnitMetadata | None:
    episode = unit.descriptor.episode
    if unit.kind is not TaskUnitKind.EPISODE or episode is None:
        return None
    label = _episode_label(episode)
    title = " ".join(unit.descriptor.title_tokens)
    group_payload = "\x1f".join(
        (
            _EPISODE_GROUP_KEY_VERSION,
            title,
            str(unit.descriptor.year or ""),
            episode.kind.value,
            str(episode.season if episode.season is not None else ""),
            str(episode.start if episode.start is not None else ""),
            str(episode.end if episode.end is not None else ""),
        )
    ).encode()
    group_key = sha256(group_payload).hexdigest()
    variant_payload = "\x1f".join(
        (
            _EPISODE_VARIANT_KEY_VERSION,
            group_key,
            unit.normalized_unit_key,
            unit.descriptor.resolution or "",
            unit.descriptor.release_source or "",
            unit.descriptor.codec or "",
            unit.descriptor.hdr or "",
            unit.descriptor.audio or "",
            unit.descriptor.language or "",
            unit.descriptor.release_group or "",
            unit.descriptor.version or "",
        )
    ).encode()
    return EpisodeUnitMetadata(
        kind=episode.kind,
        season=episode.season,
        start=episode.start,
        end=episode.end,
        label=label,
        group_key=group_key,
        variant_key=sha256(variant_payload).hexdigest(),
    )


def normalize_source_relative_path(value: str) -> str:
    if not value or "\x00" in value:
        raise DomainViolation(ErrorCode.SOURCE_UNIT_INVALID, "源文件相对路径为空或包含 NUL")
    normalized = unicodedata.normalize("NFC", value).replace("\\", "/")
    if normalized.startswith("/") or _WINDOWS_DRIVE_RE.match(normalized):
        raise DomainViolation(ErrorCode.SOURCE_UNIT_INVALID, "源文件路径必须是安全相对路径")
    parts = normalized.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise DomainViolation(ErrorCode.SOURCE_UNIT_INVALID, "源文件路径包含不安全路径段")
    return "/".join(parts)


def _classify_unit(descriptor: MediaDescriptor) -> TaskUnitKind:
    if descriptor.episode is not None:
        return TaskUnitKind.EPISODE
    if descriptor.title_tokens:
        return TaskUnitKind.MOVIE
    return TaskUnitKind.UNKNOWN


@dataclass(frozen=True, slots=True)
class _EpisodeDirectoryContext:
    season: int
    title_tokens: tuple[str, ...]
    year: int | None


def _apply_episode_directory_context(
    descriptor: MediaDescriptor,
    *,
    display_name: str,
    episode_context: str,
) -> MediaDescriptor:
    context = _episode_directory_context(episode_context)
    if context is None:
        return descriptor
    episode = descriptor.episode
    if episode is not None:
        if episode.kind in {
            EpisodeKind.SEASON_EPISODE,
            EpisodeKind.SEASON_RANGE,
            EpisodeKind.SPECIALS,
            EpisodeKind.ABSOLUTE,
        }:
            return replace(
                descriptor,
                title_tokens=descriptor.title_tokens or context.title_tokens,
                year=descriptor.year if descriptor.year is not None else context.year,
            )
        if episode.kind is EpisodeKind.EPISODE and episode.start is not None:
            kind = EpisodeKind.SPECIALS if context.season == 0 else EpisodeKind.SEASON_EPISODE
            return replace(
                descriptor,
                title_tokens=descriptor.title_tokens or context.title_tokens,
                year=descriptor.year if descriptor.year is not None else context.year,
                episode=EpisodeIdentity(kind, context.season, episode.start, episode.end),
            )

    inferred = _infer_context_episode(display_name, season=context.season)
    if inferred is None or not context.title_tokens:
        return descriptor
    return replace(
        descriptor,
        title_tokens=context.title_tokens,
        year=descriptor.year if descriptor.year is not None else context.year,
        episode=inferred,
    )


def _episode_directory_context(value: str) -> _EpisodeDirectoryContext | None:
    normalized = unicodedata.normalize("NFC", value).replace("\\", "/")
    parts = tuple(part for part in normalized.split("/") if part and part != ".")
    for index in range(len(parts) - 1, -1, -1):
        component = parts[index].strip()
        specials = _SPECIALS_COMPONENT_RE.fullmatch(component)
        season_match = _SEASON_COMPONENT_RE.fullmatch(component)
        if specials is None and season_match is None:
            continue
        if specials is not None:
            season = 0
        else:
            assert season_match is not None
            season = int(season_match.group(1))
        title_tokens: tuple[str, ...] = ()
        year: int | None = None
        if index > 0:
            parent = parse_media_name(parts[index - 1])
            title_tokens = parent.title_tokens
            year = parent.year
        return _EpisodeDirectoryContext(season=season, title_tokens=title_tokens, year=year)
    return None


def _infer_context_episode(display_name: str, *, season: int) -> EpisodeIdentity | None:
    range_match = _CONTEXT_EPISODE_RANGE_RE.search(display_name)
    if range_match is None:
        range_match = _CONTEXT_BARE_RANGE_RE.search(display_name)
    if range_match is not None:
        start, end = (int(item) for item in range_match.groups())
        if start > end:
            return None
        kind = EpisodeKind.SPECIALS if season == 0 else EpisodeKind.SEASON_RANGE
        return EpisodeIdentity(kind, season, start, end)
    single_match = _CONTEXT_EPISODE_SINGLE_RE.search(display_name)
    if single_match is None:
        single_match = _CONTEXT_BARE_SINGLE_RE.search(display_name)
    if single_match is None:
        return None
    episode = int(single_match.group(1))
    kind = EpisodeKind.SPECIALS if season == 0 else EpisodeKind.SEASON_EPISODE
    return EpisodeIdentity(kind, season, episode, episode)


def _episode_label(episode: EpisodeIdentity) -> str:
    if episode.kind is EpisodeKind.SPECIALS:
        if episode.start is None:
            return "Specials"
        if episode.end is not None and episode.end != episode.start:
            return f"Specials E{episode.start:02d}-E{episode.end:02d}"
        return f"Specials E{episode.start:02d}"
    if episode.kind is EpisodeKind.SEASON_RANGE:
        if episode.season is None or episode.start is None or episode.end is None:
            raise ValueError("范围集标识字段不完整")
        return f"S{episode.season:02d}E{episode.start:02d}-E{episode.end:02d}"
    if episode.kind is EpisodeKind.SEASON_EPISODE:
        if episode.season is None or episode.start is None:
            raise ValueError("季集标识字段不完整")
        return f"S{episode.season:02d}E{episode.start:02d}"
    if episode.kind is EpisodeKind.EPISODE:
        if episode.start is None:
            raise ValueError("集标识字段不完整")
        return f"EP{episode.start:02d}"
    if episode.kind is EpisodeKind.ABSOLUTE:
        if episode.start is None:
            raise ValueError("绝对集标识字段不完整")
        return f"ABS{episode.start:02d}"
    raise ValueError("未知季集标识类型")


def _unit_key(source_file: SourceTaskFile) -> str:
    payload = (f"{_UNIT_KEY_VERSION}\n{source_file.relative_path}\n{source_file.length}").encode()
    return sha256(payload).hexdigest()
