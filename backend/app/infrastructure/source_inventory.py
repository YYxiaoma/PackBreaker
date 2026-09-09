from __future__ import annotations

import os
import stat
import unicodedata
from pathlib import Path

from backend.app.domain.errors import DomainViolation, ErrorCode
from backend.app.domain.file_mapping import SourceFileCandidate
from backend.app.domain.verification import FileSnapshot


def scan_source_inventory(
    root: Path, *, max_files: int = 100_000
) -> tuple[SourceFileCandidate, ...]:
    """只读扫描源根；不跟随符号链接，也不读取媒体内容。"""

    if max_files <= 0:
        raise ValueError("max_files 必须大于 0")
    root_stat = _lstat(root)
    if not stat.S_ISDIR(root_stat.st_mode):
        raise DomainViolation(
            ErrorCode.PATH_MAPPING_INVALID, "源扫描根必须是普通目录且不能是符号链接"
        )

    candidates: list[SourceFileCandidate] = []
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            entries = tuple(os.scandir(directory))
        except OSError as exc:
            raise DomainViolation(ErrorCode.PATH_MAPPING_INVALID, "无法读取源目录") from exc
        for entry in sorted(entries, key=lambda item: item.name):
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
    return tuple(sorted(candidates, key=lambda item: (item.relative_path, item.source_path)))


def current_file_snapshot(path: Path) -> FileSnapshot:
    result = _lstat(path)
    if not stat.S_ISREG(result.st_mode):
        raise DomainViolation(ErrorCode.SOURCE_CHANGED, "缓存映射源已不再是普通文件")
    return _snapshot(result)


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
