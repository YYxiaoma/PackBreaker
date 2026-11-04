from __future__ import annotations

import os
import re
import stat
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from html import unescape
from pathlib import Path

from backend.app.domain.media_matching import ExternalMediaId, MediaDescriptor, tokenize_title

_MAX_NFO_BYTES = 1024 * 1024
_IMDB_RE = re.compile(r"(?i)\b(tt\d{5,10})\b")
_DOUBAN_RE = re.compile(
    r"(?i)(?:douban\.com/subject/|(?:douban|豆瓣)(?:\s*id)?\s*[:：=]\s*)(\d{3,12})"
)
_TITLE_TAG_RE = re.compile(
    r"(?is)<(?:[\w.-]+:)?(?P<tag>originaltitle|sorttitle|localizedtitle|chinesetitle|title|aka)"
    r"\b[^>]*>(?P<value>.*?)</(?:[\w.-]+:)?(?P=tag)\s*>"
)
_TITLE_LABEL_RE = re.compile(
    r"(?im)^\s*(?:original\s*title|sort\s*title|localized\s*title|chinese\s*title|"
    r"movie\s*title|title|中文片名|原名|片名)\s*[:：]\s*(.{1,200})\s*$"
)
_TITLE_TAGS = frozenset(
    {"originaltitle", "sorttitle", "localizedtitle", "chinesetitle", "title", "aka"}
)


@dataclass(frozen=True, slots=True)
class AdjacentMediaMetadata:
    titles: tuple[str, ...] = ()
    external_ids: tuple[ExternalMediaId, ...] = ()


def read_adjacent_media_metadata(
    media_path: str,
    source_root: str,
    *,
    expected_device: int | None,
    expected_inode: int | None,
    expected_size: int,
    expected_mtime_ns: str | int | None,
    descriptor: MediaDescriptor,
) -> AdjacentMediaMetadata:
    """Read only a bounded, matching sidecar NFO; never read media payload bytes."""
    try:
        root = Path(source_root)
        media = Path(media_path)
        if not root.is_absolute() or not media.is_absolute():
            return AdjacentMediaMetadata()
        root_info = root.lstat()
        media_info = media.lstat()
        if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
            return AdjacentMediaMetadata()
        if stat.S_ISLNK(media_info.st_mode) or not stat.S_ISREG(media_info.st_mode):
            return AdjacentMediaMetadata()
        if (
            isinstance(expected_device, int)
            and not isinstance(expected_device, bool)
            and media_info.st_dev != expected_device
        ):
            return AdjacentMediaMetadata()
        if (
            isinstance(expected_inode, int)
            and not isinstance(expected_inode, bool)
            and media_info.st_ino != expected_inode
        ):
            return AdjacentMediaMetadata()
        if media_info.st_size != expected_size:
            return AdjacentMediaMetadata()
        if expected_mtime_ns is not None:
            try:
                if media_info.st_mtime_ns != int(expected_mtime_ns):
                    return AdjacentMediaMetadata()
            except (TypeError, ValueError):
                return AdjacentMediaMetadata()

        resolved_root = root.resolve(strict=True)
        resolved_media = media.resolve(strict=True)
        resolved_media.relative_to(resolved_root)
        current = media.parent
        while current != root:
            current_info = current.lstat()
            if stat.S_ISLNK(current_info.st_mode) or not stat.S_ISDIR(current_info.st_mode):
                return AdjacentMediaMetadata()
            if current.parent == current:
                return AdjacentMediaMetadata()
            current = current.parent

        try:
            siblings = tuple(media.parent.iterdir())
        except OSError:
            return AdjacentMediaMetadata()
        exact_stem = sorted(
            (
                path
                for path in siblings
                if path.suffix.casefold() == ".nfo"
                and path.stem.casefold() == media.stem.casefold()
            ),
            key=lambda path: path.name.casefold(),
        )
        if exact_stem:
            candidates = tuple((path, True) for path in exact_stem[:1])
        else:
            generic = sorted(
                (
                    path
                    for path in siblings
                    if path.suffix.casefold() == ".nfo"
                    and (
                        path.name.casefold() in {"movie.nfo", "tvshow.nfo"}
                        or path.stem.casefold() == media.parent.name.casefold()
                    )
                ),
                key=lambda path: path.name.casefold(),
            )
            candidates = tuple((path, False) for path in generic[:3])

        for candidate, exact_match in candidates:
            text = _read_bounded_regular_file(candidate)
            if text is None:
                continue
            metadata = parse_nfo_metadata(text)
            if not exact_match and not _metadata_title_matches(metadata.titles, descriptor):
                continue
            if metadata.titles or metadata.external_ids:
                return metadata
        return AdjacentMediaMetadata()
    except (OSError, ValueError, TypeError):
        return AdjacentMediaMetadata()


def parse_nfo_metadata(text: str) -> AdjacentMediaMetadata:
    """Extract only movie titles and IMDb/Douban identifiers from a bounded NFO string."""
    if not text or len(text) > _MAX_NFO_BYTES:
        return AdjacentMediaMetadata()

    titles: list[str] = []
    ids: dict[str, str] = {}

    for match in _IMDB_RE.finditer(text):
        ids.setdefault("imdb", match.group(1).casefold())
    for match in _DOUBAN_RE.finditer(text):
        ids.setdefault("douban", match.group(1))

    # Reject DTD/entity-bearing documents. NFO sidecars are data, not XML programs.
    xml_root = None
    if "<!DOCTYPE" not in text.upper() and "<!ENTITY" not in text.upper():
        try:
            xml_root = ET.fromstring(text)
        except (ET.ParseError, LookupError, ValueError):
            xml_root = None
    if xml_root is not None:
        for element in xml_root.iter():
            if not isinstance(element.tag, str):
                continue
            tag = element.tag.rsplit("}", 1)[-1].casefold()
            value = " ".join("".join(element.itertext()).split())
            if tag in _TITLE_TAGS:
                _append_title(titles, value)
            elif tag in {"imdbid", "imdb"}:
                xml_match = _IMDB_RE.search(value)
                if xml_match:
                    ids.setdefault("imdb", xml_match.group(1).casefold())
            elif tag in {"doubanid", "douban"}:
                xml_match = re.search(r"\d{3,12}", value)
                if xml_match:
                    ids.setdefault("douban", xml_match.group(0))
            elif tag == "uniqueid":
                namespace = (element.attrib.get("type") or "").casefold()
                if namespace in {"imdb", "douban"} and value:
                    pattern = (
                        _IMDB_RE.search(value)
                        if namespace == "imdb"
                        else re.search(r"\d{3,12}", value)
                    )
                    if pattern:
                        identifier = pattern.group(1) if namespace == "imdb" else pattern.group(0)
                        ids.setdefault(namespace, identifier)

    for match in _TITLE_TAG_RE.finditer(text):
        _append_title(titles, _clean_title(match.group("value")))
    for match in _TITLE_LABEL_RE.finditer(text):
        _append_title(titles, _clean_title(match.group(1)))

    return AdjacentMediaMetadata(
        titles=tuple(titles),
        external_ids=tuple(
            ExternalMediaId(namespace, value) for namespace, value in sorted(ids.items()) if value
        ),
    )


def _read_bounded_regular_file(path: Path) -> str | None:
    try:
        before = path.lstat()
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            return None
        if before.st_size < 0 or before.st_size > _MAX_NFO_BYTES:
            return None
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_dev != before.st_dev
                or opened.st_ino != before.st_ino
                or opened.st_size > _MAX_NFO_BYTES
            ):
                return None
            data = stream.read(_MAX_NFO_BYTES + 1)
        if len(data) > _MAX_NFO_BYTES:
            return None
    except OSError:
        return None

    for encoding in ("utf-8-sig", "gb18030", "utf-8"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _metadata_title_matches(titles: tuple[str, ...], descriptor: MediaDescriptor) -> bool:
    for title in titles:
        candidate_tokens = set(tokenize_title(title))
        if not candidate_tokens:
            continue
        for variant in (descriptor.title_tokens, *descriptor.alias_tokens):
            source_tokens = set(variant)
            if not source_tokens:
                continue
            dice = (
                2
                * len(source_tokens & candidate_tokens)
                / (len(source_tokens) + len(candidate_tokens))
            )
            if dice >= 0.75:
                return True
    return False


def _append_title(titles: list[str], value: str) -> None:
    cleaned = _clean_title(value)
    if cleaned and cleaned.casefold() not in {item.casefold() for item in titles}:
        titles.append(cleaned)


def _clean_title(value: str) -> str:
    cleaned = unescape(re.sub(r"<[^>]+>", " ", value))
    return " ".join(cleaned.split()).strip(" \t\r\n\"'")[:200]
