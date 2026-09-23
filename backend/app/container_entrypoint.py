from __future__ import annotations

import os
import stat
from pathlib import Path


def _numeric_id(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} 必须是非负整数") from exc
    if value < 0:
        raise RuntimeError(f"{name} 必须是非负整数")
    return value


def _chown_config_tree(config_dir: Path, uid: int, gid: int) -> None:
    config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(config_dir, 0o700, follow_symlinks=False)
    for root, directories, files in os.walk(config_dir, topdown=False, followlinks=False):
        for name in (*directories, *files):
            os.chown(Path(root) / name, uid, gid, follow_symlinks=False)
        os.chown(root, uid, gid, follow_symlinks=False)


def _docker_socket_supplementary_groups(
    uid: int, gid: int, docker_socket: Path = Path("/var/run/docker.sock")
) -> list[int]:
    """Retain only a mounted Unix Docker socket's group during privilege drop."""
    try:
        socket_stat = docker_socket.stat()
    except OSError:
        return []
    if (
        not stat.S_ISSOCK(socket_stat.st_mode)
        or uid == 0
        or socket_stat.st_gid == gid
        # Linux uses the owner permission class before any supplementary
        # group permissions. Socket group membership cannot help its owner.
        or socket_stat.st_uid == uid
        or (socket_stat.st_mode & stat.S_IROTH and socket_stat.st_mode & stat.S_IWOTH)
    ):
        return []
    if socket_stat.st_mode & stat.S_IRGRP and socket_stat.st_mode & stat.S_IWGRP:
        return [socket_stat.st_gid]
    return []


def _drop_privileges_if_needed() -> None:
    if os.geteuid() != 0:
        return
    uid = _numeric_id("PUID", 1000)
    gid = _numeric_id("PGID", 1000)
    config_dir = Path(os.environ.get("PACKBREAKER_CONFIG_DIR", "/config"))
    _chown_config_tree(config_dir, uid, gid)
    # Compose's user: "0:0" only applies to this entrypoint. The application
    # subsequently drops to PUID/PGID, so blindly clearing supplementary groups
    # removes access to a mounted docker.sock with mode 0660 and a different GID.
    # Retain *only* the socket's numeric group, and only if a real Unix socket
    # explicitly exists and its group has read/write access. Never chmod/chown
    # the host-managed socket or retain all root supplementary groups.
    socket_groups = _docker_socket_supplementary_groups(uid, gid)
    try:
        os.setgroups(socket_groups)
    except OSError:
        if not socket_groups:
            raise
        # Some user-namespace mappings reject the socket's host GID. Preserve
        # the previous safe startup behavior instead of taking down the Web
        # service; the updater's actual Docker connection check stays closed.
        os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)


def main() -> None:
    _drop_privileges_if_needed()
    from backend.app.server import main as serve

    serve()


if __name__ == "__main__":
    main()
