from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check-immutable-image-runtime.sh"
IMAGE = "ghcr.io/yyxiaoma/packbreaker@sha256:" + "a" * 64
COMMIT = "b" * 40


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["ghcr.io/yyxiaoma/packbreaker:latest", "linux/amd64", "v1.0.0", COMMIT],
        ["ghcr.io/yyxiaoma/packbreaker@sha256:short", "linux/amd64", "v1.0.0", COMMIT],
        ["ghcr.io/yyxiaoma/../packbreaker@sha256:" + "a" * 64, "linux/amd64", "v1.0.0", COMMIT],
        [IMAGE, "linux/arm/v7", "v1.0.0", COMMIT],
        [IMAGE, "linux/amd64", "0.1.9", COMMIT],
        [IMAGE, "linux/amd64", "v1.0.0", "unknown"],
    ],
)
def test_immutable_runtime_gate_rejects_invalid_inputs_before_any_docker(args: list[str]) -> None:
    completed = subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=8,
        check=False,
    )
    assert completed.returncode == 2
    assert "Usage:" in completed.stderr or "Invalid immutable image" in completed.stderr


def test_immutable_runtime_gate_script_syntax() -> None:
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True, timeout=8)


def test_immutable_runtime_gate_uses_temporary_data_and_no_host_ports() -> None:
    script = SCRIPT.read_text(encoding="utf-8")
    assert "mktemp -d" in script
    assert "docker pull --platform" in script
    assert "docker create" in script
    assert '"$sandbox/config:/config"' in script
    assert '"$sandbox/data:/data"' in script
    assert "--publish" not in script
    assert "--network none" in script
    assert "--network host" not in script
    assert "docker.sock" not in script
    assert "backend.app.healthcheck" in script
    assert "backend.app.maintenance preflight" in script
    assert "backend.app.maintenance backup" in script
    assert "actual_machine" in script
    assert "docker rm --force" in script


def test_release_workflow_requires_immutable_runtime_for_both_platforms_before_assets() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    script_name = "check-immutable-image-runtime.sh"
    assert workflow.count(script_name) == 3
    assert workflow.index("scripts/verify_release_platforms.py") < workflow.index(script_name)
    assert workflow.index(script_name) < workflow.index(
        "Generate SPDX SBOM from immutable image digest"
    )
    assert workflow.index('linux/arm64 "$GITHUB_REF_NAME" "$GITHUB_SHA"') < workflow.index(
        "Generate SPDX SBOM from immutable image digest"
    )
    assert "docker/setup-qemu-action@v3" in workflow


def test_candidate_workflow_smokes_real_published_amd64_baseline_without_republishing() -> None:
    workflow = (ROOT / ".github" / "workflows" / "candidate-docker-e2e.yml").read_text(
        encoding="utf-8"
    )
    assert "check-immutable-image-runtime.sh" in workflow
    assert (
        '"${baseline_fields[0]}" linux/amd64 "${baseline_fields[1]}" "${baseline_fields[2]}"'
        in workflow
    )
    assert workflow.index("Verify actual published AMD64 baseline") < workflow.index(
        "Exercise immutable published AMD64 baseline startup"
    )
    assert workflow.index("Exercise immutable published AMD64 baseline startup") < workflow.index(
        "Build current main as local candidate"
    )
    assert "docker/build-push-action" not in workflow
