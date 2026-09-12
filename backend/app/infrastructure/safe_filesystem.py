from __future__ import annotations

import ctypes
import errno
import os
import stat
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from backend.app.domain.errors import DomainViolation, ErrorCode
from backend.app.domain.repair import RepairTargetEvidence
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


@dataclass(frozen=True, slots=True)
class DirectoryCreationInspection:
    target_root_relative_path: str
    directory_relative_path: str
    parent_relative_path: str
    parent_snapshot: FilesystemSnapshot


class SafeFilesystemGateway:
    """M3 文件系统安全边界；所有写操作都要求调用方先持久化 journal intent。"""

    def __init__(self, data_root: Path) -> None:
        self._data_root = data_root

    def normalize_relative_path(self, value: str, *, allow_root: bool = False) -> str:
        normalized, _ = _normalize_relative_path(value, allow_root=allow_root)
        return normalized

    def assert_source_matches(
        self,
        *,
        source_relative_path: str,
        expected_source_snapshot: FileSnapshot,
    ) -> FilesystemSnapshot:
        root, _ = self._require_data_root()
        _, source_parts = _normalize_relative_path(source_relative_path)
        _, snapshot = self._require_regular_file(root, source_parts)
        _assert_source_snapshot(snapshot, expected_source_snapshot)
        return snapshot

    def assert_directory(
        self,
        *,
        relative_path: str,
        expected_device: int | None = None,
    ) -> FilesystemSnapshot:
        """重新确认 `/data` 内目录链不含符号链接，并可锁定设备身份。"""

        root, root_snapshot = self._require_data_root()
        _, parts = _normalize_relative_path(relative_path, allow_root=True)
        _, snapshot = self._require_directory_chain(root, parts, root_snapshot)
        if expected_device is not None and snapshot.device != expected_device:
            raise DomainViolation(ErrorCode.CROSS_DEVICE_LINK, "目标目录设备与执行计划不一致")
        return snapshot

    def inspect_repair_target(
        self,
        *,
        target_root_relative_path: str,
        target_relative_path: str,
        expected_length: int,
        source_relative_path: str | None = None,
        expected_source_snapshot: FileSnapshot | None = None,
    ) -> RepairTargetEvidence:
        """只读检查未来 repair target 的 inode 身份和可用空间，不创建或修改文件。"""

        if expected_length < 0:
            raise ValueError("repair target expected_length 不能为负数")
        if (source_relative_path is None) != (expected_source_snapshot is None):
            raise ValueError("repair target source 路径与快照必须同时提供")

        root, root_snapshot = self._require_data_root()
        _, target_root_parts = _normalize_relative_path(
            target_root_relative_path,
            allow_root=True,
        )
        target_relative, target_parts = _normalize_relative_path(target_relative_path)
        _, target_root_snapshot = self._require_directory_chain(
            root,
            target_root_parts,
            root_snapshot,
        )

        source_device: int | None = None
        source_inode: int | None = None
        if source_relative_path is not None and expected_source_snapshot is not None:
            _, source_parts = _normalize_relative_path(source_relative_path)
            _, source_snapshot = self._require_regular_file(root, source_parts)
            _assert_source_snapshot(source_snapshot, expected_source_snapshot)
            source_device = source_snapshot.device
            source_inode = source_snapshot.inode
            if source_snapshot.size != expected_length:
                raise DomainViolation(
                    ErrorCode.SOURCE_CHANGED,
                    "repair source 长度与计划不一致",
                )

        parent_fd = _open_directory_chain(root, target_root_parts + target_parts[:-1])
        try:
            parent_snapshot = _fstat_snapshot(parent_fd)
            if parent_snapshot.device != target_root_snapshot.device:
                raise DomainViolation(
                    ErrorCode.CROSS_DEVICE_LINK,
                    "repair target 父目录设备与目标根不一致",
                )
            target_snapshot = _stat_at(parent_fd, target_parts[-1], missing_ok=True)
            if target_snapshot is not None and target_snapshot.file_type != "regular":
                raise DomainViolation(
                    ErrorCode.PATH_MAPPING_INVALID,
                    "repair target 必须是普通文件且不能是符号链接",
                )
            try:
                filesystem = os.fstatvfs(parent_fd)
            except OSError as exc:
                raise DomainViolation(
                    ErrorCode.PATH_MAPPING_INVALID,
                    "无法读取 repair target 文件系统空间",
                ) from exc
            fragment_size = filesystem.f_frsize or filesystem.f_bsize
            available_bytes = filesystem.f_bavail * fragment_size
        finally:
            os.close(parent_fd)

        return RepairTargetEvidence(
            torrent_path=target_relative,
            expected_length=expected_length,
            target_exists=target_snapshot is not None,
            target_device=None if target_snapshot is None else target_snapshot.device,
            target_inode=None if target_snapshot is None else target_snapshot.inode,
            target_size=None if target_snapshot is None else target_snapshot.size,
            target_link_count=None if target_snapshot is None else target_snapshot.link_count,
            source_device=source_device,
            source_inode=source_inode,
            available_bytes=available_bytes,
        )

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

    def inspect_directory_creation(
        self,
        *,
        target_root_relative_path: str,
        directory_relative_path: str,
    ) -> DirectoryCreationInspection:
        root, root_snapshot = self._require_data_root()
        target_root_relative, target_root_parts = _normalize_relative_path(
            target_root_relative_path,
            allow_root=True,
        )
        directory_relative, directory_parts = _normalize_relative_path(directory_relative_path)
        target_root, target_root_snapshot = self._require_directory_chain(
            root,
            target_root_parts,
            root_snapshot,
        )
        parent, parent_snapshot = self._require_directory_chain(
            target_root,
            directory_parts[:-1],
            target_root_snapshot,
        )
        if parent_snapshot.device != target_root_snapshot.device:
            raise DomainViolation(
                ErrorCode.CROSS_DEVICE_LINK,
                "待创建目录的现有父路径跨越了不同设备",
            )
        if _lstat_snapshot(parent / directory_parts[-1], missing_ok=True) is not None:
            raise DomainViolation(ErrorCode.TARGET_CONFLICT, "待创建目标目录已存在")
        parent_relative = "/".join(directory_parts[:-1]) or "."
        return DirectoryCreationInspection(
            target_root_relative_path=target_root_relative,
            directory_relative_path=directory_relative,
            parent_relative_path=parent_relative,
            parent_snapshot=parent_snapshot,
        )

    def create_directory(
        self,
        *,
        target_root_relative_path: str,
        directory_relative_path: str,
        expected_parent_snapshot: FilesystemSnapshot,
    ) -> FilesystemSnapshot:
        """只创建一个目录层级；调用前必须已经提交对应 operation journal intent。"""

        _, target_root_parts = _normalize_relative_path(target_root_relative_path, allow_root=True)
        _, directory_parts = _normalize_relative_path(directory_relative_path)
        parent_parts = target_root_parts + directory_parts[:-1]
        parent_fd = _open_directory_chain(self._data_root, parent_parts)
        try:
            observed_parent = _fstat_snapshot(parent_fd)
            _assert_filesystem_snapshot(observed_parent, expected_parent_snapshot)
            name = directory_parts[-1]
            if _stat_at(parent_fd, name, missing_ok=True) is not None:
                raise DomainViolation(ErrorCode.TARGET_CONFLICT, "待创建目标目录已存在")
            try:
                os.mkdir(name, mode=0o755, dir_fd=parent_fd)
            except FileExistsError as exc:
                raise DomainViolation(ErrorCode.TARGET_CONFLICT, "待创建目标目录并发出现") from exc
            except OSError as exc:
                raise DomainViolation(
                    ErrorCode.PATH_MAPPING_INVALID, "无法安全创建目标目录"
                ) from exc
            created = _stat_at(parent_fd, name)
            if created is None or created.file_type != "directory":
                raise DomainViolation(ErrorCode.PATH_MAPPING_INVALID, "创建后的目标目录状态异常")
            return created
        finally:
            os.close(parent_fd)

    def create_hardlink_atomic(
        self,
        *,
        source_relative_path: str,
        target_root_relative_path: str,
        target_relative_path: str,
        expected_source_snapshot: FileSnapshot,
        expected_target_parent_snapshot: FilesystemSnapshot,
        operation_token: str,
        fault_hook: Callable[[str], None] | None = None,
    ) -> FilesystemSnapshot:
        """以 deterministic 临时硬链接 + renameat2(NOREPLACE) 原子落位。"""

        _, source_parts = _normalize_relative_path(source_relative_path)
        _, target_root_parts = _normalize_relative_path(target_root_relative_path, allow_root=True)
        _, target_parts = _normalize_relative_path(target_relative_path)
        if len(operation_token) != 64 or any(
            character not in "0123456789abcdef" for character in operation_token
        ):
            raise DomainViolation(ErrorCode.PATH_MAPPING_INVALID, "硬链接操作 token 无效")

        source_parent_fd = _open_directory_chain(self._data_root, source_parts[:-1])
        target_parent_fd = _open_directory_chain(
            self._data_root,
            target_root_parts + target_parts[:-1],
        )
        try:
            source_name = source_parts[-1]
            target_name = target_parts[-1]
            observed_source = _stat_at(source_parent_fd, source_name)
            if observed_source is None or observed_source.file_type != "regular":
                raise DomainViolation(ErrorCode.PATH_MAPPING_INVALID, "源文件必须是普通文件")
            _assert_source_snapshot(observed_source, expected_source_snapshot)

            observed_parent = _fstat_snapshot(target_parent_fd)
            if observed_source.device != observed_parent.device:
                raise DomainViolation(ErrorCode.CROSS_DEVICE_LINK, "源文件与目标父目录不在同一设备")
            if _stat_at(target_parent_fd, target_name, missing_ok=True) is not None:
                raise DomainViolation(ErrorCode.TARGET_CONFLICT, "硬链接目标路径已存在")

            temporary_name = f".packbreaker-link-{operation_token}.tmp"
            temporary_snapshot = _stat_at(target_parent_fd, temporary_name, missing_ok=True)
            if temporary_snapshot is None:
                _assert_filesystem_snapshot(observed_parent, expected_target_parent_snapshot)
                try:
                    os.link(
                        source_name,
                        temporary_name,
                        src_dir_fd=source_parent_fd,
                        dst_dir_fd=target_parent_fd,
                        follow_symlinks=False,
                    )
                except FileExistsError as exc:
                    raise DomainViolation(ErrorCode.TARGET_CONFLICT, "临时硬链接并发出现") from exc
                except OSError as exc:
                    code = (
                        ErrorCode.CROSS_DEVICE_LINK
                        if exc.errno == errno.EXDEV
                        else ErrorCode.PATH_MAPPING_INVALID
                    )
                    raise DomainViolation(code, "无法安全创建临时硬链接") from exc
                _call_fault_hook(fault_hook, "after_temporary_hardlink")
                temporary_snapshot = _stat_at(target_parent_fd, temporary_name)
            elif not _same_owned_directory(observed_parent, expected_target_parent_snapshot):
                raise DomainViolation(
                    ErrorCode.SOURCE_CHANGED,
                    "临时硬链接恢复时目标父目录身份已变化",
                )

            if temporary_snapshot is None or not _same_file_identity(
                temporary_snapshot,
                observed_source,
            ):
                raise DomainViolation(ErrorCode.TARGET_CONFLICT, "临时硬链接与预期源文件不一致")
            current_source = _stat_at(source_parent_fd, source_name)
            if current_source is None:
                raise DomainViolation(ErrorCode.SOURCE_CHANGED, "源文件在硬链接落位前消失")
            _assert_source_snapshot(current_source, expected_source_snapshot)
            if _stat_at(target_parent_fd, target_name, missing_ok=True) is not None:
                raise DomainViolation(ErrorCode.TARGET_CONFLICT, "硬链接目标路径并发出现")

            _rename_noreplace(
                source_dir_fd=target_parent_fd,
                source_name=temporary_name,
                target_dir_fd=target_parent_fd,
                target_name=target_name,
            )
            _call_fault_hook(fault_hook, "after_final_hardlink")
            final_snapshot = _stat_at(target_parent_fd, target_name)
            if final_snapshot is None or not _same_file_identity(final_snapshot, current_source):
                raise DomainViolation(ErrorCode.PATH_MAPPING_INVALID, "硬链接落位后的目标状态异常")
            return final_snapshot
        finally:
            os.close(source_parent_fd)
            os.close(target_parent_fd)

    def remove_hardlink_if_matches(
        self,
        *,
        target_root_relative_path: str,
        target_relative_path: str,
        expected_snapshot: FilesystemSnapshot,
    ) -> bool:
        _, target_root_parts = _normalize_relative_path(target_root_relative_path, allow_root=True)
        _, target_parts = _normalize_relative_path(target_relative_path)
        parent_fd = _open_directory_chain(
            self._data_root,
            target_root_parts + target_parts[:-1],
        )
        try:
            name = target_parts[-1]
            current = _stat_at(parent_fd, name, missing_ok=True)
            if current is None:
                return False
            if not _same_owned_file(current, expected_snapshot):
                raise DomainViolation(
                    ErrorCode.ROLLBACK_BLOCKED, "目标硬链接快照已变化，禁止自动删除"
                )
            try:
                os.unlink(name, dir_fd=parent_fd)
            except OSError as exc:
                raise DomainViolation(ErrorCode.ROLLBACK_BLOCKED, "无法安全删除目标硬链接") from exc
            return True
        finally:
            os.close(parent_fd)

    def assert_hardlink_matches(
        self,
        *,
        target_root_relative_path: str,
        target_relative_path: str,
        expected_snapshot: FilesystemSnapshot,
    ) -> FilesystemSnapshot:
        _, target_root_parts = _normalize_relative_path(target_root_relative_path, allow_root=True)
        _, target_parts = _normalize_relative_path(target_relative_path)
        parent_fd = _open_directory_chain(
            self._data_root,
            target_root_parts + target_parts[:-1],
        )
        try:
            current = _stat_at(parent_fd, target_parts[-1])
            if current is None or not _same_owned_file(current, expected_snapshot):
                raise DomainViolation(
                    ErrorCode.SOURCE_CHANGED, "已登记硬链接与 after snapshot 不一致"
                )
            return current
        finally:
            os.close(parent_fd)

    def remove_directory_if_matches(
        self,
        *,
        target_root_relative_path: str,
        directory_relative_path: str,
        expected_snapshot: FilesystemSnapshot,
    ) -> bool:
        _, target_root_parts = _normalize_relative_path(target_root_relative_path, allow_root=True)
        _, directory_parts = _normalize_relative_path(directory_relative_path)
        parent_fd = _open_directory_chain(
            self._data_root,
            target_root_parts + directory_parts[:-1],
        )
        try:
            name = directory_parts[-1]
            current = _stat_at(parent_fd, name, missing_ok=True)
            if current is None:
                return False
            if not _same_owned_directory(current, expected_snapshot):
                raise DomainViolation(
                    ErrorCode.ROLLBACK_BLOCKED, "目标目录快照已变化，禁止自动删除"
                )
            try:
                os.rmdir(name, dir_fd=parent_fd)
            except OSError as exc:
                raise DomainViolation(
                    ErrorCode.ROLLBACK_BLOCKED, "目标目录非空或无法安全删除"
                ) from exc
            return True
        finally:
            os.close(parent_fd)

    def assert_directory_matches(
        self,
        *,
        target_root_relative_path: str,
        directory_relative_path: str,
        expected_snapshot: FilesystemSnapshot,
    ) -> FilesystemSnapshot:
        _, target_root_parts = _normalize_relative_path(target_root_relative_path, allow_root=True)
        _, directory_parts = _normalize_relative_path(directory_relative_path)
        parent_fd = _open_directory_chain(
            self._data_root,
            target_root_parts + directory_parts[:-1],
        )
        try:
            current = _stat_at(parent_fd, directory_parts[-1])
            if current is None or not _same_owned_directory(current, expected_snapshot):
                raise DomainViolation(
                    ErrorCode.SOURCE_CHANGED, "已登记目录与 after snapshot 不一致"
                )
            return current
        finally:
            os.close(parent_fd)

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

    return _snapshot_from_stat(result)


def _assert_source_snapshot(observed: FilesystemSnapshot, expected: FileSnapshot) -> None:
    if (
        observed.device != expected.device
        or observed.inode != expected.inode
        or observed.size != expected.size
        or observed.mtime_ns != expected.mtime_ns
        or observed.file_type != expected.file_type
    ):
        raise DomainViolation(ErrorCode.SOURCE_CHANGED, "源文件与执行计划快照不一致")


def _snapshot_from_stat(result: os.stat_result) -> FilesystemSnapshot:
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


def _open_directory_chain(root: Path, parts: tuple[str, ...]) -> int:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        current_fd = os.open(root, flags)
    except OSError as exc:
        raise DomainViolation(ErrorCode.PATH_MAPPING_INVALID, "无法安全打开数据根目录") from exc
    try:
        for part in parts:
            try:
                next_fd = os.open(part, flags, dir_fd=current_fd)
            except OSError as exc:
                raise DomainViolation(
                    ErrorCode.PATH_MAPPING_INVALID,
                    "目标或源路径不能经过符号链接、缺失目录或非目录节点",
                ) from exc
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except Exception:
        os.close(current_fd)
        raise


def _fstat_snapshot(fd: int) -> FilesystemSnapshot:
    try:
        return _snapshot_from_stat(os.fstat(fd))
    except OSError as exc:
        raise DomainViolation(ErrorCode.PATH_MAPPING_INVALID, "无法读取已打开目录状态") from exc


def _stat_at(fd: int, name: str, *, missing_ok: bool = False) -> FilesystemSnapshot | None:
    try:
        result = os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise DomainViolation(ErrorCode.PATH_MAPPING_INVALID, "文件系统目标路径不可见") from None
    except OSError as exc:
        raise DomainViolation(ErrorCode.PATH_MAPPING_INVALID, "无法读取文件系统目标状态") from exc
    return _snapshot_from_stat(result)


def _assert_filesystem_snapshot(
    observed: FilesystemSnapshot,
    expected: FilesystemSnapshot,
) -> None:
    if observed != expected:
        raise DomainViolation(ErrorCode.SOURCE_CHANGED, "文件系统父目录快照已变化")


def _same_file_identity(left: FilesystemSnapshot, right: FilesystemSnapshot) -> bool:
    return (
        left.file_type == "regular"
        and right.file_type == "regular"
        and left.device == right.device
        and left.inode == right.inode
        and left.size == right.size
        and left.mtime_ns == right.mtime_ns
    )


def _same_owned_file(current: FilesystemSnapshot, expected: FilesystemSnapshot) -> bool:
    return _same_file_identity(current, expected)


def _same_owned_directory(current: FilesystemSnapshot, expected: FilesystemSnapshot) -> bool:
    return (
        current.file_type == "directory"
        and expected.file_type == "directory"
        and current.device == expected.device
        and current.inode == expected.inode
    )


def _call_fault_hook(hook: Callable[[str], None] | None, checkpoint: str) -> None:
    if hook is not None:
        hook(checkpoint)


def _rename_noreplace(
    *,
    source_dir_fd: int,
    source_name: str,
    target_dir_fd: int,
    target_name: str,
) -> None:
    renameat2 = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
    if renameat2 is None:
        raise DomainViolation(
            ErrorCode.PATH_MAPPING_INVALID,
            "当前系统缺少 renameat2，不能安全执行无覆盖原子落位",
        )
    result = renameat2(
        ctypes.c_int(source_dir_fd),
        ctypes.c_char_p(os.fsencode(source_name)),
        ctypes.c_int(target_dir_fd),
        ctypes.c_char_p(os.fsencode(target_name)),
        ctypes.c_uint(1),
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number == errno.EEXIST:
        raise DomainViolation(ErrorCode.TARGET_CONFLICT, "硬链接最终目标并发出现")
    if error_number == errno.EXDEV:
        raise DomainViolation(ErrorCode.CROSS_DEVICE_LINK, "硬链接原子落位跨越不同设备")
    raise DomainViolation(ErrorCode.PATH_MAPPING_INVALID, "硬链接无法安全原子落位")
