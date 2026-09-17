from __future__ import annotations

import json
import os
import stat
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from heapq import heappop, heappush
from pathlib import Path

from backend.app.domain.errors import DomainViolation, ErrorCode
from backend.app.domain.file_mapping import SourceFileCandidate
from backend.app.domain.verification import FileSnapshot


def scan_source_inventory(
    root: Path,
    *,
    max_files: int = 100_000,
    cancel_check: Callable[[], None] | None = None,
) -> tuple[SourceFileCandidate, ...]:
    """只读扫描源根；不跟随符号链接，也不读取媒体内容。"""

    if max_files <= 0:
        raise ValueError("max_files 必须大于 0")
    _check_cancel(cancel_check)
    root_stat = _lstat(root)
    if not stat.S_ISDIR(root_stat.st_mode):
        raise DomainViolation(
            ErrorCode.PATH_MAPPING_INVALID, "源扫描根必须是普通目录且不能是符号链接"
        )

    candidates: list[SourceFileCandidate] = []
    pending = [root]
    while pending:
        _check_cancel(cancel_check)
        directory = pending.pop()
        try:
            entries = tuple(os.scandir(directory))
        except OSError as exc:
            raise DomainViolation(ErrorCode.PATH_MAPPING_INVALID, "无法读取源目录") from exc
        _check_cancel(cancel_check)
        for index, entry in enumerate(sorted(entries, key=lambda item: item.name)):
            if index % 128 == 0:
                _check_cancel(cancel_check)
            try:
                item_stat = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise DomainViolation(ErrorCode.PATH_MAPPING_INVALID, "无法读取源路径状态") from exc
            if stat.S_ISLNK(item_stat.st_mode):
                continue
            path = Path(entry.path)
            if stat.S_ISDIR(item_stat.st_mode):
                pending.append(path)
                continue
            if not stat.S_ISREG(item_stat.st_mode):
                continue
            if len(candidates) >= max_files:
                raise DomainViolation(ErrorCode.TORRENT_LIMIT_EXCEEDED, "源文件数量超过扫描上限")
            relative = _normalized_relative_path(path, root)
            candidates.append(
                SourceFileCandidate(
                    relative_path=relative,
                    source_path=str(path),
                    length=item_stat.st_size,
                    snapshot=_snapshot(item_stat),
                )
            )
    _check_cancel(cancel_check)
    return tuple(sorted(candidates, key=lambda item: (item.relative_path, item.source_path)))


@dataclass(frozen=True, slots=True)
class SourceInventoryPage:
    candidates: tuple[SourceFileCandidate, ...]
    has_more: bool
    next_cursor: str | None


def scan_source_inventory_page(
    root: Path,
    *,
    after: str | None,
    limit: int,
    cancel_check: Callable[[], None] | None = None,
) -> SourceInventoryPage:
    """按稳定字典序读取有界文件页，并跳过 cursor 之前的子树。"""

    if limit <= 0:
        raise ValueError("limit 必须大于 0")
    _check_cancel(cancel_check)
    root_stat = _lstat(root)
    if not stat.S_ISDIR(root_stat.st_mode):
        raise DomainViolation(
            ErrorCode.PATH_MAPPING_INVALID, "源扫描根必须是普通目录且不能是符号链接"
        )

    flags = _directory_flags()
    try:
        root_fd = os.open(root, flags)
    except OSError as exc:
        raise DomainViolation(ErrorCode.PATH_MAPPING_INVALID, "无法读取源目录") from exc
    try:
        pending = _directory_names(root_fd, prefix="")
        candidates: list[SourceFileCandidate] = []
        while pending and len(candidates) < limit + 1:
            _check_cancel(cancel_check)
            relative_path = heappop(pending)
            subtree_prefix = f"{relative_path}/"
            may_contain_after = (
                after is None
                or relative_path > after
                or subtree_prefix > after
                or after.startswith(subtree_prefix)
            )
            if not may_contain_after:
                continue

            try:
                observed = _stat_relative(root_fd, relative_path)
            except (FileNotFoundError, NotADirectoryError):
                continue
            except OSError as exc:
                raise DomainViolation(ErrorCode.PATH_MAPPING_INVALID, "无法读取源路径状态") from exc
            if stat.S_ISLNK(observed.st_mode):
                continue
            if stat.S_ISDIR(observed.st_mode):
                if after is not None and not (
                    subtree_prefix > after or after.startswith(subtree_prefix)
                ):
                    continue
                try:
                    child_fd = _open_directory_relative(root_fd, relative_path)
                except (FileNotFoundError, NotADirectoryError):
                    continue
                except OSError as exc:
                    raise DomainViolation(ErrorCode.PATH_MAPPING_INVALID, "无法读取源目录") from exc
                try:
                    for child_path in _directory_names(child_fd, prefix=relative_path):
                        heappush(pending, child_path)
                finally:
                    os.close(child_fd)
                continue
            if after is not None and relative_path <= after:
                continue
            if not stat.S_ISREG(observed.st_mode):
                continue
            normalized = _normalized_relative_string(relative_path)
            source_path = root.joinpath(*normalized.split("/"))
            candidates.append(
                SourceFileCandidate(
                    relative_path=normalized,
                    source_path=str(source_path),
                    length=observed.st_size,
                    snapshot=_snapshot(observed),
                )
            )

        has_more = len(candidates) > limit
        page_candidates = tuple(candidates[:limit])
        next_cursor = page_candidates[-1].relative_path if page_candidates else after
        return SourceInventoryPage(page_candidates, has_more, next_cursor)
    finally:
        os.close(root_fd)


def current_file_snapshot(path: Path) -> FileSnapshot:
    result = _lstat(path)
    if not stat.S_ISREG(result.st_mode):
        raise DomainViolation(ErrorCode.SOURCE_CHANGED, "缓存映射源已不再是普通文件")
    return _snapshot(result)


def source_inventory_digest(candidates: tuple[SourceFileCandidate, ...]) -> str:
    payload = [
        {
            "relative_path": item.relative_path,
            "source_path": item.source_path,
            "length": item.length,
            "device": item.snapshot.device,
            "inode": item.snapshot.inode,
            "size": item.snapshot.size,
            "mtime_ns": item.snapshot.mtime_ns,
            "file_type": item.snapshot.file_type,
        }
        for item in candidates
    ]
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return sha256(raw).hexdigest()


def _directory_flags() -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    return flags


def _directory_names(directory_fd: int, *, prefix: str) -> list[str]:
    try:
        with os.scandir(directory_fd) as iterator:
            paths = [f"{prefix}/{entry.name}" if prefix else entry.name for entry in iterator]
    except OSError as exc:
        raise DomainViolation(ErrorCode.PATH_MAPPING_INVALID, "无法读取源目录") from exc
    paths.sort()
    return paths


def _open_directory_relative(root_fd: int, relative_path: str) -> int:
    current_fd = os.dup(root_fd)
    try:
        for part in relative_path.split("/"):
            next_fd = os.open(part, _directory_flags(), dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


def _stat_relative(root_fd: int, relative_path: str) -> os.stat_result:
    parts = relative_path.split("/")
    current_fd = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            next_fd = os.open(part, _directory_flags(), dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        return os.stat(parts[-1], dir_fd=current_fd, follow_symlinks=False)
    finally:
        os.close(current_fd)


def _normalized_relative_string(value: str) -> str:
    parts = tuple(unicodedata.normalize("NFC", part) for part in value.split("/"))
    if not parts or any(not part or part in {".", ".."} for part in parts):
        raise DomainViolation(ErrorCode.PATH_MAPPING_INVALID, "源相对路径无效")
    return "/".join(parts)


def _lstat(path: Path) -> os.stat_result:
    try:
        result = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise DomainViolation(ErrorCode.PATH_MAPPING_INVALID, "无法读取源路径状态") from exc
    if stat.S_ISLNK(result.st_mode):
        raise DomainViolation(ErrorCode.PATH_MAPPING_INVALID, "源扫描根不能是符号链接")
    return result


def _snapshot(result: os.stat_result) -> FileSnapshot:
    return FileSnapshot(
        device=result.st_dev,
        inode=result.st_ino,
        size=result.st_size,
        mtime_ns=result.st_mtime_ns,
        file_type="regular",
    )


def _normalized_relative_path(path: Path, root: Path) -> str:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise DomainViolation(ErrorCode.PATH_MAPPING_INVALID, "源文件逃逸扫描根") from exc
    segments = tuple(unicodedata.normalize("NFC", part) for part in relative.parts)
    if not segments or any(not part or part in {".", ".."} for part in segments):
        raise DomainViolation(ErrorCode.PATH_MAPPING_INVALID, "源相对路径无效")
    return "/".join(segments)


def _check_cancel(cancel_check: Callable[[], None] | None) -> None:
    if cancel_check is not None:
        cancel_check()
