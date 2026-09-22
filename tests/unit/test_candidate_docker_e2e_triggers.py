"""The pre-release Docker gate must cover every application layer copied into the image."""

from __future__ import annotations

from pathlib import Path

import yaml  # type: ignore[import-untyped]

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "candidate-docker-e2e.yml"


def test_candidate_docker_e2e_push_covers_frontend_and_migrations() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    # PyYAML interprets the unquoted YAML 1.1 key `on` as the boolean True.
    triggers = workflow.get("on", workflow.get(True))
    assert isinstance(triggers, dict)
    push = triggers["push"]
    assert set(push["branches"]) == {"main", "candidate/**"}
    paths = set(push["paths"])
    assert "frontend/**" in paths, "Frontend-only changes must trigger the candidate image E2E"
    assert "backend/**" in paths, "Alembic migration-only changes must trigger upgrade E2E"
    assert "docs/**" in paths, "Runtime documentation is copied into the Docker image"
    assert {"README.md", "LICENSE", "Dockerfile", "pyproject.toml", "uv.lock"} <= paths
    assert "scripts/check_candidate_image_identity.py" in paths, (
        "Changes to the image identity verifier must re-run AMD64 candidate Docker acceptance"
    )
    assert "workflow_dispatch" in triggers, "Candidate E2E must remain manually runnable"


def test_candidate_docker_e2e_preserves_immutable_baseline_and_rollback_steps() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["updater-e2e"]["steps"]
    commands = "\n".join(step.get("run", "") for step in steps)
    for required in (
        "scripts/validate_release_baseline.py",
        "scripts/check-immutable-image-runtime.sh",
        "scripts/check-release-upgrade.sh",
        "scripts/check-updater-e2e.sh",
    ):
        assert required in commands
