"""v1.0.2 recovery verifies its previously pushed digest without replacing it."""

from __future__ import annotations

import subprocess
from pathlib import Path

import yaml  # type: ignore[import-untyped]

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "immutable-v102-recovery.yml"
RELEASE_SHA = "43be7bf77276f42d1dca45a4b78132d6a7044b30"
IMAGE_DIGEST = "sha256:eb6083864d3a60f0dc926ac58cc27ccb18405df5f766d66cbc533a0252539fd5"
PREVIOUS_DIGEST = "sha256:3fb0ab8cee39b245d8a2b6fedae1336afa41ff18cf380d5f448a1b1fef832cb2"


def test_recovery_keeps_both_release_identities_and_three_independent_runners() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    workflow = yaml.safe_load(text)
    triggers = workflow.get("on", workflow.get(True))
    assert triggers["push"]["branches"] == ["main"]
    assert triggers["push"]["paths"] == [".github/workflows/immutable-v102-recovery.yml"]
    env = workflow["env"]
    assert env["RELEASE_TAG"] == "v1.0.2"
    assert env["RELEASE_VERSION"] == "1.0.2"
    assert env["RELEASE_COMMIT"] == RELEASE_SHA
    assert env["IMAGE_DIGEST"] == IMAGE_DIGEST
    assert env["IMAGE"] == f"ghcr.io/yyxiaoma/packbreaker@{IMAGE_DIGEST}"
    assert env["PREVIOUS_TAG"] == "v1.0.1"
    assert env["PREVIOUS_DIGEST"] == PREVIOUS_DIGEST
    assert set(workflow["jobs"]) == {
        "immutable-amd64",
        "immutable-qemu-arm64",
        "immutable-native-arm64",
        "release-assets",
    }
    jobs = workflow["jobs"]
    assert jobs["immutable-amd64"]["runs-on"] == "ubuntu-24.04"
    assert jobs["immutable-qemu-arm64"]["runs-on"] == "ubuntu-24.04"
    assert jobs["immutable-native-arm64"]["runs-on"] == "ubuntu-24.04-arm"
    assert jobs["release-assets"]["needs"] == [
        "immutable-amd64",
        "immutable-qemu-arm64",
        "immutable-native-arm64",
    ]
    for job in jobs.values():
        checkout = next(step for step in job["steps"] if step.get("uses") == "actions/checkout@v4")
        assert checkout["with"]["ref"] == "v1.0.2"


def test_recovery_never_rebuilds_and_checks_asset_readback_before_mutable_channels() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    workflow = yaml.safe_load(text)
    jobs = workflow["jobs"]
    for forbidden in (
        "docker buildx build",
        "docker build --",
        "docker push ",
        "docker/build-push-action",
        "git tag -",
        "git push origin refs/tags",
    ):
        assert forbidden not in text
    amd64 = "\n".join(step.get("run", "") for step in jobs["immutable-amd64"]["steps"])
    assert "scripts/check-immutable-image-runtime.sh" in amd64
    assert "scripts/check-release-upgrade.sh" in amd64
    qemu = "\n".join(step.get("run", "") for step in jobs["immutable-qemu-arm64"]["steps"])
    native = "\n".join(step.get("run", "") for step in jobs["immutable-native-arm64"]["steps"])
    assert "linux/arm64" in qemu and "linux/amd64" not in qemu
    assert "linux/arm64" in native
    names = [step.get("name", "") for step in jobs["release-assets"]["steps"]]
    assert names.index("Download and verify the actually published Release assets") < names.index(
        "Advance channels only after identical digest and asset readback"
    )
    assert "PENDING_REAL_VALIDATION" in text
    assert "have not passed full real-world seeding acceptance" in text
    assert "35875040822" in text
    for job in jobs.values():
        for step in job["steps"]:
            if "run" in step:
                subprocess.run(["bash", "-n"], input=step["run"], text=True, check=True)
