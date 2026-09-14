from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from heapq import heappop, heappush
from pathlib import PurePosixPath


@dataclass(frozen=True, slots=True)
class HistoryFileSnapshot:
    relative_path: str
    device: int
    inode: int
    size: int
    mtime_ns: int


@dataclass(frozen=True, slots=True)
class HistoryScanPage:
    snapshots: tuple[HistoryFileSnapshot, ...]
    has_more: bool


class HistoryFilesystemScanner:
    """只读、no-follow 的历史目录元数据扫描器。"""

    def __init__(self, data_root: str | os.PathLike[str]) -> None:
        self._data_root = os.fspath(data_root)

    def scan(
        self,
        *,
        root_relative_path: str,
        extensions: tuple[str, ...],
        exclude_patterns: tuple[str, ...],
    ) -> tuple[HistoryFileSnapshot, ...]:
        root_fd = self._open_root(root_relative_path)
        try:
            snapshots: list[HistoryFileSnapshot] = []
            self._walk(
                root_fd,
                prefix="",
                extensions=frozenset(extensions),
                exclude_patterns=exclude_patterns,
                output=snapshots,
            )
            snapshots.sort(key=lambda item: item.relative_path)
            return tuple(snapshots)
        finally:
            os.close(root_fd)

    def scan_page(
        self,
        *,
        root_relative_path: str,
        extensions: tuple[str, ...],
        exclude_patterns: tuple[str, ...],
        after: str | None,
        limit: int,
    ) -> HistoryScanPage:
        """按稳定字典序读取一个有界页面，并跳过 cursor 之前的整棵子树。"""

        if limit < 1:
            raise ValueError("history scan page limit 必须大于 0")
        root_fd = self._open_root(root_relative_path)
        try:
            snapshots: list[HistoryFileSnapshot] = []
            pending = self._directory_names(root_fd, prefix="")
            self._collect_page(
                root_fd=root_fd,
                pending=pending,
                extensions=frozenset(extensions),
                exclude_patterns=exclude_patterns,
                after=after,
                stop_after=limit + 1,
                output=snapshots,
            )
            has_more = len(snapshots) > limit
            return HistoryScanPage(tuple(snapshots[:limit]), has_more)
        finally:
            os.close(root_fd)

    def _open_root(self, relative_path: str) -> int:
        flags = os.O_RDONLY | os.O_DIRECTORY
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        current_fd = os.open(self._data_root, flags)
        try:
            if relative_path == ".":
                return current_fd
            for part in relative_path.split("/"):
                next_fd = os.open(part, flags, dir_fd=current_fd)
                os.close(current_fd)
                current_fd = next_fd
            return current_fd
        except BaseException:
            os.close(current_fd)
            raise

    def _walk(
        self,
        directory_fd: int,
        *,
        prefix: str,
        extensions: frozenset[str],
        exclude_patterns: tuple[str, ...],
        output: list[HistoryFileSnapshot],
    ) -> None:
        with os.scandir(directory_fd) as iterator:
            names = sorted(entry.name for entry in iterator)
        directory_flags = os.O_RDONLY | os.O_DIRECTORY
        directory_flags |= getattr(os, "O_CLOEXEC", 0)
        directory_flags |= getattr(os, "O_NOFOLLOW", 0)
        for name in names:
            relative_path = f"{prefix}/{name}" if prefix else name
            if self._excluded(relative_path, exclude_patterns):
                continue
            try:
                observed = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(observed.st_mode):
                continue
            if stat.S_ISDIR(observed.st_mode):
                try:
                    child_fd = os.open(name, directory_flags, dir_fd=directory_fd)
                except (FileNotFoundError, NotADirectoryError):
                    continue
                try:
                    self._walk(
                        child_fd,
                        prefix=relative_path,
                        extensions=extensions,
                        exclude_patterns=exclude_patterns,
                        output=output,
                    )
                finally:
                    os.close(child_fd)
                continue
            if not stat.S_ISREG(observed.st_mode):
                continue
            if PurePosixPath(name).suffix.casefold() not in extensions:
                continue
            output.append(
                HistoryFileSnapshot(
                    relative_path=relative_path,
                    device=observed.st_dev,
                    inode=observed.st_ino,
                    size=observed.st_size,
                    mtime_ns=observed.st_mtime_ns,
                )
            )

    def _collect_page(
        self,
        *,
        root_fd: int,
        pending: list[str],
        extensions: frozenset[str],
        exclude_patterns: tuple[str, ...],
        after: str | None,
        stop_after: int,
        output: list[HistoryFileSnapshot],
    ) -> None:
        while pending and len(output) < stop_after:
            relative_path = heappop(pending)
            if self._excluded(relative_path, exclude_patterns):
                continue

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
                observed = self._stat_relative(root_fd, relative_path)
            except (FileNotFoundError, NotADirectoryError):
                continue
            if stat.S_ISLNK(observed.st_mode):
                continue
            if stat.S_ISDIR(observed.st_mode):
                if after is not None and not (
                    subtree_prefix > after or after.startswith(subtree_prefix)
                ):
                    continue
                try:
                    child_fd = self._open_directory_relative(root_fd, relative_path)
                except (FileNotFoundError, NotADirectoryError):
                    continue
                try:
                    for child_path in self._directory_names(child_fd, prefix=relative_path):
                        heappush(pending, child_path)
                finally:
                    os.close(child_fd)
                continue
            if after is not None and relative_path <= after:
                continue
            if not stat.S_ISREG(observed.st_mode):
                continue
            if PurePosixPath(relative_path).suffix.casefold() not in extensions:
                continue
            output.append(
                HistoryFileSnapshot(
                    relative_path=relative_path,
                    device=observed.st_dev,
                    inode=observed.st_ino,
                    size=observed.st_size,
                    mtime_ns=observed.st_mtime_ns,
                )
            )

    @staticmethod
    def _directory_names(directory_fd: int, *, prefix: str) -> list[str]:
        with os.scandir(directory_fd) as iterator:
            paths = [f"{prefix}/{entry.name}" if prefix else entry.name for entry in iterator]
        paths.sort()
        return paths

    @staticmethod
    def _directory_flags() -> int:
        flags = os.O_RDONLY | os.O_DIRECTORY
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        return flags

    def _open_directory_relative(self, root_fd: int, relative_path: str) -> int:
        current_fd = os.dup(root_fd)
        try:
            for part in relative_path.split("/"):
                next_fd = os.open(part, self._directory_flags(), dir_fd=current_fd)
                os.close(current_fd)
                current_fd = next_fd
            return current_fd
        except BaseException:
            os.close(current_fd)
            raise

    def _stat_relative(self, root_fd: int, relative_path: str) -> os.stat_result:
        parts = relative_path.split("/")
        current_fd = os.dup(root_fd)
        try:
            for part in parts[:-1]:
                next_fd = os.open(part, self._directory_flags(), dir_fd=current_fd)
                os.close(current_fd)
                current_fd = next_fd
            return os.stat(parts[-1], dir_fd=current_fd, follow_symlinks=False)
        finally:
            os.close(current_fd)

    @staticmethod
    def _excluded(relative_path: str, patterns: tuple[str, ...]) -> bool:
        folded = relative_path.casefold()
        return any(pattern in folded for pattern in patterns)
