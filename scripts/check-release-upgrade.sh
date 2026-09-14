#!/usr/bin/env bash
set -euo pipefail

candidate_image="${1:?usage: check-release-upgrade.sh <candidate-image>}"
baseline_payload="$(python scripts/validate_release_baseline.py --json)"
baseline_image="$(python -c 'import json,sys; print(json.load(sys.stdin)["immutable_image"])' <<<"$baseline_payload")"
baseline_revision="$(python -c 'import json,sys; print(json.load(sys.stdin)["alembic_revision"])' <<<"$baseline_payload")"
baseline_version="$(python -c 'import json,sys; print(json.load(sys.stdin)["version"])' <<<"$baseline_payload")"

runtime_user="$(id -u):$(id -g)"
suffix="${GITHUB_RUN_ID:-local}-$$"
baseline_container="packbreaker-upgrade-baseline-$suffix"
candidate_container="packbreaker-upgrade-candidate-$suffix"
rollback_container="packbreaker-upgrade-rollback-$suffix"
config_dir="$PWD/.ci-release-upgrade-$suffix-config"
data_dir="$PWD/.ci-release-upgrade-$suffix-data"
probe_value="baseline:$baseline_version"

cleanup() {
  docker rm --force "$baseline_container" "$candidate_container" "$rollback_container" >/dev/null 2>&1 || true
  rm -rf "$config_dir" "$data_dir"
}
trap cleanup EXIT

wait_ready() {
  local container="$1"
  for attempt in $(seq 1 60); do
    if docker exec --user "$runtime_user" "$container" python -m backend.app.healthcheck >/dev/null 2>&1; then
      return 0
    fi
    if [ "$attempt" -eq 60 ]; then
      docker logs "$container" || true
      return 1
    fi
    sleep 1
  done
}

assert_probe() {
  local container="$1"
  docker exec --user "$runtime_user" -e PB_RELEASE_PROBE="$probe_value" "$container" \
    python -c 'import os,sqlite3; c=sqlite3.connect("/config/packbreaker.db", timeout=30); row=c.execute("SELECT probe_value FROM release_upgrade_probe WHERE probe_key=\"upgrade\"").fetchone(); assert row == (os.environ["PB_RELEASE_PROBE"],), row'
}

mkdir -p "$config_dir" "$data_dir"
chmod 700 "$config_dir"
chmod 755 "$data_dir"

echo "Pull immutable baseline: $baseline_image"
docker pull "$baseline_image"

echo "Start baseline release and create synthetic compatibility state"
docker run --detach \
  --name "$baseline_container" \
  --user "$runtime_user" \
  --volume "$config_dir:/config" \
  --volume "$data_dir:/data" \
  "$baseline_image" >/dev/null
wait_ready "$baseline_container"
docker exec --user "$runtime_user" -e PB_RELEASE_PROBE="$probe_value" "$baseline_container" \
  python -c 'import os,sqlite3; c=sqlite3.connect("/config/packbreaker.db", timeout=30); c.execute("CREATE TABLE release_upgrade_probe (probe_key TEXT PRIMARY KEY, probe_value TEXT NOT NULL)"); c.execute("INSERT INTO release_upgrade_probe(probe_key, probe_value) VALUES (\"upgrade\", ?)", (os.environ["PB_RELEASE_PROBE"],)); c.commit()'
docker exec --user "$runtime_user" -e PB_BASELINE_REVISION="$baseline_revision" "$baseline_container" \
  python -c 'import os,sqlite3; c=sqlite3.connect("/config/packbreaker.db"); row=c.execute("SELECT version_num FROM alembic_version").fetchone(); assert row == (os.environ["PB_BASELINE_REVISION"],), row'
docker exec --user "$runtime_user" "$baseline_container" python -m backend.app.maintenance backup >/dev/null
mapfile -t baseline_backups < <(find "$config_dir/backups" -maxdepth 1 -type f -name 'packbreaker-*.db' -printf '%f\n')
test "${#baseline_backups[@]}" -eq 1
baseline_backup="${baseline_backups[0]}"
docker stop "$baseline_container" >/dev/null
docker rm "$baseline_container" >/dev/null

echo "Start current candidate against baseline config"
docker run --detach \
  --name "$candidate_container" \
  --user "$runtime_user" \
  --volume "$config_dir:/config" \
  --volume "$data_dir:/data" \
  "$candidate_image" >/dev/null
wait_ready "$candidate_container"
assert_probe "$candidate_container"
docker exec --user "$runtime_user" "$candidate_container" \
  python -c 'import sqlite3; c=sqlite3.connect("/config/packbreaker.db"); print("candidate_revision=" + c.execute("SELECT version_num FROM alembic_version").fetchone()[0])'
docker stop "$candidate_container" >/dev/null
docker rm "$candidate_container" >/dev/null

echo "Restore baseline backup with the baseline image, then prove rollback readiness"
docker run --rm \
  --user "$runtime_user" \
  --volume "$config_dir:/config" \
  --volume "$data_dir:/data" \
  "$baseline_image" \
  python -m backend.app.maintenance verify-backup "/config/backups/$baseline_backup" >/dev/null
docker run --rm \
  --user "$runtime_user" \
  --volume "$config_dir:/config" \
  --volume "$data_dir:/data" \
  "$baseline_image" \
  python -m backend.app.maintenance restore-backup "/config/backups/$baseline_backup" \
  --confirm-replace-current-database >/dev/null
docker run --detach \
  --name "$rollback_container" \
  --user "$runtime_user" \
  --volume "$config_dir:/config" \
  --volume "$data_dir:/data" \
  "$baseline_image" >/dev/null
wait_ready "$rollback_container"
assert_probe "$rollback_container"
docker exec --user "$runtime_user" -e PB_BASELINE_REVISION="$baseline_revision" "$rollback_container" \
  python -c 'import os,sqlite3; c=sqlite3.connect("/config/packbreaker.db"); row=c.execute("SELECT version_num FROM alembic_version").fetchone(); assert row == (os.environ["PB_BASELINE_REVISION"],), row'

echo "release upgrade/rollback gate passed: $baseline_version -> $candidate_image -> $baseline_version"
