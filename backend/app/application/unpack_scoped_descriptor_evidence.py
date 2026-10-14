"""Linux descriptor-pinned, read-only evidence for scoped CarPT acceptance.

Only opens DIRECTORY descriptors and O_PATH media descriptors (never media
content), then closes all handles. This is not a lease or permission to write:
replacements remain possible after return, and writer-side dirfd/intent gates
must be independently authorized.
"""

from __future__ import annotations

import os
import stat
from contextlib import ExitStack
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal

from backend.app.application.unpack_scoped_write_environment import (
    ScopedEnvironmentStatus,
    _canonical_absolute,
    inspect_scoped_write_environment,
)
from backend.app.domain.unpack_execution_plan import (
    UnpackExecutionActionKind,
    UnpackExecutionPlan,
)
from backend.app.domain.verification import FileSnapshot


class ScopedDescriptorStatus(StrEnum):
    BLOCKED_METADATA = "BLOCKED_METADATA"
    BLOCKED_DIRECTORY = "BLOCKED_DIRECTORY"
    BLOCKED_SOURCE = "BLOCKED_SOURCE"
    BLOCKED_RACE = "BLOCKED_RACE"
    OBSERVED_ONLY_NO_GRANT = "OBSERVED_ONLY_NO_GRANT"


@dataclass(frozen=True, slots=True)
class ScopedDescriptorEvidence:
    status: ScopedDescriptorStatus
    source_count: int = 0
    copy_bytes: int = 0
    writes_authorized: Literal[False] = False
    media_content_read: Literal[False] = False
    downloader_contacted: Literal[False] = False
    descriptor_bound_write_permission: Literal[False] = False


def _pin_directory(stack: ExitStack, path: Path) -> int:
    """Walk from / via openat, rejecting a symlink in every component."""
    if not _canonical_absolute(path):
        raise ValueError("noncanonical absolute path")
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise OSError("no safe directory open flags")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    current = os.open("/", flags)
    stack.callback(os.close, current)
    for component in path.parts[1:]:
        nxt = os.open(component, flags, dir_fd=current)
        stack.callback(os.close, nxt)
        current = nxt
    return current


def _object_id(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _source_snapshot(info: os.stat_result) -> FileSnapshot:
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("source is not regular")
    return FileSnapshot(
        device=info.st_dev,
        inode=info.st_ino,
        size=info.st_size,
        mtime_ns=info.st_mtime_ns,
        file_type="regular",
    )


def _empty_directory(fd: int) -> bool:
    with os.scandir(fd) as iterator:
        return next(iterator, None) is None


def inspect_scoped_descriptor_evidence(
    plan: UnpackExecutionPlan,
    *,
    source_directory: Path,
    source_filename: str,
    approved_parent: Path,
    expected_output: Path,
    proposal_metadata_matches: bool,
) -> ScopedDescriptorEvidence:
    """Return only non-authorizing proof; perform no writes or media reads."""
    report = ScopedDescriptorEvidence
    if (
        not hasattr(os, "O_PATH")
        or not hasattr(os, "O_NOFOLLOW")
        or not hasattr(os, "O_DIRECTORY")
        or not proposal_metadata_matches
        or source_filename in {"", ".", ".."}
        or "/" in source_filename
        or "\\" in source_filename
        or "\x00" in source_filename
        or not all(
            _canonical_absolute(p) for p in (source_directory, approved_parent, expected_output)
        )
        or expected_output.parent != approved_parent
    ):
        return report(ScopedDescriptorStatus.BLOCKED_METADATA)

    metadata = inspect_scoped_write_environment(
        plan,
        source_directory=source_directory,
        source_filename=source_filename,
        approved_parent=approved_parent,
        expected_output=expected_output,
        proposal_metadata_matches=proposal_metadata_matches,
    )
    if metadata.status is not ScopedEnvironmentStatus.METADATA_ONLY_NO_GRANT:
        return report(ScopedDescriptorStatus.BLOCKED_METADATA)

    with ExitStack() as stack:
        try:
            parent_fd = _pin_directory(stack, approved_parent)
            parent_initial = os.fstat(parent_fd)
            source_dir_fd = _pin_directory(stack, source_directory)
            source_dir_initial = os.fstat(source_dir_fd)
            if not stat.S_ISDIR(parent_initial.st_mode) or not stat.S_ISDIR(
                source_dir_initial.st_mode
            ):
                return report(ScopedDescriptorStatus.BLOCKED_DIRECTORY)

            output_fd = os.open(
                expected_output.name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=parent_fd,
            )
            stack.callback(os.close, output_fd)
            output_initial = os.fstat(output_fd)
            if (
                not stat.S_ISDIR(output_initial.st_mode)
                or output_initial.st_dev != plan.target_device
                or not _empty_directory(output_fd)
            ):
                return report(ScopedDescriptorStatus.BLOCKED_DIRECTORY)

            source_fds: list[tuple[int, FileSnapshot, str]] = []
            selected = False
            copy_bytes = 0
            for action in plan.actions:
                if action.kind is not UnpackExecutionActionKind.MATERIALIZE:
                    continue
                if (
                    action.source_path is None
                    or action.source_snapshot is None
                    or not _canonical_absolute(Path(action.source_path))
                    or Path(action.source_path).parent != source_directory
                    or action.reuse_target_snapshot is not None
                ):
                    return report(ScopedDescriptorStatus.BLOCKED_SOURCE)
                name = Path(action.source_path).name
                if name == source_filename:
                    selected = True
                file_fd = os.open(
                    name,
                    os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=source_dir_fd,
                )
                stack.callback(os.close, file_fd)
                pinned = _source_snapshot(os.fstat(file_fd))
                if pinned != action.source_snapshot or pinned.size != action.length:
                    return report(ScopedDescriptorStatus.BLOCKED_SOURCE)
                source_fds.append((file_fd, pinned, name))
                copy_bytes += action.length
            if not selected or not source_fds:
                return report(ScopedDescriptorStatus.BLOCKED_SOURCE)
            space = os.fstatvfs(output_fd)
            if space.f_bavail * space.f_frsize <= copy_bytes:
                return report(ScopedDescriptorStatus.BLOCKED_DIRECTORY)

            # A pathname's current identity must still point to the pinned
            # objects. Reopen rather than only fstat a potentially unlinked fd.
            with ExitStack() as final:
                parent_again = _pin_directory(final, approved_parent)
                source_again = _pin_directory(final, source_directory)
                output_again = os.open(
                    expected_output.name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=parent_again,
                )
                final.callback(os.close, output_again)
                if (
                    _object_id(os.fstat(parent_again)) != _object_id(parent_initial)
                    or _object_id(os.fstat(source_again)) != _object_id(source_dir_initial)
                    or _object_id(os.fstat(output_again)) != _object_id(output_initial)
                    or not _empty_directory(output_fd)
                    or not _empty_directory(output_again)
                ):
                    return report(ScopedDescriptorStatus.BLOCKED_RACE)
                for fd, snapshot, name in source_fds:
                    reopened = os.open(
                        name,
                        os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC,
                        dir_fd=source_again,
                    )
                    final.callback(os.close, reopened)
                    if (
                        _source_snapshot(os.fstat(fd)) != snapshot
                        or _source_snapshot(os.fstat(reopened)) != snapshot
                    ):
                        return report(ScopedDescriptorStatus.BLOCKED_RACE)

            return report(
                ScopedDescriptorStatus.OBSERVED_ONLY_NO_GRANT,
                source_count=len(source_fds),
                copy_bytes=copy_bytes,
            )
        except (OSError, ValueError):
            # Nothing can be inferred when any pinned/openat step fails.
            return report(ScopedDescriptorStatus.BLOCKED_RACE)
