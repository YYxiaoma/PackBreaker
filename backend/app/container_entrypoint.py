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


def _validate_non_root_runtime(config_dir: Path, uid: int, gid: int) -> None:
    current_uid = os.geteuid()
    current_gid = os.getegid()
    if current_uid != uid or current_gid != gid:
        raise RuntimeError(
            "容器当前运行身份与 PUID/PGID 不一致："
            f"当前 {current_uid}:{current_gid}，配置 {uid}:{gid}；"
            '请在 Compose 中使用与 PUID/PGID 相同的 user: "UID:GID"'
        )
    try:
        config_stat = config_dir.stat(follow_symlinks=False)
    except OSError as exc:
        raise RuntimeError(
            f"配置目录 {config_dir} 不可访问；请先在宿主机创建目录并将所有权调整为 {uid}:{gid}"
        ) from exc
    if stat.S_ISLNK(config_stat.st_mode) or not stat.S_ISDIR(config_stat.st_mode):
        raise RuntimeError(f"配置目录 {config_dir} 必须是真实目录且不能是符号链接")
    if not os.access(config_dir, os.R_OK | os.W_OK | os.X_OK):
        raise RuntimeError(
            f"配置目录 {config_dir} 对运行用户 {uid}:{gid} 不可读写；"
            f"请在宿主机调整目录所有权/权限，例如 chown -R {uid}:{gid} <config-directory>"
        )
    critical_paths = (
        ("目录", config_dir / "logs", os.R_OK | os.W_OK | os.X_OK),
        ("目录", config_dir / "backups", os.R_OK | os.W_OK | os.X_OK),
        ("目录", config_dir / "updater", os.R_OK | os.W_OK | os.X_OK),
        ("文件", config_dir / "packbreaker.db", os.R_OK | os.W_OK),
        ("文件", config_dir / "packbreaker.db-wal", os.R_OK | os.W_OK),
        ("文件", config_dir / "packbreaker.db-shm", os.R_OK | os.W_OK),
        ("文件", config_dir / "packbreaker.lock", os.R_OK | os.W_OK),
        ("文件", config_dir / "secret.key", os.R_OK | os.W_OK),
    )
    for kind, path, access_mode in critical_paths:
        try:
            item_stat = path.stat(follow_symlinks=False)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise RuntimeError(
                f"配置{kind} {path} 无法安全检查；"
                f"请在宿主机执行 chown -R {uid}:{gid} <config-directory>"
            ) from exc
        expected_type = stat.S_ISDIR if kind == "目录" else stat.S_ISREG
        if stat.S_ISLNK(item_stat.st_mode) or not expected_type(item_stat.st_mode):
            raise RuntimeError(f"配置{kind} {path} 类型异常或为符号链接，拒绝启动")
        if not os.access(path, access_mode):
            raise RuntimeError(
                f"配置{kind} {path} 对运行用户 {uid}:{gid} 权限不足；"
                f"请在宿主机执行 chown -R {uid}:{gid} <config-directory>"
            )


def _drop_privileges_if_needed() -> None:
    config_dir = Path(os.environ.get("PACKBREAKER_CONFIG_DIR", "/config"))
    current_uid = os.geteuid()
    if current_uid != 0:
        uid = _numeric_id("PUID", current_uid)
        gid = _numeric_id("PGID", os.getegid())
        _validate_non_root_runtime(config_dir, uid, gid)
        return
    uid = _numeric_id("PUID", 1000)
    gid = _numeric_id("PGID", 1000)
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
