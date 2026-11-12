from pathlib import Path

import pytest

from backend.app.domain.errors import DomainViolation
from backend.app.infrastructure import authorized_paths
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


def test_workspace_and_ssh_mounts_are_not_authorized_but_nested_media_mounts_are(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    downloads = workspace / "PackBreaker" / "runtime" / "data" / "downloads"
    downloads2 = workspace / "PackBreaker" / "runtime" / "data" / "downloads2"
    ssh = tmp_path / "ssh"
    config = tmp_path / "config"
    downloads.mkdir(parents=True)
    downloads2.mkdir()
    ssh.mkdir()
    config.mkdir()

    assert Path("/workspace") in authorized_paths._PROTECTED_EXACT_ROOTS
    assert Path("/ssh") in authorized_paths._PROTECTED_EXACT_ROOTS
    monkeypatch.setattr(
        authorized_paths,
        "_PROTECTED_EXACT_ROOTS",
        (*authorized_paths._PROTECTED_EXACT_ROOTS, workspace, ssh),
    )
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        "\n".join(
            (
                _mountinfo_line(workspace, mount_id=30),
                _mountinfo_line(downloads, mount_id=31),
                _mountinfo_line(downloads2, mount_id=32),
                _mountinfo_line(ssh, mount_id=33),
                _mountinfo_line(config, mount_id=34),
            )
        ),
        encoding="utf-8",
    )

    scope = AuthorizedPathScope.from_runtime(
        legacy_data_root=tmp_path / "legacy-data",
        config_dir=config,
        mountinfo_path=mountinfo,
    )

    assert workspace not in scope.authorized_roots
    assert ssh not in scope.authorized_roots
    assert downloads in scope.authorized_roots
    assert downloads2 in scope.authorized_roots
    current, entries = scope.browse_directories(workspace.as_posix())
    assert current == workspace.as_posix()
    assert {entry.path for entry in entries} == {(workspace / "PackBreaker").as_posix()}
    with pytest.raises(DomainViolation, match="显式挂载"):
        scope.resolve_existing_directory(workspace.as_posix())
    with pytest.raises(DomainViolation, match="显式挂载"):
        scope.resolve_existing_directory(ssh.as_posix())


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


def test_directory_browser_can_read_outside_operation_roots_without_authorizing_them(
    tmp_path: Path,
) -> None:
    downloads = tmp_path / "downloads"
    outside = tmp_path / "outside"
    nested = outside / "nested"
    downloads.mkdir()
    nested.mkdir(parents=True)
    scope = AuthorizedPathScope(
        legacy_data_root=tmp_path / "data",
        config_dir=tmp_path / "config",
        authorized_roots=(downloads,),
    )

    current, entries = scope.browse_directories(outside.as_posix())
    assert current == outside.as_posix()
    assert {entry.path for entry in entries} == {nested.as_posix()}

    with pytest.raises(DomainViolation, match="显式挂载"):
        scope.resolve_existing_directory(outside.as_posix())


def test_directory_browser_rejects_symlink_traversal_outside_operation_roots(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "outside-link"
    link.symlink_to(outside, target_is_directory=True)
    scope = AuthorizedPathScope(
        legacy_data_root=tmp_path / "data",
        config_dir=tmp_path / "config",
        authorized_roots=(),
    )

    with pytest.raises(DomainViolation, match="符号链接"):
        scope.browse_directories(link.as_posix())


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
