import os
from pathlib import Path

import pytest

from backend.app.infrastructure.history_scanner import HistoryFilesystemScanner


def test_history_scanner_page_matches_full_scan_order_and_has_more(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    root = data_root / "library"
    (root / "A" / "Season 01").mkdir(parents=True)
    (root / "B").mkdir()
    (root / "A" / "Season 01" / "01.mkv").write_bytes(b"1")
    (root / "A" / "Season 01" / "02.mkv").write_bytes(b"2")
    (root / "B" / "01.mkv").write_bytes(b"3")
    (root / "z.mkv").write_bytes(b"4")
    (root / "a").mkdir()
    (root / "a" / "child.mkv").write_bytes(b"5")
    (root / "a!.mkv").write_bytes(b"6")

    scanner = HistoryFilesystemScanner(data_root)
    full = scanner.scan(
        root_relative_path="library",
        extensions=(".mkv",),
        exclude_patterns=(),
    )
    first = scanner.scan_page(
        root_relative_path="library",
        extensions=(".mkv",),
        exclude_patterns=(),
        after=None,
        limit=3,
    )
    second = scanner.scan_page(
        root_relative_path="library",
        extensions=(".mkv",),
        exclude_patterns=(),
        after=first.snapshots[-1].relative_path,
        limit=3,
    )

    assert first.has_more is True
    assert second.has_more is False
    assert first.snapshots + second.snapshots == full


def test_history_scanner_page_prunes_subtrees_before_cursor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_root = tmp_path / "data"
    root = data_root / "library"
    old = root / "A" / "Season 01"
    current = root / "B" / "Season 01"
    old.mkdir(parents=True)
    current.mkdir(parents=True)
    for index in range(30):
        (old / f"{index:02d}.mkv").write_bytes(b"old")
    (current / "01.mkv").write_bytes(b"one")
    (current / "02.mkv").write_bytes(b"two")

    scanner = HistoryFilesystemScanner(data_root)
    real_stat_relative = scanner._stat_relative
    observed_paths: list[str] = []

    def counting_stat(root_fd: int, relative_path: str) -> os.stat_result:
        observed_paths.append(relative_path)
        return real_stat_relative(root_fd, relative_path)

    monkeypatch.setattr(scanner, "_stat_relative", counting_stat)
    page = scanner.scan_page(
        root_relative_path="library",
        extensions=(".mkv",),
        exclude_patterns=(),
        after="B/Season 01/01.mkv",
        limit=10,
    )

    assert tuple(item.relative_path for item in page.snapshots) == ("B/Season 01/02.mkv",)
    assert page.has_more is False
    assert not any(path == "A" or path.startswith("A/") for path in observed_paths)
    assert sum(path.endswith(".mkv") for path in observed_paths) <= 2


def test_history_scanner_page_does_not_follow_directory_symlink(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    root = data_root / "library"
    outside = data_root / "outside"
    root.mkdir(parents=True)
    outside.mkdir()
    (root / "inside.mkv").write_bytes(b"inside")
    (outside / "escaped.mkv").write_bytes(b"outside")
    (root / "linked").symlink_to(outside, target_is_directory=True)

    page = HistoryFilesystemScanner(data_root).scan_page(
        root_relative_path="library",
        extensions=(".mkv",),
        exclude_patterns=(),
        after=None,
        limit=10,
    )

    assert tuple(item.relative_path for item in page.snapshots) == ("inside.mkv",)
