#!/usr/bin/env bash
# Isolated, no-port runtime smoke test of an immutable GHCR release image.
# The current Release job runs amd64 natively and arm64 through QEMU; its
# native ARM64 candidate job remains a separate gate.
set -euo pipefail

if [[ "$#" -ne 4 ]]; then
  echo "Usage: check-immutable-image-runtime.sh <ghcr-image@sha256:digest> <linux/amd64|linux/arm64> <vX.Y.Z> <40-char-commit>" >&2
  exit 2
fi
image="$1"
target_platform="$2"
release_tag="$3"
release_commit="$4"
if [[ ! "$image" =~ ^ghcr\.io/[a-z0-9._/-]+@sha256:[0-9a-f]{64}$ ]] || \
   [[ "$image" == *..* ]] || \
   [[ "$target_platform" != linux/amd64 && "$target_platform" != linux/arm64 ]] || \
   [[ ! "$release_tag" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || \
   [[ ! "$release_commit" =~ ^[0-9a-f]{40}$ ]]; then
  echo "Invalid immutable image/platform/tag/commit runtime smoke inputs" >&2
  exit 2
fi
release_version="${release_tag#v}"

# All writes are restricted to a fresh mktemp-owned directory. No host ports,
# Docker socket mounts, user config, PT sites or downloaders are involved.
sandbox="$(mktemp -d "${TMPDIR:-/tmp}/.packbreaker-immutable-runtime.XXXXXXXX")"
chmod 700 "$sandbox"
container="packbreaker-image-smoke-${GITHUB_RUN_ID:-local}-$$-$RANDOM"
cleanup() {
  docker rm --force "$container" >/dev/null 2>&1 || true
  if [[ -d "$sandbox" && ! -L "$sandbox" && \
        "$(basename "$sandbox")" == .packbreaker-immutable-runtime.* && \
        "$sandbox" != / ]]; then
    rm -rf -- "$sandbox"
  fi
}
trap cleanup EXIT
mkdir -m 700 "$sandbox/config" "$sandbox/data"

docker pull --platform "$target_platform" "$image" >/dev/null
docker create \
  --name "$container" \
  --platform "$target_platform" \
  --network none \
  --user "$(id -u):$(id -g)" \
  --volume "$sandbox/config:/config" \
  --volume "$sandbox/data:/data" \
  "$image" >/dev/null

resolved_id="$(docker inspect "$container" --format '{{.Image}}')"
test "$(docker image inspect "$resolved_id" --format '{{.Os}}/{{.Architecture}}')" = "$target_platform"
test "$(docker image inspect "$resolved_id" --format '{{index .Config.Labels "org.opencontainers.image.version"}}')" = "$release_version"
test "$(docker image inspect "$resolved_id" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')" = "$release_commit"

docker start "$container" >/dev/null
ready=0
for attempt in $(seq 1 90); do
  if docker exec --user "$(id -u):$(id -g)" "$container" \
    python -m backend.app.healthcheck >/dev/null 2>&1; then
    ready=1
    break
  fi
  if [[ "$(docker inspect "$container" --format '{{.State.Running}}')" != true ]]; then
    break
  fi
  sleep 2
done
if [[ "$ready" -ne 1 ]]; then
  echo "Immutable image failed isolated runtime readiness: $target_platform" >&2
  exit 1
fi

docker exec --user "$(id -u):$(id -g)" "$container" \
  python -m backend.app.maintenance preflight >/dev/null
actual_version="$(docker exec --user "$(id -u):$(id -g)" "$container" \
  python -c 'from backend.app.versioning import app_version; print(app_version())')"
test "$actual_version" = "$release_version"
actual_machine="$(docker exec --user "$(id -u):$(id -g)" "$container" \
  python -c 'import platform; print(platform.machine())')"
case "$target_platform:$actual_machine" in
  linux/amd64:x86_64|linux/arm64:aarch64) ;;
  *) echo "Unexpected runtime CPU architecture: $target_platform / $actual_machine" >&2; exit 1 ;;
esac
docker exec --user "$(id -u):$(id -g)" "$container" \
  python -m backend.app.maintenance backup >/dev/null
mapfile -t backups < <(find "$sandbox/config/backups" -maxdepth 1 -type f -name 'packbreaker-*.db' -printf '%f\n')
test "${#backups[@]}" -eq 1
echo "Immutable GHCR image runtime smoke passed: $target_platform $release_tag (ephemeral config and database)"
