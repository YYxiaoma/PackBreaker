"""Candidate GHCR delivery must be independent of formal release channels."""

from __future__ import annotations

from pathlib import Path

import yaml  # type: ignore[import-untyped]

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "candidate-image-delivery.yml"
CANDIDATE_SHA = "47ec58c30ad521ebf00b2646d544556715d275b3"
TAG = "candidate-v1.0.1-47ec58c"


def test_candidate_delivery_is_push_only_and_never_touches_formal_channels() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    triggers = workflow.get("on", workflow.get(True))
    assert triggers["push"]["branches"] == ["candidate/v1.0.1"]
    assert triggers["push"]["paths"] == [".github/workflows/candidate-image-delivery.yml"]
    assert "release" not in triggers and "workflow_dispatch" not in triggers
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
    assert CANDIDATE_SHA in script and TAG in script
    assert "ref: ${{ env.CANDIDATE_SHA }}" in script
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
