#!/usr/bin/env bash
set -euo pipefail

image="${1:?usage: check-v109-default-entrypoint.sh <image>}"
runtime_uid="$(id -u)"
runtime_gid="$(id -g)"
suffix="${GITHUB_RUN_ID:-local}-$$-$RANDOM"
container="packbreaker-v109-entrypoint-$suffix"
config_volume="packbreaker-v109-config-$suffix"

cleanup() {
  docker rm --force "$container" >/dev/null 2>&1 || true
  docker volume rm --force "$config_volume" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker volume create "$config_volume" >/dev/null
docker run --detach --name "$container" \
  --network none \
  --env "PUID=$runtime_uid" \
  --env "PGID=$runtime_gid" \
  --volume "$config_volume:/config" \
  --volume /var/run/docker.sock:/var/run/docker.sock \
  "$image" >/dev/null

test "$(docker inspect "$container" --format '{{.Config.User}}')" = root
group_add="$(docker inspect "$container" --format '{{json .HostConfig.GroupAdd}}')"
test "$group_add" = null || test "$group_add" = '[]'

ready=0
for _attempt in $(seq 1 60); do
  if [[ "$(docker inspect "$container" --format '{{.State.Health.Status}}')" == healthy ]]; then
    ready=1
    break
  fi
  if [[ "$(docker inspect "$container" --format '{{.State.Running}}')" != true ]]; then
    break
  fi
  sleep 1
done
if [[ "$ready" -ne 1 ]]; then
  docker logs "$container" >&2 || true
  echo '::error::v1.0.9 default root bootstrap did not become healthy' >&2
  exit 1
fi

docker exec --interactive --user 0:0 \
  --env "PB_EXPECT_UID=$runtime_uid" \
  --env "PB_EXPECT_GID=$runtime_gid" \
  "$container" python - <<'PY'
import os
from pathlib import Path

from backend.app.container_entrypoint import _docker_socket_supplementary_groups

fields = {}
for line in Path('/proc/1/status').read_text(encoding='utf-8').splitlines():
    if ':' in line:
        key, value = line.split(':', 1)
        fields[key] = value.strip()

expected_uid = int(os.environ['PB_EXPECT_UID'])
expected_gid = int(os.environ['PB_EXPECT_GID'])
uids = [int(item) for item in fields['Uid'].split()]
gids = [int(item) for item in fields['Gid'].split()]
groups = [int(item) for item in fields.get('Groups', '').split()]
assert uids == [expected_uid] * 4, (uids, expected_uid)
assert gids == [expected_gid] * 4, (gids, expected_gid)
expected_groups = _docker_socket_supplementary_groups(expected_uid, expected_gid)
assert groups == expected_groups, (groups, expected_groups)
config = Path('/config')
st = config.stat()
assert st.st_uid == expected_uid, (st.st_uid, expected_uid)
assert st.st_gid == expected_gid, (st.st_gid, expected_gid)
assert st.st_mode & 0o777 == 0o700, oct(st.st_mode & 0o777)
assert Path('/proc/1/cmdline').read_bytes().startswith(b'python\x00-m\x00backend.app.container_entrypoint')
PY
