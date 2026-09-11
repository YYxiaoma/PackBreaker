from pathlib import Path

import pytest

from backend.app.infrastructure.source_inventory import scan_source_inventory


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
