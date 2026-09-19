#!/usr/bin/env bash
set -Eeuo pipefail

# Never point these commands at a user's existing containers, mounts or credentials.
candidate_image="${1:?usage: check-arm64-real-downloaders-e2e.sh <local-candidate-image>}"
test "$(uname -m)" = aarch64
test "$(docker image inspect "$candidate_image" --format '{{.Os}}/{{.Architecture}}')" = linux/arm64

suffix="${GITHUB_RUN_ID:-local}-$$"
phase="init"
report_failure() {
  local exit_code="$?"
  printf '::error file=scripts/check-arm64-real-downloaders-e2e.sh::ARM64 isolated downloader failure: phase=%s exit=%s\n' \
    "$phase" "$exit_code" >&2
  exit "$exit_code"
}
trap report_failure ERR
qb_name="packbreaker-arm64-qb-e2e-$suffix"
tr_name="packbreaker-arm64-tr-e2e-$suffix"
sandbox="$(mktemp -d "$PWD/.ci-arm64-real-downloaders.XXXXXXXX")"
test "${sandbox#"$PWD/.ci-arm64-real-downloaders."}" != "$sandbox"
mkdir -p "$sandbox/qb-config" "$sandbox/tr-config" "$sandbox/data"
chmod 700 "$sandbox" "$sandbox/qb-config" "$sandbox/tr-config"
chmod 755 "$sandbox/data"

cleanup() {
  docker rm -f "$qb_name" "$tr_name" >/dev/null 2>&1 || true
  sudo rm -rf -- "$sandbox"
}
trap cleanup EXIT

check_image() {
  local image="$1"
  docker pull --platform linux/arm64 "$image" >/dev/null
  test "$(docker image inspect "$image" --format '{{.Os}}/{{.Architecture}}')" = linux/arm64
}

wait_port() {
  local container="$1" port="$2"
  for attempt in $(seq 1 90); do
    if docker inspect "$container" --format '{{.State.Running}}' 2>/dev/null | grep -qx true && \
      docker run --rm --network "container:$container" "$candidate_image" \
        python -c 'import socket,sys; s=socket.create_connection(("127.0.0.1",int(sys.argv[1])),timeout=2); s.close()' "$port" \
        >/dev/null 2>&1; then
      return 0
    fi
    if [ "$attempt" -eq 90 ]; then
      echo "Isolated downloader WebAPI did not become ready" >&2
      return 1
    fi
    sleep 1
  done
}

run_probe() {
  local container="$1" kind="$2" password="$3"
  docker run --rm --interactive \
    --network "container:$container" \
    --user "$(id -u):$(id -g)" \
    --volume "$sandbox/data:/downloads" \
    --env "PACKBREAKER_CI_DOWNLOADER_PASSWORD=$password" \
    "$candidate_image" python - "$kind" --data-root /downloads \
    < scripts/check_arm64_downloaders_e2e.py
}

# The qBittorrent temporary password is generated for this disposable container.
# Consume it privately; never echo docker logs or the temporary credential to CI.
qb_image="lscr.io/linuxserver/qbittorrent:5.2.3-libtorrentv1"
phase="qb-image-pull"
check_image "$qb_image"
phase="qb-container-start"
docker run --detach --name "$qb_name" --network none \
  --env "PUID=$(id -u)" --env "PGID=$(id -g)" \
  --volume "$sandbox/qb-config:/config" \
  --volume "$sandbox/data:/downloads" \
  "$qb_image" >/dev/null
phase="qb-port-ready"
wait_port "$qb_name" 8080
qb_password=""
phase="qb-ephemeral-password"
for attempt in $(seq 1 40); do
  qb_password="$(docker logs "$qb_name" 2>&1 | python3 -c 'import re,sys; text=sys.stdin.read(); found=re.findall(r"temporary password[^\n]*?:\s*(\S+)",text,re.I); print(found[-1] if found else "")')"
  if [ -n "$qb_password" ]; then break; fi
  sleep 1
done
test -n "$qb_password" || { echo "No ephemeral qBittorrent password was generated" >&2; exit 1; }
phase="qb-real-api"
run_probe "$qb_name" qbittorrent "$qb_password"
unset qb_password
docker rm --force "$qb_name" >/dev/null

tr_image="lscr.io/linuxserver/transmission:4.1.3"
phase="tr-image-pull"
check_image "$tr_image"
tr_password="$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')"
phase="tr-container-start"
docker run --detach --name "$tr_name" --network none \
  --env "PUID=$(id -u)" --env "PGID=$(id -g)" \
  --env USER=packbreaker --env "PASS=$tr_password" \
  --volume "$sandbox/tr-config:/config" \
  --volume "$sandbox/data:/downloads" \
  "$tr_image" >/dev/null
phase="tr-port-ready"
wait_port "$tr_name" 9091
phase="tr-real-api"
run_probe "$tr_name" transmission "$tr_password"
unset tr_password
echo 'Isolated native ARM64 qBittorrent and Transmission Docker API tests passed'
