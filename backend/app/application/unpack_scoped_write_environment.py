"""Read-only filesystem evidence for a proposed isolated CarPT acceptance.

The inspection is explicitly NON-AUTHORIZING and NOT race-safe permission.
It does not create directories, read media content, connect to clients, or
change execution/journal state. A future writer needs independent host approval,
real-time descriptor-bound checks, and durable intent before side effects.
"""

from __future__ import annotations

import os
import shutil
import stat
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal

from backend.app.domain.task_definition import TaskConflictPolicy, TaskStorageMode
from backend.app.domain.unpack_execution_plan import (
    UnpackExecutionActionKind,
    UnpackExecutionPlan,
)
from backend.app.domain.verification import FileSnapshot
from backend.app.infrastructure.source_inventory import current_file_snapshot


class ScopedEnvironmentStatus(StrEnum):
    BLOCKED_SCOPE = "BLOCKED_SCOPE"
    BLOCKED_PARENT = "BLOCKED_PARENT"
    BLOCKED_OUTPUT_MISSING = "BLOCKED_OUTPUT_MISSING"
    BLOCKED_OUTPUT_UNSAFE = "BLOCKED_OUTPUT_UNSAFE"
    BLOCKED_SOURCE = "BLOCKED_SOURCE"
    BLOCKED_CAPACITY = "BLOCKED_CAPACITY"
    METADATA_ONLY_NO_GRANT = "METADATA_ONLY_NO_GRANT"


@dataclass(frozen=True, slots=True)
class ScopedEnvironmentReport:
    status: ScopedEnvironmentStatus
    source_count: int = 0
    copy_bytes: int = 0
    writes_authorized: Literal[False] = False
    media_content_read: Literal[False] = False
    downloader_contacted: Literal[False] = False
    descriptor_bound_write_permission: Literal[False] = False


def _canonical_absolute(path: Path) -> bool:
    value = path.as_posix()
    return (
        path.is_absolute()
        and value != "/"
        and "\x00" not in value
        and "\\" not in value
        and "//" not in value
        and str(path) == value
        and not any(part in {".", "..", ""} for part in value.split("/")[1:])
    )


def _safe_components(path: Path, *, must_exist: bool = True) -> os.stat_result | None:
    if not _canonical_absolute(path):
        return None
    stat_result: os.stat_result | None = None
    for current in (*reversed(path.parents), path):
        try:
            info = current.lstat()
        except OSError:
            return None
        if stat.S_ISLNK(info.st_mode):
            return None
        if current != path and not stat.S_ISDIR(info.st_mode):
            return None
        stat_result = info
    if stat_result is None:
        return None
    if must_exist and not stat.S_ISDIR(stat_result.st_mode):
        return None
    return stat_result


def inspect_scoped_write_environment(
    plan: UnpackExecutionPlan,
    *,
    source_directory: Path,
    source_filename: str,
    approved_parent: Path,
    expected_output: Path,
    proposal_metadata_matches: bool,
) -> ScopedEnvironmentReport:
    """Inspect read-only path metadata; matching is NEVER a write approval."""
    blocked = ScopedEnvironmentReport
    if (
        not proposal_metadata_matches
        or not plan.ready
        or plan.storage_mode is not TaskStorageMode.COPY
        or plan.conflict_policy is not TaskConflictPolicy.VERIFY_REUSE_OR_STOP
        or not _canonical_absolute(source_directory)
        or not _canonical_absolute(approved_parent)
        or not _canonical_absolute(expected_output)
        or expected_output.parent != approved_parent
        or Path(plan.output_directory) != expected_output
        or source_filename in {"", ".", ".."}
        or "/" in source_filename
        or "\\" in source_filename
        or "\x00" in source_filename
        or source_directory == expected_output
        or source_directory in expected_output.parents
        or expected_output in source_directory.parents
    ):
        return blocked(ScopedEnvironmentStatus.BLOCKED_SCOPE)

    parent_info = _safe_components(approved_parent)
    if parent_info is None or not stat.S_ISDIR(parent_info.st_mode):
        return blocked(ScopedEnvironmentStatus.BLOCKED_PARENT)

    try:
        info = expected_output.lstat()
    except FileNotFoundError:
        return blocked(ScopedEnvironmentStatus.BLOCKED_OUTPUT_MISSING)
    except OSError:
        return blocked(ScopedEnvironmentStatus.BLOCKED_OUTPUT_UNSAFE)

    output_info = _safe_components(expected_output)
    if (
        output_info is None
        or not stat.S_ISDIR(info.st_mode)
        or not stat.S_ISDIR(output_info.st_mode)
        or info.st_ino != output_info.st_ino
        or info.st_dev != plan.target_device
    ):
        return blocked(ScopedEnvironmentStatus.BLOCKED_OUTPUT_UNSAFE)

    # We may observe an empty directory and it could change later: this is a
    # read-only observation, not a lease or proof of write safety.
    try:
        with os.scandir(expected_output) as entries:
            if next(entries, None) is not None:
                return blocked(ScopedEnvironmentStatus.BLOCKED_OUTPUT_UNSAFE)
    except OSError:
        return blocked(ScopedEnvironmentStatus.BLOCKED_OUTPUT_UNSAFE)

    if _safe_components(source_directory) is None:
        return blocked(ScopedEnvironmentStatus.BLOCKED_SOURCE)
    allowed_source = source_directory / source_filename
    found_selected = False
    count = 0
    copy_bytes = 0
    source_checks: list[tuple[Path, FileSnapshot]] = []
    for action in plan.actions:
        if action.kind is not UnpackExecutionActionKind.MATERIALIZE:
            continue
        count += 1
        copy_bytes += action.length
        if not action.source_path or action.source_snapshot is None:
            return blocked(ScopedEnvironmentStatus.BLOCKED_SOURCE)
        path = Path(action.source_path)
        if (
            not _canonical_absolute(path)
            or path.parent != source_directory
            or path == expected_output
            or action.reuse_target_snapshot is not None
        ):
            return blocked(ScopedEnvironmentStatus.BLOCKED_SOURCE)
        if path == allowed_source:
            found_selected = True
        if not _safe_regular_file(path):
            return blocked(ScopedEnvironmentStatus.BLOCKED_SOURCE)
        try:
            observed = current_file_snapshot(path)
        except (OSError, ValueError):
            return blocked(ScopedEnvironmentStatus.BLOCKED_SOURCE)
        if observed != action.source_snapshot or observed.size != action.length:
            return blocked(ScopedEnvironmentStatus.BLOCKED_SOURCE)
        source_checks.append((path, observed))

    if count < 1 or not found_selected:
        return blocked(ScopedEnvironmentStatus.BLOCKED_SOURCE)

    try:
        free_bytes = shutil.disk_usage(expected_output).free
    except OSError:
        return blocked(ScopedEnvironmentStatus.BLOCKED_CAPACITY)
    if free_bytes <= copy_bytes:
        return blocked(ScopedEnvironmentStatus.BLOCKED_CAPACITY)
    # Detect common changes during inspection, but even a stable observation
    # is NOT a descriptor-bound lease and must never be used to permit writes.
    final_parent = _safe_components(approved_parent)
    final_output = _safe_components(expected_output)
    if (
        final_parent is None
        or final_output is None
        or _directory_identity(parent_info) != _directory_identity(final_parent)
        or _directory_identity(output_info) != _directory_identity(final_output)
    ):
        return blocked(ScopedEnvironmentStatus.BLOCKED_OUTPUT_UNSAFE)
    try:
        with os.scandir(expected_output) as entries:
            if next(entries, None) is not None:
                return blocked(ScopedEnvironmentStatus.BLOCKED_OUTPUT_UNSAFE)
    except OSError:
        return blocked(ScopedEnvironmentStatus.BLOCKED_OUTPUT_UNSAFE)
    for path, snapshot in source_checks:
        if not _safe_regular_file(path):
            return blocked(ScopedEnvironmentStatus.BLOCKED_SOURCE)
        try:
            if current_file_snapshot(path) != snapshot:
                return blocked(ScopedEnvironmentStatus.BLOCKED_SOURCE)
        except (OSError, ValueError):
            return blocked(ScopedEnvironmentStatus.BLOCKED_SOURCE)
    return ScopedEnvironmentReport(
        ScopedEnvironmentStatus.METADATA_ONLY_NO_GRANT,
        source_count=count,
        copy_bytes=copy_bytes,
    )


def _directory_identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_ctime_ns, info.st_mtime_ns


def _safe_regular_file(path: Path) -> bool:
    if not _canonical_absolute(path):
        return False
    parents = (*reversed(path.parents), path)
    for component in parents:
        try:
            info = component.lstat()
        except OSError:
            return False
        if stat.S_ISLNK(info.st_mode):
            return False
        if component != path and not stat.S_ISDIR(info.st_mode):
            return False
        if component == path and not stat.S_ISREG(info.st_mode):
            return False
    return True
