"""Regression tests: symlink mapping roots and outbound host guard."""

from pathlib import Path

import pytest

from backend.app.domain.downloader import (
    PathMappingRule,
    map_remote_path,
    normalize_path_mappings,
)
from backend.app.domain.errors import DomainViolation


def test_symlinked_mapping_root_is_rejected(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    rules = [
        PathMappingRule(remote_prefix="/downloads", container_prefix=str(link)),
    ]
    with pytest.raises(DomainViolation):
        normalize_path_mappings(rules)


def test_symlinked_mapping_root_rejected_at_map_time(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    rule = PathMappingRule(remote_prefix="/downloads", container_prefix=str(link))
    with pytest.raises(DomainViolation):
        map_remote_path("/downloads/movie.mkv", [rule])
