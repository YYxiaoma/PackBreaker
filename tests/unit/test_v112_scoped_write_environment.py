"""Read-only acceptance filesystem evidence; test files live only under tmp_path."""

from __future__ import annotations

import shutil
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.app.application import unpack_scoped_write_environment as environment
from backend.app.application.unpack_scoped_write_environment import (
    ScopedEnvironmentStatus,
    inspect_scoped_write_environment,
)
from backend.app.domain.task_definition import TaskConflictPolicy, TaskStorageMode
from backend.app.domain.unpack_execution_plan import (
    UnpackExecutionAction,
    UnpackExecutionActionKind,
    UnpackExecutionPlan,
)
from backend.app.domain.verification import VerificationLevel
from backend.app.infrastructure.source_inventory import current_file_snapshot


@dataclass(frozen=True)
class Fixture:
    plan: UnpackExecutionPlan
    source_dir: Path
    source_file: Path
    parent: Path
    output: Path


def _fixture(tmp_path: Path) -> Fixture:
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    source_file = source_dir / "synthetic-video.mkv"
    source_file.write_bytes(b"tiny synthetic test media")
    parent = tmp_path / "approved"
    parent.mkdir()
    output = parent / "carpt-isolated"
    output.mkdir()
    snap = current_file_snapshot(source_file)
    plan = UnpackExecutionPlan(
        item_id="synthetic-item",
        item_version_before=3,
        candidate_id="synthetic-candidate",
        candidate_generation=2,
        metainfo_digest="a" * 64,
        verification_level=VerificationLevel.FULL_VERIFIED,
        output_directory=output.as_posix(),
        storage_mode=TaskStorageMode.COPY,
        conflict_policy=TaskConflictPolicy.VERIFY_REUSE_OR_STOP,
        target_device=output.stat().st_dev,
        target_downloader_id="synthetic-tr",
        target_downloader_version=1,
        target_downloader_binding_digest="b" * 64,
        target_remote_save_path="/remote/synthetic-save",
        client_check_required=True,
        actions=(
            UnpackExecutionAction(
                torrent_path="Pack/synthetic-video.mkv",
                kind=UnpackExecutionActionKind.MATERIALIZE,
                length=snap.size,
                source_path=source_file.as_posix(),
                source_snapshot=snap,
            ),
        ),
        create_directories=("Pack",),
        blocked_reasons=(),
        created_at=datetime.now(UTC),
    )
    return Fixture(plan, source_dir, source_file, parent, output)


def _inspect(f: Fixture, **changes: object) -> environment.ScopedEnvironmentReport:
    args = {
        "plan": f.plan,
        "source_directory": f.source_dir,
        "source_filename": f.source_file.name,
        "approved_parent": f.parent,
        "expected_output": f.output,
        "proposal_metadata_matches": True,
    }
    args.update(changes)
    return inspect_scoped_write_environment(**args)  # type: ignore[arg-type]


def _assert_blocked(f: Fixture, status: ScopedEnvironmentStatus, **changes: object) -> None:
    r = _inspect(f, **changes)
    assert r.status == status
    assert r.writes_authorized is False
    assert r.media_content_read is False
    assert r.downloader_contacted is False
    assert r.descriptor_bound_write_permission is False


def test_exact_empty_root_and_snapshots_are_evidence_only(tmp_path: Path) -> None:
    f = _fixture(tmp_path)
    before = f.source_file.read_bytes()
    report = _inspect(f)
    assert report.status is ScopedEnvironmentStatus.METADATA_ONLY_NO_GRANT
    assert report.source_count == 1
    assert report.copy_bytes == len(before)
    assert report.writes_authorized is False
    assert report.descriptor_bound_write_permission is False
    assert report.downloader_contacted is False
    assert report.media_content_read is False
    assert f.source_file.read_bytes() == before
    assert list(f.output.iterdir()) == []


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"proposal_metadata_matches": False}, ScopedEnvironmentStatus.BLOCKED_SCOPE),
        ({"source_filename": "other.mkv"}, ScopedEnvironmentStatus.BLOCKED_SOURCE),
        ({"source_filename": "../video.mkv"}, ScopedEnvironmentStatus.BLOCKED_SCOPE),
        ({"source_filename": "bad\\name"}, ScopedEnvironmentStatus.BLOCKED_SCOPE),
        ({"source_filename": "bad\x00name"}, ScopedEnvironmentStatus.BLOCKED_SCOPE),
        ({"source_filename": ""}, ScopedEnvironmentStatus.BLOCKED_SCOPE),
    ],
)
def test_strict_scope_and_selected_source(
    tmp_path: Path, change: dict[str, object], expected: ScopedEnvironmentStatus
) -> None:
    f = _fixture(tmp_path)
    _assert_blocked(f, expected, **change)


def test_missing_output_is_never_created(tmp_path: Path) -> None:
    f = _fixture(tmp_path)
    f.output.rmdir()
    _assert_blocked(f, ScopedEnvironmentStatus.BLOCKED_OUTPUT_MISSING)
    assert not f.output.exists()


def test_unrecognized_output_file_or_directory_blocks(tmp_path: Path) -> None:
    f = _fixture(tmp_path)
    (f.output / "existing-file").write_text("unowned")
    _assert_blocked(f, ScopedEnvironmentStatus.BLOCKED_OUTPUT_UNSAFE)


def test_symlink_output_and_broken_symlink_fail_closed(tmp_path: Path) -> None:
    f = _fixture(tmp_path)
    f.output.rmdir()
    other = tmp_path / "elsewhere"
    other.mkdir()
    f.output.symlink_to(other, target_is_directory=True)
    _assert_blocked(f, ScopedEnvironmentStatus.BLOCKED_OUTPUT_UNSAFE)
    f.output.unlink()
    f.output.symlink_to(tmp_path / "missing")
    _assert_blocked(f, ScopedEnvironmentStatus.BLOCKED_OUTPUT_UNSAFE)


def test_parent_symlink_and_missing_parent_fail_closed(tmp_path: Path) -> None:
    f = _fixture(tmp_path)
    alternative = tmp_path / "alternative"
    alternative.mkdir()
    _assert_blocked(
        f,
        ScopedEnvironmentStatus.BLOCKED_SCOPE,
        approved_parent=alternative,
    )
    _assert_blocked(
        f,
        ScopedEnvironmentStatus.BLOCKED_PARENT,
        approved_parent=tmp_path / "missing",
        expected_output=tmp_path / "missing" / f.output.name,
        plan=replace(
            f.plan,
            output_directory=(tmp_path / "missing" / f.output.name).as_posix(),
            plan_digest="",
        ),
    )
    f.output.rmdir()
    f.parent.rmdir()
    f.parent.symlink_to(alternative, target_is_directory=True)
    _assert_blocked(f, ScopedEnvironmentStatus.BLOCKED_PARENT)


def test_output_device_must_equal_frozen_device(tmp_path: Path) -> None:
    f = _fixture(tmp_path)
    plan = replace(f.plan, target_device=f.plan.target_device + 1, plan_digest="")
    _assert_blocked(f, ScopedEnvironmentStatus.BLOCKED_OUTPUT_UNSAFE, plan=plan)


def test_source_snapshot_changed_or_removed_is_blocked(tmp_path: Path) -> None:
    f = _fixture(tmp_path)
    f.source_file.write_bytes(b"modified after plan")
    _assert_blocked(f, ScopedEnvironmentStatus.BLOCKED_SOURCE)
    f.source_file.unlink()
    _assert_blocked(f, ScopedEnvironmentStatus.BLOCKED_SOURCE)


def test_source_symlink_file_or_parent_is_blocked(tmp_path: Path) -> None:
    f = _fixture(tmp_path)
    f.source_file.unlink()
    other = tmp_path / "other.mkv"
    other.write_bytes(b"tiny")
    f.source_file.symlink_to(other)
    _assert_blocked(f, ScopedEnvironmentStatus.BLOCKED_SOURCE)
    f.source_file.unlink()
    f.source_dir.rmdir()
    alternate = tmp_path / "alternate"
    alternate.mkdir()
    (alternate / f.source_file.name).write_bytes(b"tiny")
    f.source_dir.symlink_to(alternate, target_is_directory=True)
    _assert_blocked(f, ScopedEnvironmentStatus.BLOCKED_SOURCE)


def test_other_frozen_action_outside_scoped_source_is_rejected(tmp_path: Path) -> None:
    f = _fixture(tmp_path)
    outsider = tmp_path / "outside.mkv"
    outsider.write_bytes(b"outsider")
    snap = current_file_snapshot(outsider)
    action = UnpackExecutionAction(
        torrent_path="Pack/outsider.mkv",
        kind=UnpackExecutionActionKind.MATERIALIZE,
        length=snap.size,
        source_path=outsider.as_posix(),
        source_snapshot=snap,
    )
    plan = replace(f.plan, actions=(*f.plan.actions, action), plan_digest="")
    _assert_blocked(f, ScopedEnvironmentStatus.BLOCKED_SOURCE, plan=plan)


def test_sibling_auxiliary_file_in_same_source_directory_is_allowed_as_metadata(
    tmp_path: Path,
) -> None:
    f = _fixture(tmp_path)
    sibling = f.source_dir / "subtitle.srt"
    sibling.write_bytes(b"synthetic subtitle")
    snap = current_file_snapshot(sibling)
    action = UnpackExecutionAction(
        torrent_path="Pack/subtitle.srt",
        kind=UnpackExecutionActionKind.MATERIALIZE,
        length=snap.size,
        source_path=sibling.as_posix(),
        source_snapshot=snap,
    )
    plan = replace(f.plan, actions=(*f.plan.actions, action), plan_digest="")
    report = _inspect(f, plan=plan)
    assert report.status is ScopedEnvironmentStatus.METADATA_ONLY_NO_GRANT
    assert report.source_count == 2
    assert report.writes_authorized is False


def test_reuse_action_is_not_part_of_an_initial_copy_only_scope(tmp_path: Path) -> None:
    f = _fixture(tmp_path)
    first = f.plan.actions[0]
    action = replace(first, reuse_target_snapshot=first.source_snapshot)
    plan = replace(f.plan, actions=(action,), plan_digest="")
    _assert_blocked(f, ScopedEnvironmentStatus.BLOCKED_SOURCE, plan=plan)


def test_full_copy_bytes_must_fit_target_filesystem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = _fixture(tmp_path)
    monkeypatch.setattr(shutil, "disk_usage", lambda *_args: SimpleNamespace(free=5))
    _assert_blocked(f, ScopedEnvironmentStatus.BLOCKED_CAPACITY)


def test_source_output_ancestry_or_root_escape_rejected(tmp_path: Path) -> None:
    f = _fixture(tmp_path)
    _assert_blocked(f, ScopedEnvironmentStatus.BLOCKED_SCOPE, source_directory=f.parent)
    _assert_blocked(f, ScopedEnvironmentStatus.BLOCKED_SCOPE, source_directory=f.output)
    _assert_blocked(f, ScopedEnvironmentStatus.BLOCKED_SCOPE, source_directory=f.output / "source")
    _assert_blocked(
        f,
        ScopedEnvironmentStatus.BLOCKED_SCOPE,
        source_directory=tmp_path / "source" / ".." / "source",
    )


def test_output_replaced_after_initial_check_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = _fixture(tmp_path)
    disk_usage = shutil.disk_usage

    def replace_output(path: Path) -> object:
        usage = disk_usage(path)
        f.output.rename(f.parent / "displaced-isolated-root")
        f.output.mkdir()
        return usage

    monkeypatch.setattr(shutil, "disk_usage", replace_output)
    _assert_blocked(f, ScopedEnvironmentStatus.BLOCKED_OUTPUT_UNSAFE)


def test_unknown_file_created_mid_inspection_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = _fixture(tmp_path)
    disk_usage = shutil.disk_usage

    def create_conflict(path: Path) -> object:
        usage = disk_usage(path)
        (f.output / "unowned-file").write_bytes(b"synthetic")
        return usage

    monkeypatch.setattr(shutil, "disk_usage", create_conflict)
    _assert_blocked(f, ScopedEnvironmentStatus.BLOCKED_OUTPUT_UNSAFE)


def test_source_changes_mid_inspection_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = _fixture(tmp_path)
    snapshot = current_file_snapshot
    calls = 0

    def change_after_initial_snapshot(path: Path) -> object:
        nonlocal calls
        result = snapshot(path)
        calls += 1
        if calls == 1:
            f.source_file.write_bytes(b"modified while auditing")
        return result

    monkeypatch.setattr(environment, "current_file_snapshot", change_after_initial_snapshot)
    _assert_blocked(f, ScopedEnvironmentStatus.BLOCKED_SOURCE)
    assert calls >= 2


def test_root_status_does_not_imply_host_authorization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = _fixture(tmp_path)
    monkeypatch.setattr(shutil, "disk_usage", lambda *_args: SimpleNamespace(free=10**12))
    result = _inspect(f)
    assert result.status is ScopedEnvironmentStatus.METADATA_ONLY_NO_GRANT
    assert result.writes_authorized is False
    assert result.descriptor_bound_write_permission is False
