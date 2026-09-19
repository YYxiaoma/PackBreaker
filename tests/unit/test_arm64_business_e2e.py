from __future__ import annotations

from pathlib import Path

import pytest

from scripts.check_arm64_business_e2e import run_business_probe


def test_isolated_business_probe_verifies_three_torrent_kinds_without_retaining_files(
    tmp_path: Path,
) -> None:
    assert set(run_business_probe(tmp_path)) == {"v1", "v2", "hybrid"}
    assert list(tmp_path.iterdir()) == []


def test_business_probe_rejects_symlink_root(tmp_path: Path) -> None:
    external = tmp_path / "actual"
    external.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(external, target_is_directory=True)
    with pytest.raises(ValueError, match="非符号链接"):
        run_business_probe(linked)
    assert list(external.iterdir()) == []
