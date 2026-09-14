from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import PurePosixPath


@dataclass(frozen=True, slots=True)
class HistoryFileSnapshot:
    relative_path: str
    device: int
    inode: int
    size: int
    mtime_ns: int


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

    @staticmethod
    def _excluded(relative_path: str, patterns: tuple[str, ...]) -> bool:
        folded = relative_path.casefold()
        return any(pattern in folded for pattern in patterns)
