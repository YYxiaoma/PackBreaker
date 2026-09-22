"""Offline safety checks for the candidate updater Docker acceptance harness."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check-updater-e2e.sh"


def test_updater_e2e_owns_one_private_sandbox_and_never_prints_raw_container_logs() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert 'mktemp -d "${TMPDIR:-/tmp}/.packbreaker-updater-e2e.XXXXXXXX"' in source
    assert '"$(basename "$sandbox")" == .packbreaker-updater-e2e.*' in source
    assert 'sudo rm -rf -- "$sandbox"' in source
    for variable in (
        "fault_build_dir",
        "success_config",
        "success_data",
        "rollback_config",
        "rollback_data",
        "transient_config",
        "transient_data",
    ):
        assert f'{variable}="$sandbox/' in source
        assert (
            f'"${variable}"'
            not in source.split("cleanup() {", 1)[1].split("workflow_escape() {", 1)[0]
        )
    assert "docker logs" not in source
    assert 'docker inspect "$container" || true' not in source


def test_failed_baseline_pull_does_not_touch_preexisting_updater_data(tmp_path: Path) -> None:
    preserved = tmp_path / ".packbreaker-updater-e2e.preserved"
    preserved.mkdir()
    sentinel = preserved / "do-not-remove"
    sentinel.write_text("keep", encoding="utf-8")

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    docker.write_text(
        '#!/bin/sh\ncase "$1" in\n  pull) exit 88;;\n  ps|rm) exit 0;;\n  *) exit 89;;\nesac\n',
        encoding="utf-8",
    )
    docker.chmod(0o755)
    sudo = fake_bin / "sudo"
    sudo.write_text('#!/bin/sh\nexec "$@"\n', encoding="utf-8")
    sudo.chmod(0o755)

    result = subprocess.run(
        ["bash", str(SCRIPT), "packbreaker:synthetic-candidate"],
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
            "TMPDIR": str(tmp_path),
        },
        text=True,
        capture_output=True,
        check=False,
        timeout=25,
    )
    assert result.returncode != 0
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert list(tmp_path.glob(".packbreaker-updater-e2e.*")) == [preserved]
