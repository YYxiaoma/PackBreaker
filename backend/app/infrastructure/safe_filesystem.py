from __future__ import annotations

import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from backend.app.domain.errors import DomainViolation, ErrorCode
from backend.app.domain.verification import FileSnapshot


@dataclass(frozen=True, slots=True)
class FilesystemSnapshot:
    device: int
    inode: int
    size: int
    mtime_ns: int
    file_type: str
    link_count: int

    def to_payload(self) -> dict[str, int | str]:
        return {
            "device": self.device,
            "inode": self.inode,
            "size": self.size,
            "mtime_ns": self.mtime_ns,
            "file_type": self.file_type,
            "link_count": self.link_count,
        }


@dataclass(frozen=True, slots=True)
class HardlinkInspection:
    source_relative_path: str
    target_root_relative_path: str
    target_relative_path: str
    source_snapshot: FilesystemSnapshot
    nearest_target_parent_relative_path: str
    target_parent_snapshot: FilesystemSnapshot
    missing_directories: tuple[str, ...]


class SafeFilesystemGateway:
    """M3 文件系统安全边界；当前切片只允许只读检查和快照。"""

    def __init__(self, data_root: Path) -> None:
        self._data_root = data_root

    def inspect_hardlink(
        self,
        *,
        source_relative_path: str,
        target_root_relative_path: str,
        target_relative_path: str,
        expected_source_snapshot: FileSnapshot,
    ) -> HardlinkInspection:
        root, root_snapshot = self._require_data_root()
        source_relative, source_parts = _normalize_relative_path(source_relative_path)
        target_root_relative, target_root_parts = _normalize_relative_path(
            target_root_relative_path, allow_root=True
        )
        target_relative, target_parts = _normalize_relative_path(target_relative_path)

        source_path, source_snapshot = self._require_regular_file(root, source_parts)
        _assert_source_snapshot(source_snapshot, expected_source_snapshot)

        target_root, target_root_snapshot = self._require_directory_chain(
            root,
            target_root_parts,
            root_snapshot,
        )
        if target_root_snapshot.device != source_snapshot.device:
            raise DomainViolation(
                ErrorCode.CROSS_DEVICE_LINK,
                "源文件与目标根目录不在同一设备，不能创建硬链接",
            )

        current = target_root
        current_snapshot = target_root_snapshot
        current_relative_parts: list[str] = []
        nearest_existing_relative_parts: list[str] = []
        missing_directories: list[str] = []
        missing_started = False

        for part in target_parts[:-1]:
            current_relative_parts.append(part)
            relative = "/".join(current_relative_parts)
            if missing_started:
                missing_directories.append(relative)
                continue

            candidate = current / part
            candidate_snapshot = _lstat_snapshot(candidate, missing_ok=True)
            if candidate_snapshot is None:
                missing_started = True
                missing_directories.append(relative)
                continue
            if candidate_snapshot.file_type != "directory":
                raise DomainViolation(
                    ErrorCode.PATH_MAPPING_INVALID,
                    "硬链接目标父路径必须是普通目录且不能是符号链接",
                )
            if candidate_snapshot.device != target_root_snapshot.device:
                raise DomainViolation(
                    ErrorCode.CROSS_DEVICE_LINK,
                    "硬链接目标父路径跨越了不同设备",
                )
            current = candidate
            current_snapshot = candidate_snapshot
            nearest_existing_relative_parts.append(part)

        if source_snapshot.device != current_snapshot.device:
            raise DomainViolation(
                ErrorCode.CROSS_DEVICE_LINK,
                "源文件与目标父目录不在同一设备，不能创建硬链接",
            )

        if not missing_started:
            target_path = current / target_parts[-1]
            if _lstat_snapshot(target_path, missing_ok=True) is not None:
                raise DomainViolation(ErrorCode.TARGET_CONFLICT, "硬链接目标路径已存在")

        return HardlinkInspection(
            source_relative_path=source_relative,
            target_root_relative_path=target_root_relative,
            target_relative_path=target_relative,
            source_snapshot=source_snapshot,
            nearest_target_parent_relative_path=(
                "."
                if not nearest_existing_relative_parts
                else "/".join(nearest_existing_relative_parts)
            ),
            target_parent_snapshot=current_snapshot,
            missing_directories=tuple(missing_directories),
        )

    def _require_data_root(self) -> tuple[Path, FilesystemSnapshot]:
        snapshot = _lstat_snapshot(self._data_root)
        if snapshot is None or snapshot.file_type != "directory":
            raise DomainViolation(
                ErrorCode.PATH_MAPPING_INVALID,
                "数据根目录必须是真实目录且不能是符号链接",
            )
        return self._data_root, snapshot

    def _require_regular_file(
        self,
        root: Path,
        parts: tuple[str, ...],
    ) -> tuple[Path, FilesystemSnapshot]:
        current = root
        for index, part in enumerate(parts):
            current = current / part
            snapshot = _lstat_snapshot(current)
            if snapshot is None:
                raise DomainViolation(ErrorCode.PATH_MAPPING_INVALID, "源文件路径不可见")
            if index < len(parts) - 1:
                if snapshot.file_type != "directory":
                    raise DomainViolation(
                        ErrorCode.PATH_MAPPING_INVALID,
                        "源文件路径不能经过符号链接或非目录节点",
                    )
                continue
            if snapshot.file_type != "regular":
                raise DomainViolation(
                    ErrorCode.PATH_MAPPING_INVALID,
                    "源文件必须是普通文件且不能是符号链接",
                )
            return current, snapshot
        raise DomainViolation(ErrorCode.PATH_MAPPING_INVALID, "源文件路径不能为空")

    def _require_directory_chain(
        self,
        root: Path,
        parts: tuple[str, ...],
        root_snapshot: FilesystemSnapshot,
    ) -> tuple[Path, FilesystemSnapshot]:
        current = root
        current_snapshot = root_snapshot
        for part in parts:
            current = current / part
            snapshot = _lstat_snapshot(current)
            if snapshot is None or snapshot.file_type != "directory":
                raise DomainViolation(
                    ErrorCode.PATH_MAPPING_INVALID,
                    "目标根目录必须存在、为普通目录且不能经过符号链接",
                )
            current_snapshot = snapshot
        return current, current_snapshot


def _normalize_relative_path(
    value: str, *, allow_root: bool = False
) -> tuple[str, tuple[str, ...]]:
    normalized = unicodedata.normalize("NFC", value.strip())
    has_windows_drive = len(normalized) >= 2 and normalized[0].isalpha() and normalized[1] == ":"
    if (
        not normalized
        or "\x00" in normalized
        or "\\" in normalized
        or normalized.startswith("/")
        or has_windows_drive
    ):
        raise DomainViolation(
            ErrorCode.PATH_MAPPING_INVALID, "文件系统路径必须是安全 POSIX 相对路径"
        )
    if normalized == ".":
        if allow_root:
            return ".", ()
        raise DomainViolation(ErrorCode.PATH_MAPPING_INVALID, "文件系统路径不能为空")
    parts = tuple(normalized.split("/"))
    if any(not part or part in {".", ".."} for part in parts):
        raise DomainViolation(ErrorCode.PATH_MAPPING_INVALID, "文件系统路径包含不安全路径段")
    return "/".join(parts), parts


def _lstat_snapshot(path: Path, *, missing_ok: bool = False) -> FilesystemSnapshot | None:
    try:
        result = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise DomainViolation(ErrorCode.PATH_MAPPING_INVALID, "文件系统路径不可见") from None
    except OSError as exc:
        raise DomainViolation(ErrorCode.PATH_MAPPING_INVALID, "无法读取文件系统路径状态") from exc

    if stat.S_ISREG(result.st_mode):
        file_type = "regular"
    elif stat.S_ISDIR(result.st_mode):
        file_type = "directory"
    elif stat.S_ISLNK(result.st_mode):
        file_type = "symlink"
    else:
        file_type = "other"
    return FilesystemSnapshot(
        device=result.st_dev,
        inode=result.st_ino,
        size=result.st_size,
        mtime_ns=result.st_mtime_ns,
        file_type=file_type,
        link_count=result.st_nlink,
    )


def _assert_source_snapshot(observed: FilesystemSnapshot, expected: FileSnapshot) -> None:
    if (
        observed.device != expected.device
        or observed.inode != expected.inode
        or observed.size != expected.size
        or observed.mtime_ns != expected.mtime_ns
        or observed.file_type != expected.file_type
    ):
        raise DomainViolation(ErrorCode.SOURCE_CHANGED, "源文件与执行计划快照不一致")
