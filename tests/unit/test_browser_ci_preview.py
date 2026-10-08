"""Keep the browser CI runner aligned with the production-preview E2E command."""

from __future__ import annotations

import json
from pathlib import Path

import yaml  # type: ignore[import-untyped]

ROOT = Path(__file__).resolve().parents[2]


def test_browser_ci_does_not_prestart_dev_server_on_preview_port() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    steps = workflow["jobs"]["browser-e2e"]["steps"]
    command = next(
        step["run"] for step in steps if step.get("name") == "Run browser prototype checks"
    )

    package = json.loads((ROOT / "frontend/package.json").read_text())
    assert "check-prototype-preview.cjs" in package["scripts"]["test:e2e"]
    assert "--strictPort" in (ROOT / "scripts/check-prototype-preview.cjs").read_text()
    assert "pnpm test:e2e" in command
    assert "pnpm dev" not in command, "Preview owns port 5173; a dev server would block it"
    assert "vite_pid" not in command
    assert "packbreaker-vite.log" not in command
    all_commands = "\n".join(step.get("run", "") for step in steps)
    assert "check-unpack-v2-ui.cjs" in all_commands
    assert "PB_UNPACK_V2_E2E_PRODUCTION=1 node scripts/check-unpack-v2-ui.cjs" in all_commands
    v2_script = (ROOT / "scripts/check-unpack-v2-ui.cjs").read_text()
    assert "['preview']" in v2_script
    assert "--strictPort" in v2_script
    assert "E2E_UNMOCKED_API" in v2_script
    assert "check-web-approval-e2e.cjs" not in all_commands
