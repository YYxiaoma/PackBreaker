from __future__ import annotations

import os
from pathlib import Path


def _numeric_id(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} 必须是正整数") from exc
    if value < 1:
        raise RuntimeError(f"{name} 必须是正整数")
    return value


def _chown_config_tree(config_dir: Path, uid: int, gid: int) -> None:
    config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    for root, directories, files in os.walk(config_dir, topdown=False, followlinks=False):
        for name in (*directories, *files):
            os.chown(Path(root) / name, uid, gid, follow_symlinks=False)
        os.chown(root, uid, gid, follow_symlinks=False)


def _drop_privileges_if_needed() -> None:
    if os.geteuid() != 0:
        return
    uid = _numeric_id("PUID", 1000)
    gid = _numeric_id("PGID", 1000)
    config_dir = Path(os.environ.get("PACKBREAKER_CONFIG_DIR", "/config"))
    _chown_config_tree(config_dir, uid, gid)
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)


def main() -> None:
    _drop_privileges_if_needed()
    from backend.app.server import main as serve

    serve()


if __name__ == "__main__":
    main()
