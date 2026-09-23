import os
import socket
from pathlib import Path

import pytest

import backend.app.container_entrypoint as entrypoint
from backend.app.container_entrypoint import (
    _chown_config_tree,
    _docker_socket_supplementary_groups,
    _numeric_id,
)


def test_numeric_ids_allow_root_and_reject_negative_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PUID", "1234")
    assert _numeric_id("PUID", 1000) == 1234

    monkeypatch.setenv("PUID", "0")
    assert _numeric_id("PUID", 1000) == 0

    monkeypatch.setenv("PGID", "0")
    assert _numeric_id("PGID", 1000) == 0

    monkeypatch.setenv("PUID", "-1")
    with pytest.raises(RuntimeError, match="非负整数"):
        _numeric_id("PUID", 1000)

    monkeypatch.setenv("PUID", "not-a-number")
    with pytest.raises(RuntimeError, match="非负整数"):
        _numeric_id("PUID", 1000)


def test_config_permission_bootstrap_never_follows_symlinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config"
    config.mkdir(mode=0o755)
    outside = tmp_path / "outside"
    outside.write_text("synthetic", encoding="utf-8")
    link = config / "outside-link"
    link.symlink_to(outside)
    calls: list[tuple[Path, bool]] = []

    def record_chown(
        path: os.PathLike[str] | str,
        _uid: int,
        _gid: int,
        *,
        follow_symlinks: bool = True,
    ) -> None:
        calls.append((Path(path), follow_symlinks))

    monkeypatch.setattr(os, "chown", record_chown)
    _chown_config_tree(config, 1000, 1000)

    assert config.stat().st_mode & 0o777 == 0o700
    assert (link, False) in calls
    assert all(path != outside for path, _follow in calls)


def test_docker_socket_group_survives_privilege_drop_only_for_explicit_unix_socket(
    tmp_path: Path,
) -> None:
    docker_socket = tmp_path / "docker.sock"
    assert _docker_socket_supplementary_groups(1026, 100, docker_socket) == []
    docker_socket.touch()
    docker_socket.chmod(0o660)
    assert _docker_socket_supplementary_groups(1026, 100, docker_socket) == []
    docker_socket.unlink()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(docker_socket))
        docker_socket.chmod(0o660)
        socket_group = docker_socket.stat().st_gid
        expected = [] if socket_group == 100 else [socket_group]
        assert _docker_socket_supplementary_groups(1026, 100, docker_socket) == expected
        docker_socket.chmod(0o666)
        assert _docker_socket_supplementary_groups(1026, 100, docker_socket) == []
        docker_socket.chmod(0o660)
        assert _docker_socket_supplementary_groups(1026, socket_group, docker_socket) == []
        assert _docker_socket_supplementary_groups(0, 100, docker_socket) == []
        docker_socket.chmod(0o600)
        assert _docker_socket_supplementary_groups(1026, 100, docker_socket) == []


def test_privilege_drop_keeps_only_explicit_socket_group_and_retains_puid_pgid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setenv("PACKBREAKER_CONFIG_DIR", str(config))
    monkeypatch.setenv("PUID", "1026")
    monkeypatch.setenv("PGID", "100")
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(entrypoint, "_chown_config_tree", lambda *_args: None)
    monkeypatch.setattr(entrypoint, "_docker_socket_supplementary_groups", lambda uid, gid: [998])
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(os, "setgroups", lambda v: calls.append(("groups", v)))
    monkeypatch.setattr(os, "setgid", lambda v: calls.append(("gid", v)))
    monkeypatch.setattr(os, "setuid", lambda v: calls.append(("uid", v)))
    entrypoint._drop_privileges_if_needed()
    assert calls == [("groups", [998]), ("gid", 100), ("uid", 1026)]


def test_unmapped_socket_gid_does_not_prevent_web_service_privilege_drop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PACKBREAKER_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("PUID", "1026")
    monkeypatch.setenv("PGID", "100")
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(entrypoint, "_chown_config_tree", lambda *_args: None)
    monkeypatch.setattr(entrypoint, "_docker_socket_supplementary_groups", lambda uid, gid: [998])
    calls: list[tuple[str, object]] = []

    def setgroups(values: list[int]) -> None:
        calls.append(("groups", values))
        if values:
            raise PermissionError("synthetic unmapped GID")

    monkeypatch.setattr(os, "setgroups", setgroups)
    monkeypatch.setattr(os, "setgid", lambda v: calls.append(("gid", v)))
    monkeypatch.setattr(os, "setuid", lambda v: calls.append(("uid", v)))
    entrypoint._drop_privileges_if_needed()
    assert calls == [
        ("groups", [998]),
        ("groups", []),
        ("gid", 100),
        ("uid", 1026),
    ]
