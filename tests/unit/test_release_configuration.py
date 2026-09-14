from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_all_dockerfile_base_images_are_digest_pinned() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    from_lines = [line for line in dockerfile.splitlines() if line.startswith("FROM ")]

    assert len(from_lines) == 3
    for line in from_lines:
        image = line.split()[1]
        assert re.fullmatch(r"[^@\s]+@sha256:[0-9a-f]{64}", image), line
    assert "node:22.23.2-bookworm-slim@sha256:" in from_lines[0]
    assert "python:3.11.16-slim-bookworm@sha256:" in from_lines[1]
    assert "python:3.11.16-slim-bookworm@sha256:" in from_lines[2]


def test_release_workflow_binds_linux_amd64_digest_sbom_and_manifest() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

    assert "platforms: linux/amd64" in workflow
    assert "docker/build-push-action@v7.3.0" in workflow
    assert "steps.build.outputs.digest" in workflow
    assert "anchore/sbom-action@v0.24.2" in workflow
    assert "format: spdx-json" in workflow
    assert "scripts/release_manifest.py" in workflow
    assert "sha256sum *.json > SHA256SUMS" in workflow
    assert "Refuse an existing immutable version image" in workflow
    assert "Refuse an existing GitHub Release" in workflow
    assert "Require baseline to be the latest published release" in workflow
    assert "repos/$GITHUB_REPOSITORY/releases/latest" in workflow
    assert "imagetools create" in workflow
    assert ":stable" in workflow
    assert "--generate-notes" in workflow
    assert "check-release-upgrade.sh packbreaker:release-candidate" in workflow


def test_ci_container_gate_exercises_immutable_previous_release() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert '--build-arg VERSION="$version"' in workflow
    assert "check-release-upgrade.sh packbreaker:ci" in workflow
    assert "release-baseline.json" in (ROOT / "scripts" / "validate_release_baseline.py").read_text(
        encoding="utf-8"
    )
