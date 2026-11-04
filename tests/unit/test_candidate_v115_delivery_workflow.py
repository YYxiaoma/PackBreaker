"""v1.1.5 dual-registry delivery must be SHA-bound and not a formal release."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

WORKFLOW = (
    Path(__file__).resolve().parents[2] / ".github/workflows/candidate-v115-image-delivery.yml"
)


def _workflow() -> dict[Any, Any]:
    value = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_v115_candidate_delivery_is_branch_restricted_and_sha_bound() -> None:
    w = _workflow()
    triggers = w.get("on") or w.get(True)
    assert isinstance(triggers, dict)
    assert set(triggers) == {"push", "workflow_dispatch"}
    assert triggers["push"]["branches"] == ["candidate/v1.1.5"]
    assert ".github/workflows/candidate-v115-image-delivery.yml" in triggers["push"]["paths"]
    candidate = triggers["workflow_dispatch"]["inputs"]["candidate_sha"]
    assert candidate["required"] is True and candidate["type"] == "string"
    assert set(w["jobs"]) == {"publish-candidate", "native-arm64-candidate"}
    publish = w["jobs"]["publish-candidate"]
    assert "github.repository == 'YYxiaoma/PackBreaker'" in publish["if"]
    assert "github.ref == 'refs/heads/main'" in publish["if"]
    assert "github.ref == 'refs/heads/candidate/v1.1.5'" in publish["if"]
    assert w["env"]["CANDIDATE_SHA"] == "${{ inputs.candidate_sha || github.sha }}"
    assert publish["permissions"] == {"contents": "read", "packages": "write", "actions": "read"}
    assert w["env"]["CANDIDATE_VERSION"] == "1.1.5"
    assert w["env"]["GHCR_IMAGE"] == "ghcr.io/yyxiaoma/packbreaker"
    assert w["env"]["DOCKERHUB_IMAGE"] == "docker.io/yyxiaoma01/packbreaker"
    source = WORKFLOW.read_text(encoding="utf-8")
    assert "ref: candidate/v1.1.5" in source
    assert (
        'test "$(git rev-parse refs/remotes/origin/candidate/v1.1.5)" = "$CANDIDATE_SHA"' in source
    )
    assert 'git checkout --detach "$CANDIDATE_SHA"' in source
    assert 'test "$(git rev-parse HEAD)" = "$CANDIDATE_SHA"' in source
    assert 'tag="candidate-v1.1.5-${CANDIDATE_SHA:0:12}"' in source


def test_v115_waits_for_two_successful_sha_matched_workflows_before_publish() -> None:
    w = _workflow()
    steps = w["jobs"]["publish-candidate"]["steps"]
    names = [step.get("name") for step in steps]
    baseline_script = next(
        step["run"]
        for step in steps
        if step.get("name") == "Require verified v1.1.4 immutable upgrade baseline"
    )
    for required in (
        'baseline.version == "1.1.4"',
        'baseline.tag == "v1.1.4"',
        "9d63d684de5b04985e480f45b272a308faed29ae",
        "sha256:9edb6ebcae21536f4930dc5a8061078b0049efe67af709f450a6b3e97cf03086",
        "0046_site_user_profile_v114",
    ):
        assert required in baseline_script
    assert (
        names.index("Verify candidate source identity and version")
        < names.index("Require verified v1.1.4 immutable upgrade baseline")
        < names.index("Wait for successful CI and Docker E2E for the exact candidate SHA")
        < names.index("Refuse existing candidate tags in either registry")
        < names.index("Build and push one multi-arch candidate to both registries")
    )
    script = steps[
        names.index("Wait for successful CI and Docker E2E for the exact candidate SHA")
    ]["run"]
    for required in (
        '("ci.yml", "candidate-docker-e2e.yml")',
        "head_sha={sha}&event=push",
        'branch = "candidate/v1.1.5"',
        'run.get("head_branch") == branch',
        'run.get("status") == "completed"',
        'run.get("head_sha") == sha',
        "deadline = time.monotonic() + 3600",
        "time.sleep(20)",
        'latest.get("conclusion") != "success"',
    ):
        assert required in script


def test_v115_candidate_same_digest_native_arm64_no_formal_release() -> None:
    w = _workflow()
    native = w["jobs"]["native-arm64-candidate"]
    assert native["needs"] == "publish-candidate"
    assert native["runs-on"] == "ubuntu-24.04-arm"
    assert native["permissions"] == {"contents": "read", "packages": "read"}
    source = WORKFLOW.read_text(encoding="utf-8")
    assert "platforms: linux/amd64,linux/arm64" in source
    assert 'test "$ghcr_digest" = "$digest"' in source
    assert 'test "$dockerhub_digest" = "$digest"' in source
    assert "scripts/verify_release_platforms.py" in source
    assert "scripts/check-immutable-image-runtime.sh" in source
    assert "needs.publish-candidate.outputs.digest" in source
    assert "linux/arm64" in source
    for forbidden in ("gh release create", "imagetools create", ":latest", ":stable", "git push"):
        assert forbidden not in source


def test_v115_candidate_inline_shell_parses_and_push_tags_are_immutable() -> None:
    w = _workflow()
    for job in w["jobs"].values():
        for step in job["steps"]:
            if "run" in step:
                subprocess.run(["bash", "-n"], input=step["run"], text=True, check=True)
    builder = next(
        step
        for step in w["jobs"]["publish-candidate"]["steps"]
        if step.get("uses") == "docker/build-push-action@v7.3.0"
    )
    assert builder["with"]["push"] is True
    assert "${{ env.GHCR_IMAGE }}:${{ steps.identity.outputs.tag }}" in builder["with"]["tags"]
    assert "${{ env.DOCKERHUB_IMAGE }}:${{ steps.identity.outputs.tag }}" in builder["with"]["tags"]
