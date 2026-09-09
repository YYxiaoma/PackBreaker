from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from pathlib import PurePosixPath

from backend.app.domain.errors import DomainViolation, ErrorCode
from backend.app.domain.media_matching import MediaDescriptor, MediaFileSummary, parse_media_name

_UNIT_KEY_VERSION = "packbreaker-task-unit-v1"
_WINDOWS_DRIVE_RE = re.compile(r"^[a-zA-Z]:")
_VIDEO_EXTENSIONS = frozenset({".avi", ".m4v", ".mkv", ".mov", ".mp4", ".wmv"})


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


def identify_task_units(files: tuple[SourceTaskFile, ...]) -> tuple[TaskUnit, ...]:
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
        if path.suffix.casefold() not in _VIDEO_EXTENSIONS or source_file.length == 0:
            continue
        display_name = path.stem
        descriptor = parse_media_name(
            display_name,
            total_size=source_file.length,
            files=(MediaFileSummary(path.name, source_file.length),),
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


def _unit_key(source_file: SourceTaskFile) -> str:
    payload = (f"{_UNIT_KEY_VERSION}\n{source_file.relative_path}\n{source_file.length}").encode()
    return sha256(payload).hexdigest()
