"""A failed Release may be completed only using its already-pushed digest."""

from __future__ import annotations

from pathlib import Path

import yaml  # type: ignore[import-untyped]

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/immutable-v100-recovery.yml"
DIAGNOSTICS = ROOT / ".github/workflows/immutable-v100-diagnostics.yml"


def test_recovery_reuses_the_original_tag_and_diagnostic_digest() -> None:
    recovery = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    diagnostics = yaml.load(DIAGNOSTICS.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert recovery["on"]["push"]["paths"] == [str(WORKFLOW.relative_to(ROOT))]
    assert recovery["on"]["push"]["branches"] == ["main"]
    assert recovery["env"]["IMAGE"] == diagnostics["env"]["IMAGE"]
    assert recovery["env"]["RELEASE_COMMIT"] == diagnostics["env"]["RELEASE_COMMIT"]
    assert recovery["env"]["RELEASE_TAG"] == diagnostics["env"]["RELEASE_TAG"]
    assert recovery["env"]["IMAGE"].endswith(recovery["env"]["IMAGE_DIGEST"])
    assert recovery["env"]["PREVIOUS_TAG"] == "v0.1.9"

    jobs = recovery["jobs"]
    assert set(jobs) == {"immutable-amd64-qemu", "immutable-native-arm64", "release-assets"}
    assert set(jobs["release-assets"]["needs"]) == {
        "immutable-amd64-qemu",
        "immutable-native-arm64",
    }
    assert jobs["immutable-amd64-qemu"]["permissions"] == {
        "contents": "read",
        "packages": "read",
    }
    assert jobs["immutable-native-arm64"]["permissions"] == {
        "contents": "read",
        "packages": "read",
    }
    assert jobs["release-assets"]["permissions"] == {
        "contents": "write",
        "packages": "write",
    }
    for job in jobs.values():
        checkout = next(
            step for step in job["steps"] if step.get("uses", "").startswith("actions/checkout")
        )
        assert checkout["with"]["ref"] == "v1.0.0"


def test_recovery_fails_closed_before_release_or_channel_mutation() -> None:
    recovery = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    jobs = recovery["jobs"]
    amd64_runs = "\n".join(step.get("run", "") for step in jobs["immutable-amd64-qemu"]["steps"])
    arm64_runs = "\n".join(step.get("run", "") for step in jobs["immutable-native-arm64"]["steps"])
    assert 'check-release-upgrade.sh "$IMAGE"' in amd64_runs
    assert 'check-immutable-image-runtime.sh "$IMAGE" linux/amd64' in amd64_runs
    assert 'check-immutable-image-runtime.sh "$IMAGE" linux/arm64' in amd64_runs
    assert 'check-immutable-image-runtime.sh "$IMAGE" linux/arm64' in arm64_runs
    assert "refs/tags/$RELEASE_TAG^{}" in amd64_runs
    assert 'gh release view "$RELEASE_TAG"' in amd64_runs
    assert 'verify_release_platforms.py --image "$IMAGE"' in amd64_runs

    asset_steps = jobs["release-assets"]["steps"]
    names = [step.get("name", "") for step in asset_steps]
    assert names.index(
        "Fail closed if tagged commit, release baseline or immutable image changed"
    ) < names.index("Generate SPDX SBOM from the unchanged published digest")
    assert names.index("Verify all local Release assets before publishing") < names.index(
        "Publish the original tag's Release without rebuilding the image"
    )
    assert names.index(
        "Publish the original tag's Release without rebuilding the image"
    ) < names.index("Download and verify the actually published Release assets")
    assert names.index("Download and verify the actually published Release assets") < names.index(
        "Advance channels only after identical digest and asset readback"
    )
    all_runs = "\n".join(step.get("run", "") for job in jobs.values() for step in job["steps"])
    assert all_runs.count("docker buildx imagetools create") == 1
    assert "docker build --" not in all_runs
    assert "git push" not in all_runs
    assert "gh release edit" not in all_runs
    assert "docker buildx imagetools create \\n" not in amd64_runs
    assert "docker buildx imagetools create \\n" not in arm64_runs
