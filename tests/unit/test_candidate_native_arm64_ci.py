"""Candidate branches must exercise native ARM64 and isolated AMD64 E2E before release."""

from __future__ import annotations

from pathlib import Path

import yaml  # type: ignore[import-untyped]

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"


def _triggers(filename: str) -> dict[str, object]:
    workflow = yaml.safe_load((WORKFLOWS / filename).read_text(encoding="utf-8"))
    triggers = workflow.get("on", workflow.get(True))
    if not isinstance(triggers, dict):
        raise ValueError("workflow triggers must be a mapping")
    return triggers


def test_candidate_branch_push_runs_both_native_arm64_and_amd64_docker_workflows() -> None:
    ci_triggers = _triggers("ci.yml")
    candidate_triggers = _triggers("candidate-docker-e2e.yml")
    for triggers in (ci_triggers, candidate_triggers):
        push = triggers["push"]
        assert isinstance(push, dict)
        assert "candidate/**" in push["branches"]
        assert "main" in push["branches"]


def test_candidate_branch_native_arm64_ci_retains_full_runtime_coverage() -> None:
    workflow = yaml.safe_load((WORKFLOWS / "ci.yml").read_text(encoding="utf-8"))
    steps = workflow["jobs"]["arm64"]["steps"]
    commands = "\n".join(str(step.get("run", "")) for step in steps)
    for mandatory in (
        "docker build --platform linux/arm64",
        "backend.app.healthcheck",
        "backend.app.maintenance backup",
        "backend.app.maintenance restore-backup",
        "scripts/check-updater-e2e.sh",
        "scripts/check-release-upgrade.sh",
        "scripts/check-arm64-real-downloaders-e2e.sh",
    ):
        assert mandatory in commands


def test_native_arm64_candidate_proves_formal_cross_version_upgrade_and_rollback() -> None:
    workflow = yaml.safe_load((WORKFLOWS / "ci.yml").read_text(encoding="utf-8"))
    job = workflow["jobs"]["arm64"]
    assert job["permissions"]["contents"] == "read"
    assert job["permissions"]["packages"] == "read"
    steps = job["steps"]
    names = [step.get("name") for step in steps]
    login = names.index("Authenticate for read-only formal baseline image")
    baseline = names.index("Exercise immutable formal ARM64 baseline in isolated runtime")
    upgrade = names.index("Exercise native ARM64 formal adjacent upgrade and rollback")
    updater = names.index("Exercise native ARM64 formal updater and failure rollback")
    assert login < baseline < upgrade < updater
    baseline_step = steps[baseline]["run"]
    assert "scripts/validate_release_baseline.py --json" in baseline_step
    assert '"multi"' in baseline_step
    assert "linux/arm64" in baseline_step
    assert "scripts/check-immutable-image-runtime.sh" in baseline_step
    assert (
        '"${baseline_fields[0]}" linux/arm64 "${baseline_fields[1]}" "${baseline_fields[2]}"'
        in baseline_step
    )
    assert (
        steps[upgrade]["run"].strip()
        == "bash scripts/check-release-upgrade.sh packbreaker:ci-arm64"
    )
    assert steps[updater]["run"].strip() == "bash scripts/check-updater-e2e.sh packbreaker:ci-arm64"
    # The formal updater must use the immutable published baseline. A
    # separately named synthetic exercise is permitted for anonymous volumes,
    # but must never be confused with cross-version upgrade evidence.
    assert "--synthetic-arm64-baseline" not in steps[updater]["run"]
    synthetic = next(
        step
        for step in steps
        if step.get("name") == "Exercise native ARM64 synthetic anonymous-volume replacement"
    )
    assert "--synthetic-arm64-baseline" in synthetic["run"]
