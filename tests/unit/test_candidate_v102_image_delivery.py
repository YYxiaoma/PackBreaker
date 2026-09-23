"""Keep v1.0.2 GHCR test image source-pinned and isolated from formal release."""

from __future__ import annotations

import subprocess
from pathlib import Path

import yaml  # type: ignore[import-untyped]

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "candidate-v102-image-delivery.yml"
REVIEWED_SHA = "0cd1d47da41c6bc97951c80b8bba7dac6d9dd27b"
BRANCH = "candidate/v1.0.2-eight-sites-config"


def test_delivery_pins_reviewed_source_and_cannot_touch_formal_tags() -> None:
    source = WORKFLOW.read_text(encoding="utf-8")
    workflow = yaml.safe_load(source)
    triggers = workflow.get("on", workflow.get(True))
    assert set(triggers) == {"push"}
    assert triggers["push"]["branches"] == [BRANCH]
    assert triggers["push"]["paths"] == [".github/workflows/candidate-v102-image-delivery.yml"]
    assert workflow["env"]["CANDIDATE_SHA"] == REVIEWED_SHA
    assert workflow["env"]["CANDIDATE_VERSION"] == "1.0.2"
    assert set(workflow["jobs"]) == {"publish-candidate", "native-arm64-candidate"}
    publisher = workflow["jobs"]["publish-candidate"]
    native = workflow["jobs"]["native-arm64-candidate"]
    assert f"github.ref == 'refs/heads/{BRANCH}'" in publisher["if"]
    assert publisher["permissions"] == {"contents": "read", "packages": "write"}
    assert native["permissions"] == {"contents": "read", "packages": "read"}
    assert native["needs"] == "publish-candidate"
    assert native["runs-on"] == "ubuntu-24.04-arm"
    assert 'git checkout --detach "$CANDIDATE_SHA"' in source
    assert f'git merge-base --is-ancestor "$CANDIDATE_SHA" origin/{BRANCH}' in source
    assert "candidate-v1.0.2-" in source
    assert "linux/amd64,linux/arm64" in source
    assert "scripts/verify_release_platforms.py" in source
    assert "needs.publish-candidate.outputs.digest" in source
    for forbidden in ("gh release create", "imagetools create", ":stable", ":latest"):
        assert forbidden not in source


def test_delivery_refuses_overwrite_and_checks_unchanged_digest_on_arm64() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["publish-candidate"]["steps"]
    names = [step.get("name", "") for step in steps]
    for first, second in (
        ("Verify candidate source identity and version", "Refuse existing candidate image"),
        ("Refuse existing candidate image", "Build and push candidate index"),
        ("Build and push candidate index", "Verify published candidate image"),
    ):
        assert names.index(first) < names.index(second)
    verify = steps[names.index("Verify published candidate image")]["run"]
    assert 'test "$tag_digest" = "$digest"' in verify
    assert "scripts/check-immutable-image-runtime.sh" in verify
    native = workflow["jobs"]["native-arm64-candidate"]["steps"]
    native_script = "\n".join(step.get("run", "") for step in native)
    assert 'test "$(git rev-parse HEAD)" = "$CANDIDATE_SHA"' in native_script
    assert "linux/arm64" in native_script
    for job in workflow["jobs"].values():
        for step in job["steps"]:
            if "run" in step:
                subprocess.run(["bash", "-n"], input=step["run"], text=True, check=True)
