"""升级日志（release notes）的读取与校验。

升级日志随镜像发布（`resources/release_notes.json`，由 `scripts/build_release_notes.py`
从各版本 tag 的 RELEASE_NOTES.md 生成），因此离线部署的「关于」页也能看到历史版本
的变更说明。文件缺失或损坏时返回空目录并记 warning，不影响启动与其它功能。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MAX_ENTRIES = 50
MAX_SECTIONS_PER_ENTRY = 12
MAX_ITEMS_PER_SECTION = 12
MAX_TITLE_LENGTH = 60
MAX_ITEM_LENGTH = 400

_VERSION_PATTERN = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}$")

logger = logging.getLogger("packbreaker.release_notes")


@dataclass(frozen=True, slots=True)
class ReleaseNoteSection:
    title: str
    items: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReleaseNoteEntry:
    version: str
    tag: str
    released_on: str
    summary: str
    sections: tuple[ReleaseNoteSection, ...]
    is_current: bool


@dataclass(frozen=True, slots=True)
class ReleaseNotesCatalog:
    entries: tuple[ReleaseNoteEntry, ...]

    def covers(self, version: str) -> bool:
        """当前运行版本是否已有对应条目（发布时应保证为 True）。"""

        normalized = _normalize_version(version)
        return any(entry.version == normalized for entry in self.entries)


def load_release_notes(path: Path, *, current_version: str | None = None) -> ReleaseNotesCatalog:
    """读取升级日志文件；任何异常都 fail-soft 成空目录。"""

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        logger.warning("升级日志文件不存在，已跳过：%s", path)
        return ReleaseNotesCatalog(())
    try:
        payload = json.loads(raw)
    except ValueError:
        logger.warning("升级日志文件不是合法 JSON，已跳过：%s", path)
        return ReleaseNotesCatalog(())
    if not isinstance(payload, dict):
        logger.warning("升级日志文件结构无效，已跳过：%s", path)
        return ReleaseNotesCatalog(())
    raw_entries = payload.get("entries")
    if not isinstance(raw_entries, list):
        logger.warning("升级日志文件缺少 entries，已跳过：%s", path)
        return ReleaseNotesCatalog(())

    current = _normalize_version(current_version or "")
    parsed: list[ReleaseNoteEntry] = []
    seen: set[str] = set()
    for item in raw_entries:
        entry = _parse_entry(item, current)
        if entry is None or entry.version in seen:
            continue
        seen.add(entry.version)
        parsed.append(entry)

    parsed.sort(key=_version_sort_key, reverse=True)
    return ReleaseNotesCatalog(tuple(parsed[:MAX_ENTRIES]))


def _parse_entry(payload: Any, current_version: str) -> ReleaseNoteEntry | None:
    if not isinstance(payload, dict):
        return None
    version = _normalize_version(_text(payload.get("version")))
    if not version or not _VERSION_PATTERN.match(version):
        return None
    tag = _text(payload.get("tag")) or f"v{version}"
    released_on = _text(payload.get("released_on"))
    summary = _text(payload.get("summary"))[:MAX_ITEM_LENGTH]
    sections = _parse_sections(payload.get("sections"))
    if not sections:
        return None
    return ReleaseNoteEntry(
        version=version,
        tag=tag,
        released_on=released_on,
        summary=summary,
        sections=sections,
        is_current=version == current_version,
    )


def _parse_sections(payload: Any) -> tuple[ReleaseNoteSection, ...]:
    if not isinstance(payload, list):
        return ()
    sections: list[ReleaseNoteSection] = []
    for item in payload[:MAX_SECTIONS_PER_ENTRY]:
        if not isinstance(item, dict):
            continue
        title = _text(item.get("title"))[:MAX_TITLE_LENGTH]
        raw_items = item.get("items")
        if not title or not isinstance(raw_items, list):
            continue
        items = tuple(
            text
            for text in (
                _text(value)[:MAX_ITEM_LENGTH] for value in raw_items[:MAX_ITEMS_PER_SECTION]
            )
            if text
        )
        if items:
            sections.append(ReleaseNoteSection(title=title, items=items))
    return tuple(sections)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _normalize_version(value: str) -> str:
    return value.strip().lstrip("vV")


def _version_sort_key(entry: ReleaseNoteEntry) -> tuple[int, int, int]:
    return tuple(int(part) for part in entry.version.split("."))  # type: ignore[return-value]
