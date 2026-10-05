from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_v109_image_bootstrap_reads_puid_pgid_without_compose_user_override() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")

    assert "USER root" in dockerfile
    assert "USER packbreaker" not in dockerfile
    assert "backend.app.container_healthcheck" in dockerfile
    assert "\n    user:" not in compose
    assert "group_add:" not in compose
    assert 'PUID: "${PUID:-1000}"' in compose
    assert 'PGID: "${PGID:-1000}"' in compose
    assert "backend.app.container_healthcheck" in compose


def test_release_workflow_publishes_same_version_to_ghcr_and_docker_hub() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    sync = (ROOT / ".github" / "workflows" / "sync-release-channels.yml").read_text(
        encoding="utf-8"
    )

    assert "docker.io/yyxiaoma01/packbreaker" in workflow
    assert "DOCKERHUB_USERNAME" in workflow
    assert "DOCKERHUB_TOKEN" in workflow
    assert "Verify Docker Hub immutable mirror matches the GHCR release digest" in workflow
    assert "Docker Hub $channel channel digest mismatch" in workflow
    assert "docker.io/yyxiaoma01/packbreaker" in sync
    assert "DOCKERHUB_USERNAME" in sync
    assert "DOCKERHUB_TOKEN" in sync
    assert 'test "$dockerhub_version_digest" = "$digest"' in sync


def test_ci_exercises_default_root_bootstrap_without_user_or_group_add() -> None:
    candidate = (ROOT / ".github" / "workflows" / "candidate-docker-e2e.yml").read_text(
        encoding="utf-8"
    )
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    probe = (ROOT / "scripts" / "check-v109-default-entrypoint.sh").read_text(encoding="utf-8")

    assert "check-v109-default-entrypoint.sh packbreaker:candidate-docker-e2e" in candidate
    assert "check-v109-default-entrypoint.sh packbreaker:ci-arm64" in ci
    assert "docker run --detach --name" in probe
    assert (
        "--user"
        not in probe.split('docker run --detach --name "$container"', 1)[1].split(
            '"$image" >/dev/null', 1
        )[0]
    )
    assert "--group-add" not in probe
    assert "docker exec --interactive --user 0:0" in probe
    assert "Path('/proc/1/status')" in probe
    assert (
        'test "$(docker image inspect packbreaker:ci --format \'{{.Config.User}}\')" = "root"' in ci
    )
    container_smoke = ci.split("- name: Smoke test image and health endpoint", 1)[1].split(
        "  updater-e2e:", 1
    )[0]
    assert '--env PUID="$runtime_uid"' in container_smoke
    assert '--env PGID="$runtime_gid"' in container_smoke
    assert '--user "$(id -u):$(id -g)"' not in container_smoke
    assert 'Path("/proc/1/status")' in container_smoke


def test_v1011_candidate_delivery_is_dual_registry_and_manual_only() -> None:
    workflow = (ROOT / ".github" / "workflows" / "candidate-v1011-image-delivery.yml").read_text(
        encoding="utf-8"
    )

    assert "workflow_dispatch:" in workflow
    assert "candidate_sha:" in workflow
    assert "push:" not in workflow.split("permissions:", 1)[0]
    assert "ghcr.io/yyxiaoma/packbreaker" in workflow
    assert "docker.io/yyxiaoma01/packbreaker" in workflow
    assert "DOCKERHUB_USERNAME" in workflow
    assert "DOCKERHUB_TOKEN" in workflow
    assert "Refuse existing candidate tags in either registry" in workflow
    assert "Build and push one multi-arch candidate to both registries" in workflow
    assert "Verify both candidate tags resolve to the same immutable index" in workflow
    assert "linux/amd64,linux/arm64" in workflow
    assert "CANDIDATE_VERSION: 1.0.11" in workflow
    assert "candidate-v1.0.11-" in workflow
    assert ":latest" not in workflow
    assert ":stable" not in workflow
    assert "gh release" not in workflow
    assert 'test "$dockerhub_digest" = "$digest"' in workflow
    assert '"$DOCKERHUB_IMAGE@$digest" linux/amd64 v1.0.11' in workflow
    assert '"$DOCKERHUB_IMAGE@$digest" linux/arm64 v1.0.11' in workflow
