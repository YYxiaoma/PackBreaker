"""Keep the AMD64 CI runtime smoke isolated from the checkout and public ports."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml  # type: ignore[import-untyped]

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def _smoke() -> str:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    step = next(
        step
        for step in workflow["jobs"]["container"]["steps"]
        if step.get("name") == "Smoke test image and health endpoint"
    )
    script = step["run"]
    if not isinstance(script, str):
        raise TypeError("AMD64 CI smoke must use a shell script")
    return script


def test_amd64_ci_smoke_owns_private_sandbox_and_keeps_http_local() -> None:
    script = _smoke()
    assert 'mktemp -d "${TMPDIR:-/tmp}/.packbreaker-amd64-ci.XXXXXXXX"' in script
    assert "trap cleanup EXIT" in script
    assert '"$sandbox/config:/config"' in script
    assert '"$sandbox/data:/data"' in script
    assert '"$(basename "$sandbox")" == .packbreaker-amd64-ci.*' in script
    assert 'rm -rf -- "$sandbox"' in script
    assert '--publish "127.0.0.1:18000:8000"' in script
    assert "--publish 18000:8000" not in script
    assert "docker logs" not in script
    assert ".ci-config" not in script and ".ci-data" not in script
    assert script.count("docker run") == script.count("--network none") + 1 == 3
    assert "backend.app.maintenance verify-backup" in script
    assert "backend.app.maintenance restore-backup" in script
    assert "scripts/check-release-upgrade.sh packbreaker:ci" in script


def test_amd64_ci_failed_container_start_keeps_preexisting_data(tmp_path: Path) -> None:
    preserved = tmp_path / ".packbreaker-amd64-ci.preserved"
    preserved.mkdir()
    sentinel = preserved / "user-data"
    sentinel.write_text("keep", encoding="utf-8")
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    docker.write_text(
        '#!/bin/sh\ncase "$1" in\n'
        "  rm) exit 0;;\n"
        "  run) exit 88;;\n"
        '  logs) printf "SYNTHETIC-BOOTSTRAP-SECRET\\n";;\n'
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
        timeout=20,
        check=False,
    )
    assert result.returncode == 88
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert list(tmp_path.glob(".packbreaker-amd64-ci.*")) == [preserved]
    assert "SYNTHETIC-BOOTSTRAP-SECRET" not in result.stdout + result.stderr
