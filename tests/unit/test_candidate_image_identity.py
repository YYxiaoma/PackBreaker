"""Fail closed when candidate Docker CI tests a stale or mislabelled image."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml  # type: ignore[import-untyped]

ROOT = Path(__file__).resolve().parents[2]


def _valid_image_metadata() -> dict[str, Any]:
    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]
    return {
        "Id": "sha256:" + "1" * 64,
        "Os": "linux",
        "Architecture": "amd64",
        "Config": {
            "Labels": {
                "org.opencontainers.image.version": version,
                "org.opencontainers.image.revision": "b" * 40,
            }
        },
    }


def test_candidate_workflows_verify_exact_image_identity_before_runtime_checks() -> None:
    cases = (
        (
            "candidate-docker-e2e.yml",
            "updater-e2e",
            "linux/amd64",
            "Exercise previous-release upgrade and rollback",
        ),
        ("ci.yml", "arm64", "linux/arm64", "Exercise native ARM64 container startup and backup"),
    )
    for filename, job, platform, runtime_name in cases:
        workflow = yaml.safe_load((ROOT / ".github" / "workflows" / filename).read_text())
        steps = workflow["jobs"][job]["steps"]
        identity = next(
            i
            for i, step in enumerate(steps)
            if step.get("name") == "Verify candidate image identity"
        )
        runtime = next(i for i, step in enumerate(steps) if step.get("name") == runtime_name)
        assert identity < runtime
        script = steps[identity]["run"]
        assert "scripts/check_candidate_image_identity.py" in script
        assert platform in script
        assert '"$GITHUB_SHA"' in script


@pytest.mark.parametrize(
    ("field", "wrong"),
    [
        ("Id", "invalid-digest"),
        ("Os", "windows"),
        ("Architecture", "arm64"),
        ("version", "1.0.0"),
        ("revision", "a" * 40),
    ],
)
def test_candidate_identity_rejects_mismatch_with_fake_docker(
    tmp_path: Path, field: str, wrong: str
) -> None:
    inspect = _valid_image_metadata()
    if field in ("version", "revision"):
        inspect["Config"]["Labels"][f"org.opencontainers.image.{field}"] = wrong
    else:
        inspect[field] = wrong
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    docker.write_text("#!/bin/sh\nprintf '%s\\n' \"$PB_SYNTHETIC_IMAGE_JSON\"\n", encoding="utf-8")
    docker.chmod(0o755)
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "check_candidate_image_identity.py"),
            "--image",
            "packbreaker:synthetic-candidate",
            "--platform",
            "linux/amd64",
            "--commit",
            "b" * 40,
        ],
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
            "PB_SYNTHETIC_IMAGE_JSON": json.dumps(inspect),
        },
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode != 0
    assert "candidate image identity" in result.stderr.lower()
    assert "PB_SYNTHETIC_IMAGE_JSON" not in result.stderr


def test_candidate_image_identity_accepts_matching_local_image(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    docker.write_text("#!/bin/sh\nprintf '%s\\n' \"$PB_SYNTHETIC_IMAGE_JSON\"\n", encoding="utf-8")
    docker.chmod(0o755)
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "check_candidate_image_identity.py"),
            "--image",
            "packbreaker:synthetic-candidate",
            "--platform",
            "linux/amd64",
            "--commit",
            "b" * 40,
        ],
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
            "PB_SYNTHETIC_IMAGE_JSON": json.dumps(_valid_image_metadata()),
        },
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0
    assert "candidate image identity: verified linux/amd64" in result.stdout
