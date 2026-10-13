"""Ensure pending real seven-site E2E blocks all v1.1.2 release preparation jobs."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml  # type: ignore[import-untyped]

WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/release.yml"
GATE = "seven-site-release-preflight"


def _jobs() -> dict[str, Any]:
    value = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(value, dict) and isinstance(value.get("jobs"), dict)
    return cast(dict[str, Any], value["jobs"])


def test_real_seven_site_gate_is_preflight_with_no_release_write_permission() -> None:
    jobs = _jobs()
    assert GATE in jobs
    gate = jobs[GATE]
    assert gate["runs-on"] == "ubuntu-latest"
    assert gate["permissions"] == {"contents": "read"}
    assert not gate.get("needs")
    assert gate["timeout-minutes"] <= 5
    assert gate["steps"][0]["uses"] == "actions/checkout@v4"
    gate_steps = [step for step in gate["steps"] if "--require-ready" in step.get("run", "")]
    assert len(gate_steps) == 1
    assert gate_steps[0].get("if") == "github.ref_name == 'v1.1.2'"
    assert gate_steps[0]["run"].strip() == (
        "python3 scripts/validate_v112_site_release_gate.py --require-ready"
    )


def test_every_release_preparation_job_waits_for_seven_site_preflight() -> None:
    jobs = _jobs()
    for job_name in ("arm64-validation", "cookiecloud-upstream-validation"):
        dependencies = jobs[job_name].get("needs", [])
        if isinstance(dependencies, str):
            dependencies = [dependencies]
        assert GATE in dependencies, job_name

    release_dependencies = jobs["release"]["needs"]
    assert "arm64-validation" in release_dependencies
    assert "cookiecloud-upstream-validation" in release_dependencies


def test_second_release_gate_remains_before_registry_and_github_mutations() -> None:
    steps = _jobs()["release"]["steps"]
    gate_index = next(i for i, step in enumerate(steps) if "--require-ready" in step.get("run", ""))
    assert steps[gate_index].get("if") == "github.ref_name == 'v1.1.2'"
    earlier = steps[:gate_index]
    assert any(step.get("name") == "Validate release tag" for step in earlier)
    for step in earlier:
        text = step.get("run", "")
        assert "gh release create" not in text
        assert "docker push" not in text
        assert "docker/build-push-action" not in step.get("uses", "")
