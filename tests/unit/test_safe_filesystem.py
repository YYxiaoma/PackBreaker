import os
from pathlib import Path

import pytest

from backend.app.domain.errors import DomainViolation, ErrorCode
from backend.app.domain.verification import FileSnapshot
from backend.app.infrastructure.safe_filesystem import SafeFilesystemGateway


def _snapshot(path: Path) -> FileSnapshot:
    result = path.stat(follow_symlinks=False)
    return FileSnapshot(
        device=result.st_dev,
        inode=result.st_ino,
        size=result.st_size,
        mtime_ns=result.st_mtime_ns,
    )


def test_hardlink_inspection_is_read_only_and_reports_missing_directories(tmp_path: Path) -> None:
    source = tmp_path / "source" / "movie.mkv"
    target_root = tmp_path / "target"
    source.parent.mkdir()
    target_root.mkdir()
    source.write_bytes(b"synthetic")
    before = source.stat(follow_symlinks=False)

    result = SafeFilesystemGateway(tmp_path).inspect_hardlink(
        source_relative_path="source/movie.mkv",
        target_root_relative_path="target",
        target_relative_path="Pack/Season 01/movie.mkv",
        expected_source_snapshot=_snapshot(source),
    )

    after = source.stat(follow_symlinks=False)
    assert result.missing_directories == ("Pack", "Pack/Season 01")
    assert result.nearest_target_parent_relative_path == "."
    assert result.source_snapshot.inode == before.st_ino == after.st_ino
    assert result.source_snapshot.link_count == before.st_nlink == after.st_nlink
    assert not (target_root / "Pack").exists()


def test_hardlink_inspection_rejects_changed_source_snapshot(tmp_path: Path) -> None:
    source = tmp_path / "movie.mkv"
    target = tmp_path / "target"
    source.write_bytes(b"first")
    target.mkdir()
    expected = _snapshot(source)
    source.write_bytes(b"second-content")

    with pytest.raises(DomainViolation) as failure:
        SafeFilesystemGateway(tmp_path).inspect_hardlink(
            source_relative_path="movie.mkv",
            target_root_relative_path="target",
            target_relative_path="movie.mkv",
            expected_source_snapshot=expected,
        )

    assert failure.value.code is ErrorCode.SOURCE_CHANGED


def test_hardlink_inspection_rejects_source_symlink(tmp_path: Path) -> None:
    real = tmp_path / "real.mkv"
    real.write_bytes(b"data")
    link = tmp_path / "link.mkv"
    link.symlink_to(real)
    (tmp_path / "target").mkdir()

    with pytest.raises(DomainViolation) as failure:
        SafeFilesystemGateway(tmp_path).inspect_hardlink(
            source_relative_path="link.mkv",
            target_root_relative_path="target",
            target_relative_path="movie.mkv",
            expected_source_snapshot=_snapshot(real),
        )

    assert failure.value.code is ErrorCode.PATH_MAPPING_INVALID


def test_hardlink_inspection_rejects_symlink_in_target_parent(tmp_path: Path) -> None:
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"data")
    target = tmp_path / "target"
    outside = tmp_path / "outside"
    target.mkdir()
    outside.mkdir()
    (target / "linked").symlink_to(outside, target_is_directory=True)

    with pytest.raises(DomainViolation) as failure:
        SafeFilesystemGateway(tmp_path).inspect_hardlink(
            source_relative_path="movie.mkv",
            target_root_relative_path="target",
            target_relative_path="linked/movie.mkv",
            expected_source_snapshot=_snapshot(source),
        )

    assert failure.value.code is ErrorCode.PATH_MAPPING_INVALID


def test_hardlink_inspection_rejects_existing_target(tmp_path: Path) -> None:
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"data")
    target = tmp_path / "target"
    target.mkdir()
    (target / "movie.mkv").write_bytes(b"existing")

    with pytest.raises(DomainViolation) as failure:
        SafeFilesystemGateway(tmp_path).inspect_hardlink(
            source_relative_path="movie.mkv",
            target_root_relative_path="target",
            target_relative_path="movie.mkv",
            expected_source_snapshot=_snapshot(source),
        )

    assert failure.value.code is ErrorCode.TARGET_CONFLICT


@pytest.mark.parametrize(
    "unsafe_path",
    ["../escape.mkv", "/absolute.mkv", "C:/movie.mkv", "a//movie.mkv", "a\\movie.mkv"],
)
def test_hardlink_inspection_rejects_unsafe_relative_paths(
    tmp_path: Path,
    unsafe_path: str,
) -> None:
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"data")
    (tmp_path / "target").mkdir()

    with pytest.raises(DomainViolation) as failure:
        SafeFilesystemGateway(tmp_path).inspect_hardlink(
            source_relative_path="movie.mkv",
            target_root_relative_path="target",
            target_relative_path=unsafe_path,
            expected_source_snapshot=_snapshot(source),
        )

    assert failure.value.code is ErrorCode.PATH_MAPPING_INVALID


def test_hardlink_inspection_uses_nearest_existing_parent_snapshot(tmp_path: Path) -> None:
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"data")
    parent = tmp_path / "target" / "Pack"
    parent.mkdir(parents=True)

    result = SafeFilesystemGateway(tmp_path).inspect_hardlink(
        source_relative_path="movie.mkv",
        target_root_relative_path="target",
        target_relative_path="Pack/Season/movie.mkv",
        expected_source_snapshot=_snapshot(source),
    )

    actual = os.stat(parent, follow_symlinks=False)
    assert result.nearest_target_parent_relative_path == "Pack"
    assert result.target_parent_snapshot.inode == actual.st_ino
    assert result.missing_directories == ("Pack/Season",)
