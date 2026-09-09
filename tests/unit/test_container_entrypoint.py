import os
from pathlib import Path

import pytest

from backend.app.container_entrypoint import _chown_config_tree, _numeric_id


def test_numeric_ids_must_be_positive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PUID", "1234")
    assert _numeric_id("PUID", 1000) == 1234

    monkeypatch.setenv("PUID", "0")
    with pytest.raises(RuntimeError, match="正整数"):
        _numeric_id("PUID", 1000)

    monkeypatch.setenv("PUID", "not-a-number")
    with pytest.raises(RuntimeError, match="正整数"):
        _numeric_id("PUID", 1000)


def test_config_permission_bootstrap_never_follows_symlinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config"
    config.mkdir()
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

    assert (link, False) in calls
    assert all(path != outside for path, _follow in calls)
