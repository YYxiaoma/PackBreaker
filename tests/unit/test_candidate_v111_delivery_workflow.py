from __future__ import annotations

from pathlib import Path

import yaml  # type: ignore[import-untyped]

WORKFLOW = (
    Path(__file__).resolve().parents[2] / ".github/workflows/candidate-v111-image-delivery.yml"
)


def test_v111_candidate_delivery_is_manual_and_sha_bound() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    workflow = yaml.safe_load(text)
    triggers = workflow.get("on", workflow.get(True))

    assert set(triggers) == {"workflow_dispatch"}
    assert triggers["workflow_dispatch"]["inputs"]["candidate_sha"]["required"]
    assert workflow["env"]["CANDIDATE_VERSION"] == "1.1.1"
    assert workflow["env"]["GHCR_IMAGE"] == "ghcr.io/yyxiaoma/packbreaker"
    assert workflow["env"]["DOCKERHUB_IMAGE"] == "docker.io/yyxiaoma01/packbreaker"
    assert set(workflow["jobs"]) == {"publish-candidate", "native-arm64-candidate"}

    publish = workflow["jobs"]["publish-candidate"]
    assert "refs/heads/main" in publish["if"]
    assert publish["permissions"]["actions"] == "read"
    assert publish["permissions"]["packages"] == "write"
    assert "ref: candidate/v1.1.1" in text
    assert 'test "$(git rev-parse refs/remotes/origin/candidate/v1.1.1)" = "$CANDIDATE_SHA"' in text
    assert "head_sha={sha}&event=push" in text
    assert 'run.get("conclusion") == "success"' in text
    assert '("ci.yml", "candidate-docker-e2e.yml")' in text
    assert 'run.get("head_branch") == "candidate/v1.1.1"' in text
    assert "Require verified v1.1.0 immutable upgrade baseline" in text
    assert "9c436d036a165fc419e1347ee954680cabd68a8b8aa358908ebd48328360d255" in text
    assert 'tag="candidate-v1.1.1-${CANDIDATE_SHA:0:12}"' in text
    assert "Refuse existing candidate tags in either registry" in text


def test_v111_candidate_delivery_does_not_advance_formal_channels() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    workflow = yaml.safe_load(text)
    arm = workflow["jobs"]["native-arm64-candidate"]
    assert arm["needs"] == "publish-candidate"
    assert arm["runs-on"] == "ubuntu-24.04-arm"
    assert "linux/amd64,linux/arm64" in text
    assert "verify_release_platforms.py" in text
    assert "check-immutable-image-runtime.sh" in text
    assert "candidate-v1.1.1-" in text
    assert "refs/remotes/origin/candidate/v1.1.1" in text
    assert "ref: main" not in text
    assert ":latest" not in text
    assert ":stable" not in text
    assert "git push" not in text
    assert "gh release create" not in text
    assert not any("release" in job for job in workflow["jobs"])


def test_required_sha_bound_e2e_runs_for_candidate_push_and_checks_formal_upgrade() -> None:
    workflows = WORKFLOW.parents[0]
    e2e = yaml.safe_load((workflows / "candidate-docker-e2e.yml").read_text(encoding="utf-8"))
    ci = yaml.safe_load((workflows / "ci.yml").read_text(encoding="utf-8"))
    e2e_triggers = e2e.get("on", e2e.get(True))
    ci_triggers = ci.get("on", ci.get(True))
    assert "candidate/**" in e2e_triggers["push"]["branches"]
    assert "candidate/**" in ci_triggers["push"]["branches"]
    watched = set(e2e_triggers["push"]["paths"])
    assert {"backend/**", "Dockerfile", "release-baseline.json"}.issubset(watched)
    e2e_steps = e2e["jobs"]["updater-e2e"]["steps"]
    upgrade = next(
        step
        for step in e2e_steps
        if step.get("name") == "Exercise previous-release upgrade and rollback"
    )
    updater = next(
        step
        for step in e2e_steps
        if step.get("name") == "Exercise real updater, rollback, and Compose-label preservation"
    )
    for step in (upgrade, updater):
        assert step["if"] == "steps.release_relation.outputs.candidate_newer == 'true'"
    assert "scripts/check-release-upgrade.sh" in upgrade["run"]
    assert "scripts/check-updater-e2e.sh" in updater["run"]


def test_official_docker_hub_current_image_has_real_docker_candidate_gate() -> None:
    workflows = WORKFLOW.parents[0]
    e2e = yaml.safe_load((workflows / "candidate-docker-e2e.yml").read_text(encoding="utf-8"))
    watched = set(e2e.get("on", e2e.get(True))["push"]["paths"])
    assert "scripts/check-official-dockerhub-updater.sh" in watched
    steps = e2e["jobs"]["updater-e2e"]["steps"]
    mirror = next(
        step for step in steps if "check-official-dockerhub-updater.sh" in step.get("run", "")
    )
    # Mirrors the user's Compose, no upgrade and no formal-version condition.
    assert mirror.get("if") is None
    assert mirror["run"] == (
        "bash scripts/check-official-dockerhub-updater.sh packbreaker:candidate-docker-e2e"
    )
    assert next(i for i, step in enumerate(steps) if step is mirror) > next(
        i for i, step in enumerate(steps) if step.get("name") == "Verify candidate image identity"
    )
    ci = yaml.safe_load((workflows / "ci.yml").read_text(encoding="utf-8"))
    arm_steps = ci["jobs"]["arm64"]["steps"]
    arm_gate = next(
        step for step in arm_steps if "check-official-dockerhub-updater.sh" in step.get("run", "")
    )
    assert arm_gate.get("if") is None
    assert (
        arm_gate["run"] == "bash scripts/check-official-dockerhub-updater.sh packbreaker:ci-arm64"
    )
    assert next(i for i, step in enumerate(arm_steps) if step is arm_gate) > next(
        i
        for i, step in enumerate(arm_steps)
        if step.get("name") == "Build native ARM64 runtime image"
    )
    script = (
        Path(__file__).resolve().parents[2] / "scripts/check-official-dockerhub-updater.sh"
    ).read_text(encoding="utf-8")
    for required in (
        "yyxiaoma01/packbreaker:ci-official-image-",
        "127.0.0.1:38000:8000",
        "PUID=1026",
        "PGID=100",
        "/downloads",
        "/downloads2",
        "/var/run/docker.sock",
        "DockerEngineClient",
        "build_replacement_plan",
        "preserve_docker_socket=True",
        "UPGRADE_CURRENT_IMAGE_UNTRUSTED",
        "UPGRADE_TARGET_IMAGE_UNTRUSTED",
        "original_id",
    ):
        assert required in script
    assert "docker push" not in script
    assert "docker pull" not in script
    assert "start_upgrade(" not in script
