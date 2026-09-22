"""Offline safety checks for the formal Docker upgrade/rollback gate."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check-release-upgrade.sh"


def test_release_upgrade_uses_fresh_private_sandbox_and_network_isolation() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert 'mktemp -d "${TMPDIR:-/tmp}/.packbreaker-release-upgrade.XXXXXXXX"' in source
    assert 'config_dir="$sandbox/config"' in source
    assert 'data_dir="$sandbox/data"' in source
    assert 'baseline_build_dir="$sandbox/synthetic-baseline-build"' in source
    assert 'rm -rf -- "$sandbox"' in source
    assert '"$(basename "$sandbox")" == .packbreaker-release-upgrade.*' in source
    assert '"$PWD/.ci-release-upgrade-' not in source
    assert 'rm -rf "$config_dir"' not in source
    # Every release-upgrade container, including the backup verification and
    # restore one-shots, must run without external network access.
    assert source.count("docker run") == source.count("--network none") == 8
    assert "--network host" not in source
    assert "--publish" not in source
    assert "docker.sock" not in source


def test_release_upgrade_failure_cleans_only_its_own_sandbox(tmp_path: Path) -> None:
    # A fake Docker executable prevents any real container or registry access;
    # the script must stop after a failed pull and remove only its mktemp dir.
    preserved = tmp_path / ".packbreaker-release-upgrade.preserved"
    preserved.mkdir()
    sentinel = preserved / "user-data"
    sentinel.write_text("do not delete", encoding="utf-8")
    binary_dir = tmp_path / "fake-bin"
    binary_dir.mkdir()
    # The workspace may only expose the virtualenv interpreter as `python`.
    # Supply that interpreter explicitly rather than depending on host PATH.
    (binary_dir / "python").symlink_to(sys.executable)
    fake_docker = binary_dir / "docker"
    fake_docker.write_text("#!/bin/sh\nexit 88\n", encoding="utf-8")
    fake_docker.chmod(0o755)
    environment = {
        **os.environ,
        "PATH": f"{binary_dir}:{os.environ.get('PATH', '')}",
        "TMPDIR": str(tmp_path),
    }
    completed = subprocess.run(
        ["bash", str(SCRIPT), "packbreaker:synthetic-candidate"],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=25,
    )
    assert completed.returncode == 88
    assert sentinel.read_text(encoding="utf-8") == "do not delete"
    assert list(tmp_path.glob(".packbreaker-release-upgrade.*")) == [preserved]


def test_formal_upgrade_rejects_old_candidate_before_starting_containers(
    tmp_path: Path,
) -> None:
    """A baseline-version candidate must never count as a release upgrade."""
    binary_dir = tmp_path / "fake-bin"
    binary_dir.mkdir()
    (binary_dir / "python").symlink_to(sys.executable)
    fake_docker = binary_dir / "docker"
    fake_docker.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >> "$PB_FAKE_DOCKER_LOG"\n'
        'case "$1" in\n'
        "  pull|rm) exit 0;;\n"
        '  run) printf "1.0.0\\n"; exit 0;;\n'
        "  *) exit 88;;\n"
        "esac\n",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    log = tmp_path / "docker-calls"
    completed = subprocess.run(
        ["bash", str(SCRIPT), "packbreaker:synthetic-stale-candidate"],
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": f"{binary_dir}:{os.environ.get('PATH', '')}",
            "TMPDIR": str(tmp_path),
            "PB_FAKE_DOCKER_LOG": str(log),
        },
        text=True,
        capture_output=True,
        check=False,
        timeout=25,
    )
    assert completed.returncode != 0
    assert "candidate version" in completed.stderr
    calls = log.read_text(encoding="utf-8")
    assert "run --rm --network none" in calls
    assert "run --detach" not in calls
    assert not list(tmp_path.glob(".packbreaker-release-upgrade.*"))


def test_release_upgrade_checks_candidate_schema_against_its_own_migration_head() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert "ScriptDirectory.from_config" in source
    assert "make_migration_config" in source
    assert "assert row == (head,)" in source


def test_readiness_failure_does_not_disclose_container_bootstrap_logs(tmp_path: Path) -> None:
    """A failed ephemeral container must not print an initial admin password."""
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    (fake_bin / "python").symlink_to(sys.executable)
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        '#!/bin/sh\ncase "$1" in\n'
        "  pull|rm) exit 0;;\n"
        '  run) case "$*" in *"--detach"*) printf "synthetic-container-id\\n";; '
        '*) printf "1.0.1\\n";; esac; exit 0;;\n'
        "  exec) exit 88;;\n"
        '  logs) printf "SYNTHETIC-BOOTSTRAP-PASSWORD-DO-NOT-PRINT\\n"; exit 0;;\n'
        "  *) exit 89;;\n"
        "esac\n",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    fake_seq = fake_bin / "seq"
    fake_seq.write_text('#!/bin/sh\nprintf "60\\n"\n', encoding="utf-8")
    fake_seq.chmod(0o755)

    completed = subprocess.run(
        ["bash", str(SCRIPT), "packbreaker:synthetic-candidate"],
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
            "TMPDIR": str(tmp_path),
        },
        text=True,
        capture_output=True,
        timeout=25,
        check=False,
    )
    assert completed.returncode != 0
    assert "SYNTHETIC-BOOTSTRAP-PASSWORD-DO-NOT-PRINT" not in (completed.stdout + completed.stderr)
    assert "readiness failed" in completed.stderr
    assert not list(tmp_path.glob(".packbreaker-release-upgrade.*"))
