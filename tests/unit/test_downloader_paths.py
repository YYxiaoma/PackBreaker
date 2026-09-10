from pathlib import Path

import pytest

from backend.app.domain.downloader import (
    PathMappingRule,
    map_remote_path,
    normalize_path_mappings,
    reverse_map_container_path,
    reverse_map_container_path_unique,
)
from backend.app.domain.errors import DomainViolation, ErrorCode


def test_longest_prefix_mapping_and_reverse_round_trip(tmp_path: Path) -> None:
    root = (tmp_path / "data").resolve()
    general = root / "downloads"
    movies = root / "movies"
    rules = normalize_path_mappings(
        [
            PathMappingRule("/remote", str(general)),
            PathMappingRule("/remote/movies", str(movies)),
        ],
        allowed_root=root,
    )

    matched = map_remote_path(
        "/remote/movies/Example/file.mkv",
        rules,
        allowed_root=root,
    )

    assert matched.rule_index == 1
    assert matched.container_path == movies / "Example" / "file.mkv"
    assert reverse_map_container_path(matched.container_path, rules[1]) == (
        "/remote/movies/Example/file.mkv"
    )


def test_windows_remote_mapping_is_case_insensitive(tmp_path: Path) -> None:
    root = (tmp_path / "data").resolve()
    rules = normalize_path_mappings(
        [PathMappingRule(r"D:\Downloads", str(root / "downloads"))],
        allowed_root=root,
    )

    matched = map_remote_path(
        r"d:\downloads\Movie\file.mkv",
        rules,
        allowed_root=root,
    )

    assert matched.container_path == root / "downloads" / "Movie" / "file.mkv"


def test_mapping_rejects_traversal_and_container_escape(tmp_path: Path) -> None:
    root = (tmp_path / "data").resolve()
    with pytest.raises(DomainViolation) as traversal:
        map_remote_path(
            "/remote/../outside.mkv",
            [PathMappingRule("/remote", str(root))],
            allowed_root=root,
        )
    assert traversal.value.code is ErrorCode.PATH_MAPPING_INVALID

    with pytest.raises(DomainViolation) as escape:
        normalize_path_mappings(
            [PathMappingRule("/remote", str(tmp_path / "outside"))],
            allowed_root=root,
        )
    assert escape.value.code is ErrorCode.PATH_MAPPING_INVALID


def test_equal_length_mapping_ambiguity_is_blocked(tmp_path: Path) -> None:
    root = (tmp_path / "data").resolve()
    rules = normalize_path_mappings(
        [
            PathMappingRule("/remote", str(root / "a")),
            PathMappingRule("/remote", str(root / "b")),
        ],
        allowed_root=root,
    )

    with pytest.raises(DomainViolation) as ambiguous:
        map_remote_path("/remote/file.mkv", rules, allowed_root=root)

    assert ambiguous.value.code is ErrorCode.MAPPING_AMBIGUOUS


def test_reverse_mapping_uses_unique_longest_container_prefix(tmp_path: Path) -> None:
    root = (tmp_path / "data").resolve()
    rules = normalize_path_mappings(
        [
            PathMappingRule("/downloads", str(root / "downloads")),
            PathMappingRule("/movies", str(root / "downloads" / "movies")),
        ],
        allowed_root=root,
    )

    remote = reverse_map_container_path_unique(
        root / "downloads" / "movies" / "Example",
        rules,
        allowed_root=root,
    )

    assert remote == "/movies/Example"


def test_reverse_mapping_rejects_path_outside_data_root(tmp_path: Path) -> None:
    root = (tmp_path / "data").resolve()
    rules = normalize_path_mappings(
        [PathMappingRule("/downloads", str(root / "downloads"))],
        allowed_root=root,
    )

    with pytest.raises(DomainViolation) as failure:
        reverse_map_container_path_unique(
            tmp_path / "outside",
            rules,
            allowed_root=root,
        )

    assert failure.value.code is ErrorCode.PATH_MAPPING_INVALID
