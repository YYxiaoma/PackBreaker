from pathlib import Path

import pytest

from backend.app.infrastructure.source_inventory import (
    scan_source_inventory,
    scan_source_inventory_page,
)


def test_source_inventory_cooperatively_checks_cancel_during_directory_walk(tmp_path: Path) -> None:
    root = tmp_path / "source"
    nested = root / "nested"
    nested.mkdir(parents=True)
    (root / "root.bin").write_bytes(b"root")
    (nested / "nested.bin").write_bytes(b"nested")
    calls = 0

    class Cancelled(RuntimeError):
        pass

    def cancel_check() -> None:
        nonlocal calls
        calls += 1
        if calls == 5:
            raise Cancelled("synthetic inventory cancel")

    with pytest.raises(Cancelled, match="synthetic inventory cancel"):
        scan_source_inventory(root, cancel_check=cancel_check)
    assert calls == 5


def test_source_inventory_page_uses_stable_cursor_across_nested_directories(tmp_path: Path) -> None:
    root = tmp_path / "source"
    nested = root / "nested"
    nested.mkdir(parents=True)
    (root / "a.bin").write_bytes(b"a")
    (nested / "b.bin").write_bytes(b"b")
    (nested / "c.bin").write_bytes(b"c")
    (root / "z.bin").write_bytes(b"z")

    first = scan_source_inventory_page(root, after=None, limit=2)
    assert [item.relative_path for item in first.candidates] == ["a.bin", "nested/b.bin"]
    assert first.has_more is True
    assert first.next_cursor == "nested/b.bin"

    second = scan_source_inventory_page(root, after=first.next_cursor, limit=2)
    assert [item.relative_path for item in second.candidates] == ["nested/c.bin", "z.bin"]
    assert second.has_more is False
    assert second.next_cursor == "z.bin"


def test_source_inventory_page_resumes_after_cursor_without_rewalking_prior_subtree(
    tmp_path: Path,
) -> None:
    root = tmp_path / "source"
    old = root / "a-old"
    current = root / "m-current"
    old.mkdir(parents=True)
    current.mkdir(parents=True)
    for index in range(5):
        (old / f"old-{index}.bin").write_bytes(b"old")
    (current / "one.bin").write_bytes(b"1")
    (current / "two.bin").write_bytes(b"2")
    (root / "z.bin").write_bytes(b"z")

    page = scan_source_inventory_page(root, after="m-current/one.bin", limit=2)
    assert [item.relative_path for item in page.candidates] == ["m-current/two.bin", "z.bin"]
    assert page.has_more is False
