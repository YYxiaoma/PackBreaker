#!/usr/bin/env bash
# Exercise the official Docker Hub *current-image* alias with a real Docker
# inspect, while keeping the candidate image and all filesystem state local.
# Does not request or execute a container upgrade.
set -Eeuo pipefail

candidate_image="${1:?usage: check-official-dockerhub-updater.sh <candidate-image>}"
sandbox="$(mktemp -d "${TMPDIR:-/tmp}/.packbreaker-official-image.XXXXXXXX")"
chmod 700 "$sandbox"
suffix="${GITHUB_RUN_ID:-local}-$$-$RANDOM"
container="packbreaker-official-image-$suffix"
local_alias="yyxiaoma01/packbreaker:ci-official-image-$suffix"
target_digest="ghcr.io/yyxiaoma/packbreaker@sha256:$(printf '8%.0s' {1..64})"

cleanup() {
  docker rm --force "$container" >/dev/null 2>&1 || true
  docker image rm "$local_alias" >/dev/null 2>&1 || true
  if [[ -d "$sandbox" && ! -L "$sandbox" && \
        "$(basename "$sandbox")" == .packbreaker-official-image.* && \
        "$sandbox" != / ]]; then
    sudo rm -rf -- "$sandbox" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

mkdir -p "$sandbox/config" "$sandbox/downloads" "$sandbox/downloads2"
sudo chown 1026:100 "$sandbox/config"
chmod 755 "$sandbox/downloads" "$sandbox/downloads2"

# This is a local-only tag pointing to the just-built candidate image.
# Never pull the Docker Hub latest tag or push anything to that namespace.
docker tag "$candidate_image" "$local_alias"
test "$(docker image inspect "$local_alias" --format '{{.Id}}')" = \
  "$(docker image inspect "$candidate_image" --format '{{.Id}}')"

docker run --detach \
  --name "$container" \
  --restart unless-stopped \
  --publish "127.0.0.1:38000:8000" \
  --env PUID=1026 \
  --env PGID=100 \
  --env PACKBREAKER_TIMEZONE=Asia/Shanghai \
  --volume "$sandbox/config:/config" \
  --volume "$sandbox/downloads:/downloads" \
  --volume "$sandbox/downloads2:/downloads2" \
  --volume /var/run/docker.sock:/var/run/docker.sock \
  --health-interval 1s \
  --health-timeout 2s \
  --health-retries 10 \
  --health-start-period 1s \
  --security-opt no-new-privileges:true \
  "$local_alias" >/dev/null

for attempt in $(seq 1 90); do
  if [[ "$(docker inspect "$container" --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}')" == healthy ]]; then
    break
  fi
  if (( attempt == 90 )); then
    # Bootstrap logs may contain a one-time password; never print them in CI.
    echo "Isolated official-image container did not become healthy" >&2
    exit 1
  fi
  sleep 1
done

original_id="$(docker inspect "$container" --format '{{.Id}}')"

# Compare real Docker API inspect output and the actual production
# build_replacement_plan. Drop exec's root privileges to those of PID 1
# before inspecting the Docker socket (docker exec -u 0 masks bad permissions).
docker exec -i --user 0:0 \
  --env PB_CI_CONTAINER="$container" \
  --env PB_CI_IMAGE="$local_alias" \
  --env PB_CI_TARGET="$target_digest" \
  "$container" python - <<'PY'
import copy
import os
from pathlib import Path

from backend.app.infrastructure.docker_updater import (
    DockerEngineClient, DockerUpdaterError, build_replacement_plan,
)

fields = {
    line.split(":", 1)[0]: line.split(":", 1)[1].split()
    for line in Path("/proc/1/status").read_text().splitlines()
    if ":" in line
}
assert fields["Uid"][1] == "1026", fields["Uid"]
assert fields["Gid"][1] == "100", fields["Gid"]
os.setgroups([int(group) for group in fields["Groups"]])
os.setgid(100)
os.setuid(1026)

with DockerEngineClient() as docker:
    container = docker.inspect_container(os.environ["PB_CI_CONTAINER"])
    old_image = docker.inspect_image(container["Image"])

assert container["Config"]["Image"] == os.environ["PB_CI_IMAGE"]
target = os.environ["PB_CI_TARGET"]
allowed = "ghcr.io/yyxiaoma/packbreaker"
plan = build_replacement_plan(
    container, old_image, target_image=target,
    allowed_image=allowed, preserve_docker_socket=True,
)
assert plan.old_image_reference == os.environ["PB_CI_IMAGE"]
assert plan.create_payload["Image"] == target
assert plan.container_id == container["Id"]
assert plan.container_name == os.environ["PB_CI_CONTAINER"]
assert set(plan.create_payload["Env"]) >= {
    "PUID=1026", "PGID=100", "PACKBREAKER_TIMEZONE=Asia/Shanghai",
}
expected = {"/config", "/downloads", "/downloads2", "/var/run/docker.sock"}
actual = {bind.split(":", 2)[1] for bind in plan.create_payload["HostConfig"]["Binds"]}
assert expected <= actual, actual
assert plan.create_payload["HostConfig"]["PortBindings"]["8000/tcp"] == [
    {"HostIp": "127.0.0.1", "HostPort": "38000"}
]
assert plan.create_payload["HostConfig"]["RestartPolicy"]["Name"] == "unless-stopped"
assert any(
    value.startswith("no-new-privileges")
    for value in plan.create_payload["HostConfig"]["SecurityOpt"]
)
for suspicious in (
    "yyxiaoma01/packbreaker-evil:latest",
    "docker.io/attacker/packbreaker:latest",
):
    other = copy.deepcopy(container)
    other["Config"]["Image"] = suspicious
    try:
        build_replacement_plan(
            other, old_image, target_image=target, allowed_image=allowed
        )
    except DockerUpdaterError as exc:
        assert exc.code == "UPGRADE_CURRENT_IMAGE_UNTRUSTED"
    else:
        raise AssertionError("Untrusted image alias was accepted")

# A Docker Hub mirror is allowed as *current* image, never the upgrade target.
try:
    build_replacement_plan(
        container, old_image,
        target_image="docker.io/yyxiaoma01/packbreaker@sha256:" + "8" * 64,
        allowed_image=allowed,
    )
except DockerUpdaterError as exc:
    assert exc.code == "UPGRADE_TARGET_IMAGE_UNTRUSTED"
else:
    raise AssertionError("Untrusted upgrade destination was accepted")

print("real Docker inspect official current-image alias gate passed")
PY

test "$(docker inspect "$container" --format '{{.Id}}')" = "$original_id"
test "$(docker inspect "$container" --format '{{.State.Running}}')" = true
echo "isolated official Docker Hub current-image gate passed without container replacement"
