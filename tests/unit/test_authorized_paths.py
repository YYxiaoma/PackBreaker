from pathlib import Path

import pytest

from backend.app.domain.errors import DomainViolation
from backend.app.infrastructure.authorized_paths import AuthorizedPathScope


def _mountinfo_line(mountpoint: Path, *, mount_id: int) -> str:
    return f"{mount_id} 1 0:1 / {mountpoint.as_posix()} rw,relatime - ext4 /dev/synthetic rw"


def test_runtime_scope_discovers_explicit_directory_mounts_and_excludes_config(
    tmp_path: Path,
) -> None:
    downloads = tmp_path / "downloads"
    downloads2 = tmp_path / "downloads2"
    config = tmp_path / "config"
    for path in (downloads, downloads2, config):
        path.mkdir()
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        "\n".join(
            (
                _mountinfo_line(downloads, mount_id=10),
                _mountinfo_line(downloads2, mount_id=11),
                _mountinfo_line(config, mount_id=12),
            )
        ),
        encoding="utf-8",
    )

    scope = AuthorizedPathScope.from_runtime(
        legacy_data_root=tmp_path / "legacy-data",
        config_dir=config,
        mountinfo_path=mountinfo,
    )

    assert downloads in scope.authorized_roots
    assert downloads2 in scope.authorized_roots
    assert config not in scope.authorized_roots


def test_runtime_scope_excludes_default_tmp_mount(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(_mountinfo_line(Path("/tmp"), mount_id=20), encoding="utf-8")

    scope = AuthorizedPathScope.from_runtime(
        legacy_data_root=tmp_path / "legacy-data",
        config_dir=config,
        mountinfo_path=mountinfo,
    )

    assert Path("/tmp") not in scope.authorized_roots


def test_scope_accepts_absolute_authorized_path_and_rejects_unmounted_path(tmp_path: Path) -> None:
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    movie = downloads / "movies"
    movie.mkdir()
    scope = AuthorizedPathScope(
        legacy_data_root=tmp_path / "data",
        config_dir=tmp_path / "config",
        authorized_roots=(downloads,),
    )

    reference, resolved = scope.resolve_existing_directory(movie.as_posix())
    assert reference == movie.as_posix()
    assert resolved == movie

    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(DomainViolation, match="显式挂载"):
        scope.resolve_existing_directory(outside.as_posix())


def test_scope_preserves_legacy_relative_paths_under_data_root(tmp_path: Path) -> None:
    data = tmp_path / "data"
    source = data / "downloads"
    source.mkdir(parents=True)
    scope = AuthorizedPathScope.legacy_only(legacy_data_root=data)

    reference, resolved = scope.resolve_existing_directory("downloads")
    assert reference == source.as_posix()
    assert resolved == source


def test_scope_rejects_symlink_escape_inside_authorized_mount(tmp_path: Path) -> None:
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (downloads / "escape").symlink_to(outside, target_is_directory=True)
    scope = AuthorizedPathScope(
        legacy_data_root=tmp_path / "data",
        config_dir=tmp_path / "config",
        authorized_roots=(downloads,),
    )

    with pytest.raises(DomainViolation, match="符号链接"):
        scope.resolve_existing_directory((downloads / "escape").as_posix())


def test_root_browser_lists_only_authorized_mount_roots(tmp_path: Path) -> None:
    downloads = tmp_path / "downloads"
    downloads2 = tmp_path / "downloads2"
    downloads.mkdir()
    downloads2.mkdir()
    scope = AuthorizedPathScope(
        legacy_data_root=tmp_path / "data",
        config_dir=tmp_path / "config",
        authorized_roots=(downloads, downloads2),
    )

    current, entries = scope.browse_directories("/")
    assert current == "/"
    assert {entry.path for entry in entries} == {downloads.as_posix(), downloads2.as_posix()}


def test_output_anchor_allows_missing_tail_but_not_unmounted_parent(tmp_path: Path) -> None:
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    scope = AuthorizedPathScope(
        legacy_data_root=tmp_path / "data",
        config_dir=tmp_path / "config",
        authorized_roots=(downloads,),
    )

    reference, anchor, exists = scope.resolve_output_anchor(
        (downloads / "new" / "movies").as_posix()
    )
    assert reference == (downloads / "new" / "movies").as_posix()
    assert anchor == downloads
    assert exists is False

    with pytest.raises(DomainViolation):
        scope.resolve_output_anchor((tmp_path / "outside" / "movies").as_posix())
