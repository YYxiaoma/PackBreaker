"""Release must not expose assets before the published ARM64 digest runs natively."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml  # type: ignore[import-untyped]

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"


def test_native_arm64_candidate_smoke_is_ephemeral_and_network_isolated() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["arm64-validation"]["steps"]
    smoke = next(
        step["run"]
        for step in steps
        if step.get("name") == "Build and smoke test native ARM64 release candidate"
    )
    assert 'mktemp -d "${TMPDIR:-/tmp}/.packbreaker-arm64-candidate.XXXXXXXX"' in smoke
    assert "trap cleanup EXIT" in smoke
    assert '"$sandbox/config:/config"' in smoke
    assert '"$sandbox/data:/data"' in smoke
    assert 'docker run --detach --name "$container"' in smoke
    assert "--network none" in smoke
    assert "backend.app.healthcheck" in smoke
    assert "--publish" not in smoke
    assert "--network host" not in smoke
    assert ".ci-release-arm64-config" not in smoke
    assert ".ci-release-arm64-data" not in smoke
    assert "docker logs" not in smoke  # May disclose a generated bootstrap password.


def test_native_arm64_candidate_failed_start_cleans_only_its_sandbox(tmp_path: Path) -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["arm64-validation"]["steps"]
    smoke = next(
        step["run"]
        for step in steps
        if step.get("name") == "Build and smoke test native ARM64 release candidate"
    )
    preserved = tmp_path / ".packbreaker-arm64-candidate.preserved"
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
        '#!/bin/sh\ncase "$1" in\n  build|rm) exit 0;;\n  run) exit 88;;\n  *) exit 89;;\nesac\n',
        encoding="utf-8",
    )
    docker.chmod(0o755)

    completed = subprocess.run(
        ["bash", "-c", smoke],
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
            "TMPDIR": str(tmp_path),
            "GITHUB_REF_NAME": "v1.0.1",
            "GITHUB_SHA": "a" * 40,
        },
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 88
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert list(tmp_path.glob(".packbreaker-arm64-candidate.*")) == [preserved]


def _validate_order(jobs: dict[str, object]) -> None:
    native = jobs["native-arm64-published-runtime"]
    assets = jobs["release-assets"]
    assert isinstance(native, dict) and isinstance(assets, dict)
    assert native["needs"] == "release"
    assert set(assets["needs"]) == {"release", "native-arm64-published-runtime"}
    assert "if" not in assets and "if" not in native


def test_release_assets_wait_for_native_arm64_same_immutable_digest() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    assert jobs["release"]["needs"] == "arm64-validation"
    assert jobs["release"]["runs-on"] == "ubuntu-latest"
    assert jobs["native-arm64-published-runtime"]["runs-on"] == "ubuntu-24.04-arm"
    assert jobs["release-assets"]["runs-on"] == "ubuntu-latest"
    assert workflow["concurrency"]["cancel-in-progress"] is False
    _validate_order(jobs)

    build = jobs["release"]
    outputs = build["outputs"]
    assert outputs == {
        "image": "${{ steps.release_meta.outputs.image }}",
        "version": "${{ steps.release_meta.outputs.version }}",
        "created": "${{ steps.release_meta.outputs.created }}",
        "digest": "${{ steps.build.outputs.digest }}",
    }
    build_steps = "\n".join(str(step) for step in build["steps"])
    native_steps = "\n".join(str(step) for step in jobs["native-arm64-published-runtime"]["steps"])
    assets_steps = "\n".join(str(step) for step in jobs["release-assets"]["steps"])

    assert "docker/build-push-action@v7.3.0" in build_steps
    assert "Exercise immutable published ARM64 image under QEMU" in build_steps
    published_upgrade_steps = [
        (index, step)
        for index, step in enumerate(build["steps"])
        if step.get("name")
        == "Exercise formal AMD64 adjacent upgrade and rollback with published immutable digest"
    ]
    assert len(published_upgrade_steps) == 1
    upgrade_index, upgrade_step = published_upgrade_steps[0]
    assert upgrade_step["if"] == "${{ !startsWith(github.ref_name, 'v0.') }}"
    assert upgrade_step["run"].strip() == (
        "bash scripts/check-release-upgrade.sh \\\n"
        '  "${{ steps.release_meta.outputs.image }}@${{ steps.build.outputs.digest }}"'
    )
    step_names = [step.get("name") for step in build["steps"]]
    assert step_names.index("Build and publish target architecture image index") < upgrade_index
    assert (
        step_names.index("Exercise immutable published AMD64 image in isolated runtime")
        < upgrade_index
    )
    assert upgrade_index < step_names.index(
        "Exercise immutable published ARM64 image under QEMU in isolated runtime"
    )
    assert "gh release create" not in build_steps
    assert "actions/upload-artifact" not in build_steps
    assert ":stable" not in build_steps and ":latest" not in build_steps

    assert "--platform linux/arm64" not in native_steps  # runtime gate has platform arg
    assert 'test "$(uname -m)" = aarch64' in native_steps
    assert "check-immutable-image-runtime.sh" in native_steps
    assert "${{ needs.release.outputs.image }}@${{ needs.release.outputs.digest }}" in native_steps
    assert 'linux/arm64 "$GITHUB_REF_NAME" "$GITHUB_SHA"' in native_steps
    assert 'if [[ "$GITHUB_REF_NAME" == v0.* ]]' in native_steps
    assert "docker/build-push-action" not in native_steps
    assert "gh release create" not in native_steps
    assert jobs["native-arm64-published-runtime"]["permissions"] == {
        "contents": "read",
        "packages": "read",
    }

    assert (
        "Ensure native runtime-verified published digest still matches the version tag"
        in assets_steps
    )
    assert "${{ needs.release.outputs.digest }}" in assets_steps
    assert "verify_release_platforms.py" in assets_steps
    assert "scripts/verify_release_evidence.py" in assets_steps
    assert "gh release create" in assets_steps
    assert ":stable" in assets_steps and ":latest" in assets_steps
    for step in jobs["release-assets"]["steps"]:
        if step.get("name") in (
            "Generate SPDX SBOM from immutable image digest",
            "Generate release manifest and checksums",
            "Fail closed on local Release asset identity before any asset upload",
        ):
            assert "${{ steps.build.outputs.digest }}" not in str(step)


@pytest.mark.parametrize("missing", ["release", "native-arm64-published-runtime"])
def test_release_asset_job_rejects_missing_required_dependency(missing: str) -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    jobs["release-assets"]["needs"].remove(missing)
    with pytest.raises(AssertionError):
        _validate_order(jobs)


def test_candidate_ci_proves_index_digest_template_against_existing_published_baseline() -> None:
    candidate = (ROOT / ".github" / "workflows" / "candidate-docker-e2e.yml").read_text(
        encoding="utf-8"
    )
    workflow = WORKFLOW.read_text(encoding="utf-8")
    for body in (candidate, workflow):
        assert "--format '{{json .Manifest.Digest}}'" in body
        assert 'test "$actual" =' in body
    assert candidate.index("Read-only verify published baseline version tag") < candidate.index(
        "Build current main as local candidate"
    )


def test_candidate_ci_verifies_amd64_only_and_multiarch_formal_baselines() -> None:
    candidate = (ROOT / ".github" / "workflows" / "candidate-docker-e2e.yml").read_text(
        encoding="utf-8"
    )
    assert 'platform not in ("linux/amd64", "multi")' in candidate
    assert '["--multiarch"] if platform == "multi" else []' in candidate
    assert "*platform_args," in candidate
    assert "expected published AMD64-only baseline" not in candidate
    assert "scripts/verify_release_platforms.py" in candidate
