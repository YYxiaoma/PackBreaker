"""Release candidate identity must agree across source, build and API surfaces."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

from backend.app.versioning import app_version

ROOT = Path(__file__).resolve().parents[2]


def test_release_candidate_version_surfaces_match() -> None:
    project_version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]["version"]
    frontend_version = json.loads((ROOT / "frontend/package.json").read_text(encoding="utf-8"))[
        "version"
    ]
    openapi_version = json.loads((ROOT / "frontend/openapi.json").read_text(encoding="utf-8"))[
        "info"
    ]["version"]
    lock_packages = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))["package"]
    root_versions = [item["version"] for item in lock_packages if item["name"] == "packbreaker"]
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    docker_versions = re.findall(r"^ARG VERSION=(\S+)$", dockerfile, flags=re.MULTILINE)

    assert re.fullmatch(r"\d+\.\d+\.\d+", project_version)
    assert project_version == frontend_version == openapi_version == app_version()
    assert root_versions == [project_version]
    assert docker_versions == [project_version]


def test_release_baseline_is_not_newer_than_project_version() -> None:
    candidate = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]
    baseline = json.loads((ROOT / "release-baseline.json").read_text(encoding="utf-8"))
    assert baseline["tag"] == f"v{baseline['version']}"
    # Immediately after publishing, project and formal baseline share the release
    # version. Once development advances, the project version may be newer.
    assert tuple(map(int, baseline["version"].split("."))) <= tuple(map(int, candidate.split(".")))
    assert baseline["immutable_image"] == f"{baseline['image']}@{baseline['image_digest']}"
