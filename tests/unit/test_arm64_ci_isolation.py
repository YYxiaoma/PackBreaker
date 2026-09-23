"""Offline isolation and failure-cleanup checks for the native ARM64 CI smoke."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml  # type: ignore[import-untyped]

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
STEP = "Exercise native ARM64 container startup and backup"


def _smoke() -> str:
    jobs = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]
    script = next(item["run"] for item in jobs["arm64"]["steps"] if item.get("name") == STEP)
    if not isinstance(script, str):
        raise TypeError("Native ARM64 CI smoke must be a shell script")
    return script


def test_arm64_ci_smoke_uses_private_sandbox_and_no_external_network_or_logs() -> None:
    script = _smoke()
    assert 'mktemp -d "${TMPDIR:-/tmp}/.packbreaker-arm64-ci.XXXXXXXX"' in script
    assert "trap cleanup EXIT" in script
    assert '"$sandbox/config:/config"' in script
    assert '"$sandbox/data:/data"' in script
    assert 'docker run --detach --name "$container"' in script
    assert script.count("docker run") == script.count("--network none") == 3
    assert "--publish" not in script
    assert "--network host" not in script
    assert "docker logs" not in script
    assert "18001" not in script
    assert ".ci-arm64-config" not in script and ".ci-arm64-data" not in script
    assert '"$(basename "$sandbox")" == .packbreaker-arm64-ci.*' in script
    assert 'rm -rf -- "$sandbox"' in script
    assert "ready=0" in script
    assert script.count('if [[ "$ready" -ne 1 ]]') == 2


def test_arm64_ci_failed_container_start_preserves_preexisting_directory(tmp_path: Path) -> None:
    preserved = tmp_path / ".packbreaker-arm64-ci.preserved"
    preserved.mkdir()
    sentinel = preserved / "user-data"
    sentinel.write_text("keep", encoding="utf-8")
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    uname = fake_bin / "uname"
    uname.write_text("#!/bin/sh\nprintf 'aarch64\\n'\n", encoding="utf-8")
    uname.chmod(0o755)
    docker = fake_bin / "docker"
    docker.write_text(
        '#!/bin/sh\ncase "$1" in\n'
        "  rm) exit 0;;\n"
        "  run) exit 88;;\n"
        '  logs) printf "SYNTHETIC-SECRET-DO-NOT-PRINT\\n";;\n'
        "  *) exit 89;;\n"
        "esac\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    result = subprocess.run(
        ["bash", "-c", _smoke()],
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
            "TMPDIR": str(tmp_path),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert result.returncode == 88
    assert "SYNTHETIC-SECRET-DO-NOT-PRINT" not in result.stdout + result.stderr
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert list(tmp_path.glob(".packbreaker-arm64-ci.*")) == [preserved]
