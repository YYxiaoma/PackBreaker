"""Explicit candidate GHCR delivery must pin reviewed source and leave formal tags untouched."""

from __future__ import annotations

import subprocess
from pathlib import Path

import yaml  # type: ignore[import-untyped]

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "candidate-image-delivery.yml"


def test_candidate_delivery_requires_explicit_dispatch_and_never_touches_formal_channels() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    triggers = workflow.get("on", workflow.get(True))
    assert set(triggers) == {"workflow_dispatch"}
    candidate_input = triggers["workflow_dispatch"]["inputs"]["candidate_sha"]
    assert candidate_input["required"] is True
    assert candidate_input["type"] == "string"
    assert set(workflow["jobs"]) == {"publish-candidate", "native-arm64-candidate"}

    publish = workflow["jobs"]["publish-candidate"]
    native = workflow["jobs"]["native-arm64-candidate"]
    assert publish["permissions"] == {"contents": "read", "packages": "write"}
    assert native["permissions"] == {"contents": "read", "packages": "read"}
    assert native["runs-on"] == "ubuntu-24.04-arm"
    assert native["needs"] == "publish-candidate"
    assert "github.repository == 'YYxiaoma/PackBreaker'" in publish["if"]
    assert "github.ref == 'refs/heads/candidate/v1.0.1'" in publish["if"]

    script = WORKFLOW.read_text(encoding="utf-8")
    assert "docker/build-push-action@v7.3.0" in script
    assert "linux/amd64,linux/arm64" in script
    assert "scripts/verify_release_platforms.py" in script
    assert "scripts/check-immutable-image-runtime.sh" in script
    assert "--multiarch" in script
    assert "CANDIDATE_SHA: ${{ inputs.candidate_sha }}" in script
    assert '[[ "$CANDIDATE_SHA" =~ ^[0-9a-f]{40}$ ]]' in script
    assert 'git merge-base --is-ancestor "$CANDIDATE_SHA" origin/candidate/v1.0.1' in script
    assert 'git checkout --detach "$CANDIDATE_SHA"' in script
    assert 'test "$(git rev-parse HEAD)" = "$CANDIDATE_SHA"' in script
    assert "CANDIDATE_TAG=candidate-v1.0.1-${CANDIDATE_SHA:0:12}" in script
    assert "BUILD_DATE=${{ steps.identity.outputs.build_date }}" in script
    assert "github.event.head_commit.timestamp" not in script
    assert "47ec58c30ad521ebf00b2646d544556715d275b3" not in script
    for forbidden in (
        "gh release create",
        "imagetools create",
        ":stable",
        ":latest",
        "refs/tags/v1.0.1",
    ):
        assert forbidden not in script


def test_candidate_delivery_rejects_existing_tag_and_checks_published_digest() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["publish-candidate"]["steps"]
    names = [step.get("name", "") for step in steps]
    assert names.index("Verify candidate source identity and version") < names.index(
        "Refuse existing candidate image"
    )
    assert names.index("Refuse existing candidate image") < names.index(
        "Build and push candidate index"
    )
    assert names.index("Build and push candidate index") < names.index(
        "Verify published candidate image"
    )
    verify = steps[names.index("Verify published candidate image")]["run"]
    assert "steps.build.outputs.digest" in verify
    assert "scripts/verify_release_platforms.py" in verify
    assert 'test "$tag_digest" = "$digest"' in verify
    assert "linux/amd64" in verify
    assert "linux/arm64" in verify
    native_script = "\n".join(
        step.get("run", "") for step in workflow["jobs"]["native-arm64-candidate"]["steps"]
    )
    assert "needs.publish-candidate.outputs.digest" in native_script
    assert "linux/arm64" in native_script
    assert "ref: ${{ inputs.candidate_sha }}" in WORKFLOW.read_text(encoding="utf-8")


def test_candidate_delivery_inline_shell_is_valid_and_native_checks_exact_commit() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    for job in workflow["jobs"].values():
        for step in job["steps"]:
            if "run" not in step:
                continue
            subprocess.run(["bash", "-n"], input=step["run"], text=True, check=True)
    native_steps = workflow["jobs"]["native-arm64-candidate"]["steps"]
    native_script = "\n".join(step.get("run", "") for step in native_steps)
    assert 'test "$(git rev-parse HEAD)" = "$CANDIDATE_SHA"' in native_script
