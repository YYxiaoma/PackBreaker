"""Keep formal cross-version gates mandatory for newer candidates."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml  # type: ignore[import-untyped]

from scripts import ci_release_relation
from scripts.ci_release_relation import is_newer_release, main

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("candidate", "baseline", "expected"),
    [
        ("1.0.1", "1.0.1", False),
        ("1.0.2", "1.0.1", True),
        ("1.1.0", "1.0.1", True),
        ("2.0.0", "1.9.9", True),
    ],
)
def test_release_relation(candidate: str, baseline: str, expected: bool) -> None:
    assert is_newer_release(candidate, baseline) is expected


@pytest.mark.parametrize("candidate", ["1.0.0", "0.9.99"])
def test_release_relation_rejects_older_checkout(candidate: str) -> None:
    with pytest.raises(ValueError, match="不能早于"):
        is_newer_release(candidate, "1.0.1")


def test_same_version_after_release_exports_explicit_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ci_release_relation, "project_version", lambda: "1.0.1")
    output = tmp_path / "github-output"
    assert main(["--github-output", str(output)]) == 0
    assert output.read_text(encoding="utf-8") == "candidate_newer=false\n"


def test_newer_candidate_exports_true(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ci_release_relation, "project_version", lambda: "1.0.2")
    output = tmp_path / "github-output"
    assert main(["--github-output", str(output)]) == 0
    assert output.read_text(encoding="utf-8") == "candidate_newer=true\n"


def test_ci_retains_version_conditioned_upgrade_and_unconditional_runtime() -> None:
    workflows = ROOT / ".github" / "workflows"
    ci = yaml.safe_load((workflows / "ci.yml").read_text(encoding="utf-8"))
    candidate = yaml.safe_load((workflows / "candidate-docker-e2e.yml").read_text(encoding="utf-8"))
    relation = "steps.release_relation.outputs.candidate_newer == 'true'"
    arm_steps = ci["jobs"]["arm64"]["steps"]
    arm_names = {step.get("name"): step for step in arm_steps}
    assert (
        "python -m scripts.ci_release_relation"
        in arm_names["Classify formal release relation"]["run"]
    )
    assert (
        "scripts/check-immutable-image-runtime.sh"
        in arm_names["Exercise immutable formal ARM64 baseline in isolated runtime"]["run"]
    )
    for name in (
        "Exercise native ARM64 formal adjacent upgrade and rollback",
        "Exercise native ARM64 formal updater and failure rollback",
    ):
        assert arm_names[name]["if"] == relation
    smoke = next(
        step["run"]
        for step in ci["jobs"]["container"]["steps"]
        if step.get("name") == "Smoke test image and health endpoint"
    )
    assert "scripts/check-release-upgrade.sh packbreaker:ci" in smoke
    assert "python -m scripts.ci_release_relation" in smoke
    updater = ci["jobs"]["updater-e2e"]["steps"]
    assert all(
        step["if"] == relation
        for step in updater
        if step.get("name")
        in (
            "Build updater candidate image",
            "Run real Docker updater success and rollback E2E",
        )
    )
    candidate_steps = candidate["jobs"]["updater-e2e"]["steps"]
    assert all(
        step["if"] == relation
        for step in candidate_steps
        if step.get("name")
        in (
            "Exercise previous-release upgrade and rollback",
            "Exercise real updater, rollback, and Compose-label preservation",
        )
    )
    # A release tag still runs unconditional strict cross-version gates.
    release = yaml.safe_load((workflows / "release.yml").read_text(encoding="utf-8"))
    release_scripts = "\n".join(
        str(step.get("run", "")) for step in release["jobs"]["release"]["steps"]
    )
    assert "scripts/check-release-upgrade.sh" in release_scripts
    assert "scripts/check-updater-e2e.sh" in release_scripts
    assert "scripts.ci_release_relation" not in release_scripts
    assert "python3 -m scripts.ci_release_relation" in next(
        step["run"]
        for step in candidate_steps
        if step.get("name") == "Classify formal release relation"
    )
