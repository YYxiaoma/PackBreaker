"""Descriptor-based metadata inspection, never a permission to write."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest

from backend.app.application.unpack_scoped_descriptor_evidence import (
    ScopedDescriptorEvidence,
    ScopedDescriptorStatus,
    inspect_scoped_descriptor_evidence,
)
from tests.unit.test_v112_scoped_write_environment import Fixture, _fixture


def _inspect(f: Fixture, **override: object) -> ScopedDescriptorEvidence:
    args = dict(
        plan=f.plan,
        source_directory=f.source_dir,
        source_filename=f.source_file.name,
        approved_parent=f.parent,
        expected_output=f.output,
        proposal_metadata_matches=True,
    )
    args.update(override)
    return inspect_scoped_descriptor_evidence(**args)  # type: ignore[arg-type]


def _always_false(f: Fixture, status: ScopedDescriptorStatus, **change: object) -> None:
    result = _inspect(f, **change)
    assert result.status == status
    assert result.writes_authorized is False
    assert result.media_content_read is False
    assert result.downloader_contacted is False
    assert result.descriptor_bound_write_permission is False


def test_descriptor_pinned_sources_and_root_still_never_authorize(tmp_path: Path) -> None:
    f = _fixture(tmp_path)
    original = f.source_file.read_bytes()
    result = _inspect(f)
    assert result.status is ScopedDescriptorStatus.OBSERVED_ONLY_NO_GRANT
    assert result.source_count == 1
    assert result.copy_bytes == len(original)
    assert result.writes_authorized is False
    assert f.source_file.read_bytes() == original
    assert list(f.output.iterdir()) == []


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("proposal_metadata_matches", False),
        ("source_filename", "other.mkv"),
        ("source_filename", "../danger"),
        ("approved_parent", Path("/not-approved")),
        ("source_directory", Path("/not-approved")),
    ],
)
def test_proposal_and_scope_changes_fail_closed(
    tmp_path: Path, field: str, replacement: object
) -> None:
    f = _fixture(tmp_path)
    _always_false(f, ScopedDescriptorStatus.BLOCKED_METADATA, **{field: replacement})


def test_preexisting_file_and_missing_output_rejected(tmp_path: Path) -> None:
    f = _fixture(tmp_path)
    (f.output / "unknown-owner").write_bytes(b"unowned")
    _always_false(f, ScopedDescriptorStatus.BLOCKED_METADATA)
    (f.output / "unknown-owner").unlink()
    f.output.rmdir()
    _always_false(f, ScopedDescriptorStatus.BLOCKED_METADATA)
    assert not f.output.exists()


def test_symlink_parent_and_source_are_rejected(tmp_path: Path) -> None:
    f = _fixture(tmp_path)
    f.source_file.unlink()
    other = tmp_path / "other"
    other.write_bytes(b"synthetic")
    f.source_file.symlink_to(other)
    _always_false(f, ScopedDescriptorStatus.BLOCKED_METADATA)


def test_target_device_changed_rejected(tmp_path: Path) -> None:
    f = _fixture(tmp_path)
    plan = replace(f.plan, target_device=f.plan.target_device + 5, plan_digest="")
    _always_false(f, ScopedDescriptorStatus.BLOCKED_METADATA, plan=plan)


def test_direct_file_descriptor_is_o_path_not_media_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = _fixture(tmp_path)
    original_open = os.open
    source_calls: list[int] = []

    def observed_open(
        path: str | bytes | os.PathLike[str], flags: int, *args: object, **kwargs: object
    ) -> int:
        if path == f.source_file.name:
            source_calls.append(flags)
            assert flags & os.O_PATH
            assert flags & os.O_NOFOLLOW
            assert flags & os.O_CLOEXEC
        return original_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "open", observed_open)
    result = _inspect(f)
    assert result.status is ScopedDescriptorStatus.OBSERVED_ONLY_NO_GRANT
    assert len(source_calls) == 2


def test_output_rename_during_pinned_read_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = _fixture(tmp_path)
    original_vfs = os.fstatvfs

    def replace_output(fd: int) -> os.statvfs_result:
        data = original_vfs(fd)
        f.output.rename(f.parent / "moved-old-dir")
        f.output.mkdir()
        return data

    monkeypatch.setattr(os, "fstatvfs", replace_output)
    _always_false(f, ScopedDescriptorStatus.BLOCKED_RACE)


def test_source_replacement_during_pinned_read_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = _fixture(tmp_path)
    original_vfs = os.fstatvfs

    def replace_source(fd: int) -> os.statvfs_result:
        data = original_vfs(fd)
        f.source_file.rename(f.source_dir / "old-video.mkv")
        f.source_file.write_bytes(b"same size synthetic media"[: len(b"tiny synthetic test media")])
        return data

    monkeypatch.setattr(os, "fstatvfs", replace_source)
    _always_false(f, ScopedDescriptorStatus.BLOCKED_RACE)


def test_source_symlink_created_during_pinned_read_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = _fixture(tmp_path)
    original_vfs = os.fstatvfs

    def replace_source(fd: int) -> os.statvfs_result:
        data = original_vfs(fd)
        f.source_file.rename(f.source_dir / "backup-video.mkv")
        f.source_file.symlink_to(f.source_dir / "backup-video.mkv")
        return data

    monkeypatch.setattr(os, "fstatvfs", replace_source)
    _always_false(f, ScopedDescriptorStatus.BLOCKED_RACE)


def test_directory_gains_conflicting_file_mid_inspection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = _fixture(tmp_path)
    original_vfs = os.fstatvfs

    def make_conflict(fd: int) -> os.statvfs_result:
        data = original_vfs(fd)
        (f.output / "unowned").write_bytes(b"synthetic")
        return data

    monkeypatch.setattr(os, "fstatvfs", make_conflict)
    _always_false(f, ScopedDescriptorStatus.BLOCKED_RACE)


def test_insufficient_fd_based_space_is_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = _fixture(tmp_path)

    class TooSmall:
        f_bavail = 1
        f_frsize = 1

    monkeypatch.setattr(os, "fstatvfs", lambda *_args: TooSmall())
    _always_false(f, ScopedDescriptorStatus.BLOCKED_DIRECTORY)


def test_no_output_is_created_from_readonly_evidence(tmp_path: Path) -> None:
    f = _fixture(tmp_path)
    f.output.rmdir()
    _always_false(f, ScopedDescriptorStatus.BLOCKED_METADATA)
    assert not f.output.exists()
