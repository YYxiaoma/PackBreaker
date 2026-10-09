from __future__ import annotations

from pathlib import Path

import yaml  # type: ignore[import-untyped]

WORKFLOW = (
    Path(__file__).resolve().parents[2] / ".github/workflows/candidate-v110-image-delivery.yml"
)


def test_v110_candidate_delivery_is_manual_and_sha_bound() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    workflow = yaml.safe_load(text)
    triggers = workflow.get("on", workflow.get(True))

    assert set(triggers) == {"workflow_dispatch"}
    assert triggers["workflow_dispatch"]["inputs"]["candidate_sha"]["required"]
    assert workflow["env"]["CANDIDATE_VERSION"] == "1.1.0"
    assert workflow["env"]["GHCR_IMAGE"] == "ghcr.io/yyxiaoma/packbreaker"
    assert workflow["env"]["DOCKERHUB_IMAGE"] == "docker.io/yyxiaoma01/packbreaker"
    assert set(workflow["jobs"]) == {"publish-candidate", "native-arm64-candidate"}

    publish = workflow["jobs"]["publish-candidate"]
    assert "refs/heads/main" in publish["if"]
    assert publish["permissions"]["actions"] == "read"
    assert publish["permissions"]["packages"] == "write"
    assert "ref: candidate/v1.1.0" in text
    assert 'test "$(git rev-parse refs/remotes/origin/candidate/v1.1.0)" = "$CANDIDATE_SHA"' in text
    assert "head_sha={sha}&event=push" in text
    assert 'run.get("conclusion") == "success"' in text
    assert 'tag="candidate-v1.1.0-${CANDIDATE_SHA:0:12}"' in text
    assert "Refuse existing candidate tags in either registry" in text


def test_v110_candidate_delivery_does_not_advance_formal_channels() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    workflow = yaml.safe_load(text)
    arm = workflow["jobs"]["native-arm64-candidate"]
    assert arm["needs"] == "publish-candidate"
    assert arm["runs-on"] == "ubuntu-24.04-arm"
    assert "linux/amd64,linux/arm64" in text
    assert "verify_release_platforms.py" in text
    assert "check-immutable-image-runtime.sh" in text
    assert "candidate-v1.1.0-" in text
    assert "refs/remotes/origin/candidate/v1.1.0" in text
    assert "ref: main" not in text
    assert ":latest" not in text
    assert ":stable" not in text
    assert "git push" not in text
    assert "gh release create" not in text
    assert not any("release" in job for job in workflow["jobs"])
