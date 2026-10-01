"""The upstream CookieCloud E2E must remain isolated and narrowly triggered."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "cookiecloud-upstream-e2e.yml"


def _workflow() -> dict[Any, Any]:
    value = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_cookiecloud_upstream_e2e_uses_official_image_and_api_root() -> None:
    workflow = _workflow()
    job = workflow["jobs"]["official-server"]
    commands = "\n".join(str(step.get("run", "")) for step in job["steps"])
    assert "easychen/cookiecloud:latest" in commands
    assert "--env API_ROOT=/cc" in commands
    assert "127.0.0.1:18088:8088" in commands
    assert "scripts/check_cookiecloud_upstream_docker.py" in commands


def test_cookiecloud_upstream_e2e_only_runs_for_cookiecloud_related_changes() -> None:
    workflow = _workflow()
    triggers = workflow.get("on", workflow.get(True))
    assert isinstance(triggers, dict)
    push = triggers["push"]
    assert set(push["branches"]) == {"main", "candidate/**"}
    paths = set(push["paths"])
    assert "backend/app/infrastructure/cookiecloud.py" in paths
    assert "scripts/check_cookiecloud_upstream_docker.py" in paths
    assert ".github/workflows/cookiecloud-upstream-e2e.yml" in paths
