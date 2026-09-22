#!/usr/bin/env bash
set -euo pipefail

candidate_image="${1:?usage: check-release-upgrade.sh <candidate-image> [--synthetic-arm64-baseline]}"
baseline_mode="${2:-formal}"
if [[ "$baseline_mode" != formal && "$baseline_mode" != --synthetic-arm64-baseline ]]; then
  echo "Invalid baseline mode: $baseline_mode" >&2
  exit 2
fi
baseline_payload="$(python scripts/validate_release_baseline.py --json)"
baseline_image="$(python -c 'import json,sys; print(json.load(sys.stdin)["immutable_image"])' <<<"$baseline_payload")"
baseline_revision="$(python -c 'import json,sys; print(json.load(sys.stdin)["alembic_revision"])' <<<"$baseline_payload")"
baseline_version="$(python -c 'import json,sys; print(json.load(sys.stdin)["version"])' <<<"$baseline_payload")"

runtime_user="$(id -u):$(id -g)"
# Never assume ownership of a predictable working-tree path during cleanup.
sandbox="$(mktemp -d "${TMPDIR:-/tmp}/.packbreaker-release-upgrade.XXXXXXXX")"
chmod 700 "$sandbox"
suffix="${GITHUB_RUN_ID:-local}-$$-${RANDOM}"
baseline_container="packbreaker-upgrade-baseline-$suffix"
candidate_container="packbreaker-upgrade-candidate-$suffix"
rollback_container="packbreaker-upgrade-rollback-$suffix"
config_dir="$sandbox/config"
data_dir="$sandbox/data"
baseline_build_dir="$sandbox/synthetic-baseline-build"
probe_value="baseline:$baseline_version"

cleanup() {
  docker rm --force "$baseline_container" "$candidate_container" "$rollback_container" >/dev/null 2>&1 || true
  if [[ -d "$sandbox" && ! -L "$sandbox" && \
        "$(basename "$sandbox")" == .packbreaker-release-upgrade.* && \
        "$sandbox" != / ]]; then
    rm -rf -- "$sandbox"
  fi
}
trap cleanup EXIT

wait_ready() {
  local container="$1"
  for attempt in $(seq 1 60); do
    if docker exec --user "$runtime_user" "$container" python -m backend.app.healthcheck >/dev/null 2>&1; then
      return 0
    fi
    if [ "$attempt" -eq 60 ]; then
      # Startup logs can contain the one-time administrator password. Never
      # print raw container output in a release CI failure path.
      echo "Isolated upgrade container readiness failed; inspect the disposable test environment without publishing bootstrap logs" >&2
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

if [[ "$baseline_mode" == --synthetic-arm64-baseline ]]; then
  # The previous official release has no ARM64 image. Never pull or relabel the
  # official AMD64 baseline as an ARM64 upgrade. This exercise uses two distinct
  # local ARM64 image IDs with the SAME application version; it is not a real
  # cross-version migration or an upgrade from a published ARM64 release.
  test "$(uname -m)" = aarch64
  test "$(docker image inspect "$candidate_image" --format '{{.Os}}/{{.Architecture}}')" = linux/arm64
  mkdir -p "$baseline_build_dir"
  cat >"$baseline_build_dir/Dockerfile" <<'DOCKERFILE'
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
LABEL org.packbreaker.ci.synthetic-arm64-baseline="true"
DOCKERFILE
  baseline_image="packbreaker:ci-arm64-release-upgrade-baseline-$suffix"
  docker build --platform linux/arm64 \
    --build-arg BASE_IMAGE="$candidate_image" \
    --tag "$baseline_image" "$baseline_build_dir" >/dev/null
  test "$(docker image inspect "$baseline_image" --format '{{.Os}}/{{.Architecture}}')" = linux/arm64
  test "$(docker image inspect "$baseline_image" --format '{{.Id}}')" != \
    "$(docker image inspect "$candidate_image" --format '{{.Id}}')"
  baseline_version="$(docker run --rm --network none "$baseline_image" python -c 'from backend.app.versioning import app_version; print(app_version())')"
  candidate_version="$(docker run --rm --network none "$candidate_image" python -c 'from backend.app.versioning import app_version; print(app_version())')"
  test "$baseline_version" = "$candidate_version"
  probe_value="synthetic-arm64-same-version:$baseline_version"
  echo "Synthetic ARM64 baseline: two different local image identities at the same version (NOT a published ARM64 upgrade)"
else
  echo "Pull immutable formal baseline: $baseline_image"
  docker pull "$baseline_image"
  # Check the running package identity before starting containers or touching
  # the ephemeral database. A stale baseline-version image is not an upgrade.
  expected_candidate_version="$(python -c 'from scripts.validate_release_baseline import project_version; print(project_version())')"
  candidate_version="$(docker run --rm --network none "$candidate_image" python -c 'from backend.app.versioning import app_version; print(app_version())')"
  if [[ "$candidate_version" != "$expected_candidate_version" || "$candidate_version" == "$baseline_version" ]]; then
    echo "Invalid candidate version: expected a newer project release, not the baseline" >&2
    exit 1
  fi
fi

mkdir -p "$config_dir" "$data_dir"
chmod 700 "$config_dir"
chmod 755 "$data_dir"

echo "Start baseline release and create synthetic compatibility state"
docker run --detach \
  --name "$baseline_container" \
  --network none \
  --user "$runtime_user" \
  --volume "$config_dir:/config" \
  --volume "$data_dir:/data" \
  "$baseline_image" >/dev/null
wait_ready "$baseline_container"
if [[ "$baseline_mode" == --synthetic-arm64-baseline ]]; then
  baseline_revision="$(docker exec --user "$runtime_user" "$baseline_container" \
    python -c 'import sqlite3; c=sqlite3.connect("/config/packbreaker.db"); print(c.execute("SELECT version_num FROM alembic_version").fetchone()[0])')"
  test -n "$baseline_revision"
fi
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
  --network none \
  --user "$runtime_user" \
  --volume "$config_dir:/config" \
  --volume "$data_dir:/data" \
  "$candidate_image" >/dev/null
wait_ready "$candidate_container"
assert_probe "$candidate_container"
docker exec --user "$runtime_user" "$candidate_container" \
  python -c 'import sqlite3; from alembic.script import ScriptDirectory; from backend.app.infrastructure.runtime import make_migration_config; head=ScriptDirectory.from_config(make_migration_config("sqlite:////config/packbreaker.db")).get_current_head(); c=sqlite3.connect("file:/config/packbreaker.db?mode=ro",uri=True); row=c.execute("SELECT version_num FROM alembic_version").fetchone(); assert row == (head,), (row,head); print("candidate_revision="+head)'
if [[ "$baseline_mode" == --synthetic-arm64-baseline ]]; then
  docker exec --user "$runtime_user" -e PB_BASELINE_REVISION="$baseline_revision" "$candidate_container" \
    python -c 'import os,sqlite3; c=sqlite3.connect("/config/packbreaker.db"); row=c.execute("SELECT version_num FROM alembic_version").fetchone(); assert row == (os.environ["PB_BASELINE_REVISION"],), row'
fi
docker stop "$candidate_container" >/dev/null
docker rm "$candidate_container" >/dev/null

echo "Restore baseline backup with the baseline image, then prove rollback readiness"
docker run --rm \
  --network none \
  --user "$runtime_user" \
  --volume "$config_dir:/config" \
  --volume "$data_dir:/data" \
  "$baseline_image" \
  python -m backend.app.maintenance verify-backup "/config/backups/$baseline_backup" >/dev/null
docker run --rm \
  --network none \
  --user "$runtime_user" \
  --volume "$config_dir:/config" \
  --volume "$data_dir:/data" \
  "$baseline_image" \
  python -m backend.app.maintenance restore-backup "/config/backups/$baseline_backup" \
  --confirm-replace-current-database >/dev/null
docker run --detach \
  --name "$rollback_container" \
  --network none \
  --user "$runtime_user" \
  --volume "$config_dir:/config" \
  --volume "$data_dir:/data" \
  "$baseline_image" >/dev/null
wait_ready "$rollback_container"
assert_probe "$rollback_container"
docker exec --user "$runtime_user" -e PB_BASELINE_REVISION="$baseline_revision" "$rollback_container" \
  python -c 'import os,sqlite3; c=sqlite3.connect("/config/packbreaker.db"); row=c.execute("SELECT version_num FROM alembic_version").fetchone(); assert row == (os.environ["PB_BASELINE_REVISION"],), row'

if [[ "$baseline_mode" == --synthetic-arm64-baseline ]]; then
  echo "synthetic same-version ARM64 image replacement/backup restore gate passed (NOT a formal cross-version upgrade): $baseline_version"
else
  echo "release upgrade/rollback gate passed: $baseline_version -> $candidate_image -> $baseline_version"
fi
