"""v1.0.5 candidate delivery must publish only an isolated exact-SHA image."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "candidate-v105-image-delivery.yml"


def _workflow() -> dict[Any, Any]:
    value = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_v105_candidate_delivery_is_manual_and_version_pinned() -> None:
    workflow = _workflow()
    triggers = workflow.get("on", workflow.get(True))
    assert isinstance(triggers, dict)
    assert set(triggers) == {"workflow_dispatch"}
    dispatch = triggers["workflow_dispatch"]
    assert isinstance(dispatch, dict)
    assert dispatch["inputs"]["candidate_sha"]["required"] is True

    env = workflow["env"]
    assert env["CANDIDATE_VERSION"] == "1.0.5"
    assert env["IMAGE"] == "ghcr.io/yyxiaoma/packbreaker"


def test_v105_candidate_delivery_never_updates_release_channels() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "candidate-v1.0.5-" in text
    assert "gh release" not in text
    assert "git tag" not in text
    assert ":latest" not in text
    assert ":stable" not in text
    assert "docker buildx imagetools create" not in text


def test_v105_candidate_delivery_verifies_main_sha_and_both_architectures() -> None:
    workflow = _workflow()
    jobs = workflow["jobs"]
    publish = jobs["publish-candidate"]
    assert publish["if"] == (
        "github.repository == 'YYxiaoma/PackBreaker' && github.ref == 'refs/heads/main'"
    )
    commands = "\n".join(str(step.get("run", "")) for step in publish["steps"])
    assert 'git merge-base --is-ancestor "$CANDIDATE_SHA" origin/main' in commands
    assert 'test "$(git rev-parse HEAD)" = "$CANDIDATE_SHA"' in commands
    assert "--tag v1.0.5 --commit" in commands
    assert "--multiarch" in commands
    assert "linux/amd64 v1.0.5" in commands

    native = jobs["native-arm64-candidate"]
    native_commands = "\n".join(str(step.get("run", "")) for step in native["steps"])
    assert "linux/arm64 v1.0.5" in native_commands
    assert "aarch64" in native_commands
