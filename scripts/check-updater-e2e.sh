#!/usr/bin/env bash
set -Eeuo pipefail

candidate_image="${1:?usage: check-updater-e2e.sh <candidate-image> [--synthetic-arm64-baseline]}"
baseline_mode="${2:-formal}"
if [[ "$baseline_mode" != formal && "$baseline_mode" != --synthetic-arm64-baseline ]]; then
  echo "Invalid baseline mode: $baseline_mode" >&2
  exit 2
fi
baseline_payload="$(python3 scripts/validate_release_baseline.py --json)"
baseline_image="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["immutable_image"])' <<<"$baseline_payload")"
baseline_version="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["version"])' <<<"$baseline_payload")"
candidate_version="$(python3 -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')"
if [[ "$baseline_mode" == --synthetic-arm64-baseline ]]; then
  # This explicitly synthetic mode is a same-version container replacement/rollback
  # exercise using two distinct locally built ARM64 image identities, NOT an upgrade
  # from the published formal release baseline and NOT a cross-version migration claim.
  baseline_version="$candidate_version"
fi
runtime_uid="$(id -u)"
runtime_gid="$(id -g)"
runtime_user="$runtime_uid:$runtime_gid"
suffix="${GITHUB_RUN_ID:-local}-$$"
registry_port="15000"
registry_container="packbreaker-updater-registry-$suffix"
registry_repo="127.0.0.1:${registry_port}/packbreaker"
local_baseline_tag="$registry_repo:baseline"
local_candidate_tag="$registry_repo:candidate"
local_fault_tag="$registry_repo:fault"
fault_build_dir="$PWD/.ci-updater-e2e-$suffix-fault-build"
success_config="$PWD/.ci-updater-e2e-$suffix-success-config"
success_data="$PWD/.ci-updater-e2e-$suffix-success-data"
rollback_config="$PWD/.ci-updater-e2e-$suffix-rollback-config"
rollback_data="$PWD/.ci-updater-e2e-$suffix-rollback-data"
transient_config="$PWD/.ci-updater-e2e-$suffix-transient-config"
transient_data="$PWD/.ci-updater-e2e-$suffix-transient-data"
success_main="packbreaker-updater-success-$suffix"
success_helper="packbreaker-updater-success-helper-$suffix"
rollback_main="packbreaker-updater-rollback-$suffix"
rollback_helper="packbreaker-updater-rollback-helper-$suffix"
transient_main="packbreaker-updater-transient-$suffix"
success_request="success-$suffix"
rollback_request="rollback-$suffix"
transient_request="transient-$suffix"

cleanup() {
  docker ps -aq --filter "name=$suffix" | xargs -r docker rm --force >/dev/null 2>&1 || true
  sudo rm -rf \
    "$fault_build_dir" \
    "$success_config" "$success_data" \
    "$rollback_config" "$rollback_data" \
    "$transient_config" "$transient_data" >/dev/null 2>&1 || true
}

workflow_escape() {
  local value="$1"
  value="${value//'%'/'%25'}"
  value="${value//$'\r'/'%0D'}"
  value="${value//$'\n'/'%0A'}"
  printf '%s' "$value"
}

report_failure() {
  local exit_code="$?"
  local line="${BASH_LINENO[0]:-0}"
  local command="${BASH_COMMAND:-unknown}"
  local state_summary=""
  local input_summary=""
  local docker_summary=""
  local annotation=""
  set +e
  if sudo test -f "$transient_config/transient-updater/state.json"; then
    state_summary="$(sudo python3 -c 'import json,sys; p=json.load(open(sys.argv[1], encoding="utf-8")); print(json.dumps({"phase":p.get("phase"),"message":p.get("message"),"backup_database_file":p.get("backup_database_file")}, ensure_ascii=False, separators=(",",":")))' "$transient_config/transient-updater/state.json" 2>/dev/null)"
  fi
  input_summary="$(python3 -c 'import json,sys; print(json.dumps({"backup_database_file":sys.argv[1],"backup_manifest_file":sys.argv[2]}, ensure_ascii=False, separators=(",",":")))' "${transient_backup_db:-}" "${transient_backup_manifest:-}" 2>/dev/null)"
  docker_summary="$(docker ps -a --filter "name=$transient_main" --format '{{.Names}}={{.ID}}:{{.Status}}' 2>/dev/null | tr '\n' ';')"
  annotation="exit=$exit_code command=$command transient_state=$state_summary transient_input=$input_summary docker=$docker_summary"
  printf '::error file=scripts/check-updater-e2e.sh,line=%s::%s\n' \
    "$line" "$(workflow_escape "$annotation")"
  exit "$exit_code"
}

trap cleanup EXIT
trap report_failure ERR

wait_registry() {
  for attempt in $(seq 1 30); do
    if curl --fail --silent "http://127.0.0.1:${registry_port}/v2/" >/dev/null; then
      return 0
    fi
    if [ "$attempt" -eq 30 ]; then
      docker logs "$registry_container" || true
      return 1
    fi
    sleep 1
  done
}

wait_app_ready() {
  local container="$1"
  local exec_user="${2:-$runtime_user}"
  for attempt in $(seq 1 90); do
    if docker exec --user "$exec_user" "$container" \
      python -m backend.app.healthcheck >/dev/null 2>&1; then
      return 0
    fi
    if ! docker inspect "$container" >/dev/null 2>&1; then
      echo "容器在 readiness 前消失: $container" >&2
      return 1
    fi
    if [ "$attempt" -eq 90 ]; then
      docker logs "$container" || true
      return 1
    fi
    sleep 1
  done
}

wait_app_ready_after_switch() {
  local container="$1"
  local exec_user="$2"
  local previous_id="$3"
  for attempt in $(seq 1 180); do
    local current_id transient_phase
    current_id="$(docker inspect "$container" --format '{{.Id}}' 2>/dev/null || true)"
    if [ -n "$current_id" ] && [ "$current_id" != "$previous_id" ] && \
      docker exec --user "$exec_user" "$container" \
        python -m backend.app.healthcheck >/dev/null 2>&1; then
      return 0
    fi
    transient_phase="$(sudo python3 -c 'import json,sys; p=json.load(open(sys.argv[1], encoding="utf-8")); print(p.get("phase") or "")' "$transient_config/transient-updater/state.json" 2>/dev/null || true)"
    case "$transient_phase" in
      rolled_back|failed|manual_recovery_required)
        echo "一次性 updater 已进入终态但未完成容器替换: $transient_phase" >&2
        return 1
        ;;
    esac
    if [ "$attempt" -eq 180 ]; then
      docker ps -a --filter "name=$container" || true
      docker logs "$container" 2>/dev/null || true
      return 1
    fi
    sleep 1
  done
}

wait_transient_helper_cleanup() {
  for attempt in $(seq 1 30); do
    local helper_present request_present
    helper_present="$(docker ps -a --format '{{.Names}}' | grep --fixed-strings "${transient_main}-updater-once-" || true)"
    request_present="$(sudo find "$transient_config/transient-updater/requests" -maxdepth 1 -type f -name '*.json' -print -quit 2>/dev/null || true)"
    if [ -z "$helper_present" ] && [ -z "$request_present" ]; then
      return 0
    fi
    if [ "$attempt" -eq 30 ]; then
      docker ps -a --filter "name=${transient_main}-updater-once-" || true
      sudo find "$transient_config/transient-updater/requests" -maxdepth 1 -type f -name '*.json' -print 2>/dev/null || true
      return 1
    fi
    sleep 1
  done
}

wait_transient_terminal() {
  for attempt in $(seq 1 30); do
    local status_json phase request_id
    status_json="$(
      docker exec --user 0:0 \
        --env PB_TARGET_CONTAINER="$transient_main" \
        --env PB_ALLOWED_IMAGE="$registry_repo" \
        "$transient_main" \
        python -c 'import json,os; from pathlib import Path; from backend.app.infrastructure.transient_updater import TransientUpdaterLauncher; launcher=TransientUpdaterLauncher(config_dir=Path("/config"), target_container=os.environ["PB_TARGET_CONTAINER"], allowed_image=os.environ["PB_ALLOWED_IMAGE"]); print(json.dumps(launcher.status().as_dict(), sort_keys=True))'
    )"
    phase="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["phase"])' <<<"$status_json")"
    request_id="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("request_id") or "")' <<<"$status_json")"
    case "$phase" in
      succeeded)
        test "$request_id" = "$transient_request"
        return 0
        ;;
      rolled_back|failed|manual_recovery_required)
        echo "$status_json" >&2
        return 1
        ;;
    esac
    if [ "$attempt" -eq 30 ]; then
      echo "$status_json" >&2
      return 1
    fi
    sleep 1
  done
}

wait_docker_healthy() {
  local container="$1"
  for attempt in $(seq 1 90); do
    local status
    status="$(docker inspect "$container" --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}')"
    if [ "$status" = "healthy" ]; then
      return 0
    fi
    if [ "$status" = "unhealthy" ]; then
      docker logs "$container" || true
      return 1
    fi
    if [ "$attempt" -eq 90 ]; then
      docker inspect "$container" || true
      return 1
    fi
    sleep 1
  done
}

repo_digest_for_tag() {
  local tag="$1"
  docker image inspect "$tag" --format '{{json .RepoDigests}}' | \
    python3 -c 'import json,sys; repo=sys.argv[1] + "@"; refs=json.load(sys.stdin) or []; matches=[item for item in refs if item.startswith(repo)]; assert len(matches) == 1, matches; print(matches[0])' "$registry_repo"
}

prepare_case() {
  local main_container="$1"
  local config_dir="$2"
  local data_dir="$3"
  local host_port="$4"
  local probe_value="$5"

  mkdir -p "$config_dir" "$data_dir"
  chmod 700 "$config_dir"
  chmod 755 "$data_dir"

  docker run --detach \
    --name "$main_container" \
    --restart unless-stopped \
    --user 0:0 \
    --env PUID="$runtime_uid" \
    --env PGID="$runtime_gid" \
    --env PACKBREAKER_TIMEZONE=Asia/Shanghai \
    --publish "127.0.0.1:${host_port}:8000" \
    --volume "$config_dir:/config" \
    --volume "$data_dir:/data" \
    --health-interval 1s \
    --health-timeout 2s \
    --health-retries 10 \
    --health-start-period 1s \
    "$local_baseline_tag" >/dev/null

  wait_app_ready "$main_container"
  wait_docker_healthy "$main_container"
  test "$(docker inspect "$main_container" --format '{{.Image}}')" = "$baseline_image_id"
  test "$(docker exec --user "$runtime_user" "$main_container" python -c 'from backend.app.versioning import app_version; print(app_version())')" = "$baseline_version"

  docker exec --user "$runtime_user" --env PB_RELEASE_PROBE="$probe_value" "$main_container" \
    python -c 'import os,sqlite3; c=sqlite3.connect("/config/packbreaker.db", timeout=30); c.execute("CREATE TABLE release_upgrade_probe (probe_key TEXT PRIMARY KEY, probe_value TEXT NOT NULL)"); c.execute("INSERT INTO release_upgrade_probe(probe_key, probe_value) VALUES (\"upgrade\", ?)", (os.environ["PB_RELEASE_PROBE"],)); c.commit(); c.close()'
  docker exec --user "$runtime_user" "$main_container" \
    python -m backend.app.maintenance backup >/dev/null

  mapfile -t case_backups < <(find "$config_dir/backups" -maxdepth 1 -type f -name 'packbreaker-*.db' -printf '%f\n')
  test "${#case_backups[@]}" -eq 1
  case_backup_db="${case_backups[0]}"
  case_backup_manifest="${case_backup_db%.db}.json"
  test -f "$config_dir/backups/$case_backup_manifest"
}

start_helper() {
  local helper_container="$1"
  local main_container="$2"
  local config_dir="$3"

  docker run --detach \
    --name "$helper_container" \
    --user 0:0 \
    --env PUID="$runtime_uid" \
    --env PGID="$runtime_gid" \
    --env PACKBREAKER_CONFIG_DIR=/config \
    --env PACKBREAKER_UPDATER_TARGET_CONTAINER="$main_container" \
    --env PACKBREAKER_UPDATER_ALLOWED_IMAGE="$registry_repo" \
    --volume "$config_dir:/config" \
    --volume /var/run/docker.sock:/var/run/docker.sock \
    "$candidate_image" \
    python -m backend.app.updater_helper >/dev/null

  for attempt in $(seq 1 30); do
    if [ -S "$config_dir/updater/updater.sock" ] && [ -f "$config_dir/updater/token" ]; then
      docker exec "$helper_container" python -c 'from pathlib import Path; from backend.app.infrastructure.updater_protocol import UpdaterClient; s=UpdaterClient(Path("/config/updater/updater.sock"), Path("/config/updater/token")).status(); assert s.phase == "idle", s; print(s.phase)' >/dev/null
      return 0
    fi
    if ! docker inspect "$helper_container" >/dev/null 2>&1 || [ "$(docker inspect "$helper_container" --format '{{.State.Running}}')" != "true" ]; then
      docker logs "$helper_container" || true
      return 1
    fi
    if [ "$attempt" -eq 30 ]; then
      docker logs "$helper_container" || true
      return 1
    fi
    sleep 1
  done
}

trigger_upgrade() {
  local helper_container="$1"
  local request_id="$2"
  local target_image="$3"
  local backup_db="$4"
  local backup_manifest="$5"

  docker exec \
    --env PB_REQUEST_ID="$request_id" \
    --env PB_CURRENT_VERSION="$baseline_version" \
    --env PB_TARGET_VERSION="$candidate_version" \
    --env PB_TARGET_IMAGE="$target_image" \
    --env PB_BACKUP_DB="$backup_db" \
    --env PB_BACKUP_MANIFEST="$backup_manifest" \
    "$helper_container" \
    python -c 'import os; from pathlib import Path; from backend.app.infrastructure.updater_protocol import UpdaterClient,UpgradeHelperRequest; client=UpdaterClient(Path("/config/updater/updater.sock"), Path("/config/updater/token"), timeout_seconds=5); status=client.start_upgrade(UpgradeHelperRequest(request_id=os.environ["PB_REQUEST_ID"], current_version=os.environ["PB_CURRENT_VERSION"], target_version=os.environ["PB_TARGET_VERSION"], target_image=os.environ["PB_TARGET_IMAGE"], backup_database_file=os.environ["PB_BACKUP_DB"], backup_manifest_file=os.environ["PB_BACKUP_MANIFEST"], grace_seconds=0.5)); assert status.phase == "accepted", status; print(status.phase)' >/dev/null
}

wait_helper_terminal() {
  local helper_container="$1"
  local expected_phase="$2"
  for attempt in $(seq 1 180); do
    local status_json phase
    status_json="$(docker exec "$helper_container" python -c 'import json; from pathlib import Path; from backend.app.infrastructure.updater_protocol import UpdaterClient; s=UpdaterClient(Path("/config/updater/updater.sock"), Path("/config/updater/token"), timeout_seconds=5).status(); print(json.dumps(s.as_dict(), sort_keys=True))')"
    phase="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["phase"])' <<<"$status_json")"
    case "$phase" in
      succeeded|rolled_back|failed|manual_recovery_required)
        echo "$status_json"
        test "$phase" = "$expected_phase"
        return
        ;;
    esac
    if [ "$attempt" -eq 180 ]; then
      echo "$status_json" >&2
      docker logs "$helper_container" || true
      return 1
    fi
    sleep 1
  done
}

assert_probe() {
  local container="$1"
  local expected="$2"
  docker exec --user "$runtime_user" --env PB_EXPECTED_PROBE="$expected" "$container" \
    python -c 'import os,sqlite3; c=sqlite3.connect("/config/packbreaker.db", timeout=30); row=c.execute("SELECT probe_value FROM release_upgrade_probe WHERE probe_key=\"upgrade\"").fetchone(); assert row == (os.environ["PB_EXPECTED_PROBE"],), row; c.close()'
}

assert_quiesced_backup_exists() {
  local config_dir="$1"
  mapfile -t helper_backups < <(sudo find "$config_dir/backups/pre-upgrade-helper" -maxdepth 1 -type f -name 'packbreaker-*.db' -printf '%f\n' 2>/dev/null || true)
  test "${#helper_backups[@]}" -eq 1
  sudo test -f "$config_dir/backups/pre-upgrade-helper/${helper_backups[0]%.db}.json"
}

prepare_transient_case() {
  mkdir -p "$transient_config" "$transient_data"
  chmod 700 "$transient_config"
  chmod 755 "$transient_data"

  docker run --detach \
    --name "$transient_main" \
    --restart unless-stopped \
    --user 0:0 \
    --env PUID=0 \
    --env PGID=0 \
    --env PACKBREAKER_TIMEZONE=Asia/Shanghai \
    --label com.docker.compose.project=packbreaker-e2e \
    --label com.docker.compose.service=packbreaker \
    --label com.docker.compose.container-number=1 \
    --publish 127.0.0.1:18083:8000 \
    --volume "$transient_config:/config" \
    --volume "$transient_data:/data" \
    --volume /var/run/docker.sock:/var/run/docker.sock \
    --health-interval 1s \
    --health-timeout 2s \
    --health-retries 10 \
    --health-start-period 1s \
    "$local_candidate_tag" >/dev/null

  wait_app_ready "$transient_main" 0:0
  wait_docker_healthy "$transient_main"
  transient_initial_container_id="$(docker inspect "$transient_main" --format '{{.Id}}')"
  docker exec --user 0:0 --env PB_RELEASE_PROBE=transient-original "$transient_main" \
    python -c 'import os,sqlite3; c=sqlite3.connect("/config/packbreaker.db", timeout=30); c.execute("CREATE TABLE release_upgrade_probe (probe_key TEXT PRIMARY KEY, probe_value TEXT NOT NULL)"); c.execute("INSERT INTO release_upgrade_probe(probe_key, probe_value) VALUES (\"upgrade\", ?)", (os.environ["PB_RELEASE_PROBE"],)); c.commit(); c.close()'
  docker exec --user 0:0 "$transient_main" python -m backend.app.maintenance backup >/dev/null
  mapfile -t transient_backups < <(sudo find "$transient_config/backups" -maxdepth 1 -type f -name 'packbreaker-*.db' -printf '%f\n' 2>/dev/null || true)
  test "${#transient_backups[@]}" -eq 1
  transient_backup_db="${transient_backups[0]}"
  transient_backup_manifest="${transient_backup_db%.db}.json"
}

trigger_transient_upgrade() {
  docker exec --user 0:0 \
    --env PB_REQUEST_ID="$transient_request" \
    --env PB_CURRENT_VERSION="$candidate_version" \
    --env PB_TARGET_VERSION="$candidate_version" \
    --env PB_TARGET_IMAGE="$candidate_target" \
    --env PB_BACKUP_DB="$transient_backup_db" \
    --env PB_BACKUP_MANIFEST="$transient_backup_manifest" \
    --env PB_TARGET_CONTAINER="$transient_main" \
    --env PB_ALLOWED_IMAGE="$registry_repo" \
    "$transient_main" \
    python -c 'import os; from pathlib import Path; from backend.app.infrastructure.transient_updater import TransientUpdaterLauncher; from backend.app.infrastructure.updater_protocol import UpgradeHelperRequest; launcher=TransientUpdaterLauncher(config_dir=Path("/config"), target_container=os.environ["PB_TARGET_CONTAINER"], allowed_image=os.environ["PB_ALLOWED_IMAGE"]); status=launcher.start_upgrade(UpgradeHelperRequest(request_id=os.environ["PB_REQUEST_ID"], current_version=os.environ["PB_CURRENT_VERSION"], target_version=os.environ["PB_TARGET_VERSION"], target_image=os.environ["PB_TARGET_IMAGE"], backup_database_file=os.environ["PB_BACKUP_DB"], backup_manifest_file=os.environ["PB_BACKUP_MANIFEST"], grace_seconds=2.0)); assert status.phase == "accepted", status' >/dev/null
}

mkdir -p "$fault_build_dir"

if [[ "$baseline_mode" == --synthetic-arm64-baseline ]]; then
  test "$(uname -m)" = aarch64
  test "$(docker image inspect "$candidate_image" --format '{{.Os}}/{{.Architecture}}')" = linux/arm64
  mkdir -p "$fault_build_dir/synthetic-baseline"
  cat >"$fault_build_dir/synthetic-baseline/Dockerfile" <<'DOCKERFILE'
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
LABEL org.packbreaker.ci.synthetic-arm64-baseline="true"
DOCKERFILE
  baseline_image="packbreaker:updater-e2e-arm64-baseline"
  echo "Build isolated synthetic ARM64 baseline (NOT a published release)"
  docker build --platform linux/arm64 --build-arg BASE_IMAGE="$candidate_image" \
    --tag "$baseline_image" "$fault_build_dir/synthetic-baseline" >/dev/null
  test "$(docker image inspect "$baseline_image" --format '{{.Os}}/{{.Architecture}}')" = linux/arm64
else
  echo "Pull formal baseline by immutable digest: $baseline_image"
  docker pull "$baseline_image" >/dev/null
fi
baseline_image_id="$(docker image inspect "$baseline_image" --format '{{.Id}}')"
candidate_image_id="$(docker image inspect "$candidate_image" --format '{{.Id}}')"
test "$baseline_image_id" != "$candidate_image_id"
test "$(docker run --rm "$candidate_image" python -c 'from backend.app.versioning import app_version; print(app_version())')" = "$candidate_version"

echo "Start isolated local registry"
docker run --detach \
  --name "$registry_container" \
  --publish "127.0.0.1:${registry_port}:5000" \
  registry:2 >/dev/null
wait_registry

echo "Mirror exact baseline bits and candidate into the isolated registry"
docker tag "$baseline_image" "$local_baseline_tag"
docker push "$local_baseline_tag" >/dev/null
test "$(docker image inspect "$local_baseline_tag" --format '{{.Id}}')" = "$baseline_image_id"
docker tag "$candidate_image" "$local_candidate_tag"
docker push "$local_candidate_tag" >/dev/null
candidate_target="$(repo_digest_for_tag "$local_candidate_tag")"
test -n "$candidate_target"

cat >"$fault_build_dir/fault_entrypoint.py" <<'PY'
import os
import sqlite3

path = os.path.join(os.environ.get("PACKBREAKER_CONFIG_DIR", "/config"), "packbreaker.db")
with sqlite3.connect(path, timeout=30) as connection:
    connection.execute(
        "UPDATE release_upgrade_probe SET probe_value=? WHERE probe_key='upgrade'",
        ("candidate-mutated",),
    )
os.execvp("python", ["python", "-m", "backend.app.container_entrypoint"])
PY
cat >"$fault_build_dir/Dockerfile" <<'DOCKERFILE'
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
COPY fault_entrypoint.py /app/fault_entrypoint.py
HEALTHCHECK --interval=1s --timeout=1s --start-period=1s --retries=1 CMD ["python", "-c", "raise SystemExit(1)"]
CMD ["python", "/app/fault_entrypoint.py"]
DOCKERFILE
docker build --build-arg BASE_IMAGE="$candidate_image" --tag packbreaker:updater-e2e-fault "$fault_build_dir" >/dev/null
docker tag packbreaker:updater-e2e-fault "$local_fault_tag"
docker push "$local_fault_tag" >/dev/null
fault_target="$(repo_digest_for_tag "$local_fault_tag")"
test -n "$fault_target"

echo "Run real helper success path: $baseline_version -> $candidate_version"
prepare_case "$success_main" "$success_config" "$success_data" 18081 "success-original"
success_backup_db="$case_backup_db"
success_backup_manifest="$case_backup_manifest"
start_helper "$success_helper" "$success_main" "$success_config"
trigger_upgrade "$success_helper" "$success_request" "$candidate_target" "$success_backup_db" "$success_backup_manifest"
success_status="$(wait_helper_terminal "$success_helper" succeeded)"
python3 -c 'import json,sys; s=json.load(sys.stdin); assert s["rollback_performed"] is False, s' <<<"$success_status"
wait_app_ready "$success_main"
test "$(docker inspect "$success_main" --format '{{.Image}}')" = "$candidate_image_id"
test "$(docker exec --user "$runtime_user" "$success_main" python -c 'from backend.app.versioning import app_version; print(app_version())')" = "$candidate_version"
assert_probe "$success_main" "success-original"
assert_quiesced_backup_exists "$success_config"
curl --fail --silent http://127.0.0.1:18081/api/v1/health/ready >/dev/null
test -z "$(docker inspect "$success_main" --format '{{range .Mounts}}{{if eq .Destination "/var/run/docker.sock"}}{{.Source}}{{end}}{{end}}')"
if docker ps -a --format '{{.Names}}' | grep --fixed-strings --quiet "${success_main}-rollback-"; then
  echo "成功升级后仍残留 rollback 容器" >&2
  exit 1
fi

echo "Run real helper rollback path with a database-mutating unhealthy candidate"
prepare_case "$rollback_main" "$rollback_config" "$rollback_data" 18082 "rollback-original"
rollback_backup_db="$case_backup_db"
rollback_backup_manifest="$case_backup_manifest"
start_helper "$rollback_helper" "$rollback_main" "$rollback_config"
trigger_upgrade "$rollback_helper" "$rollback_request" "$fault_target" "$rollback_backup_db" "$rollback_backup_manifest"
rollback_status="$(wait_helper_terminal "$rollback_helper" rolled_back)"
python3 -c 'import json,sys; s=json.load(sys.stdin); assert s["rollback_performed"] is True, s' <<<"$rollback_status"
wait_app_ready "$rollback_main"
test "$(docker inspect "$rollback_main" --format '{{.Image}}')" = "$baseline_image_id"
test "$(docker exec --user "$runtime_user" "$rollback_main" python -c 'from backend.app.versioning import app_version; print(app_version())')" = "$baseline_version"
assert_probe "$rollback_main" "rollback-original"
assert_quiesced_backup_exists "$rollback_config"
curl --fail --silent http://127.0.0.1:18082/api/v1/health/ready >/dev/null

docker exec --user "$runtime_user" "$rollback_main" \
  python -c 'import glob,sqlite3; files=glob.glob("/config/backups/pre-restore/packbreaker-*.db"); assert len(files) == 1, files; c=sqlite3.connect(files[0], timeout=30); row=c.execute("SELECT probe_value FROM release_upgrade_probe WHERE probe_key=\"upgrade\"").fetchone(); assert row == ("candidate-mutated",), row; c.close()'

echo "Run real single-container transient helper replacement path"
# 正式 v0.1.3 尚未包含 transient launcher，因此首个支持该能力的候选版本无法从
# v0.1.3 主容器内部发起完整相邻版本流程。这里先用候选镜像验证真实 Docker 替换机制：
# 一次性 helper 创建/接管、主容器重建、docker.sock 保留、状态持久化与自动清理。
# 待 transient-capable 版本成为正式 baseline 后，再由后续版本覆盖完整相邻版本 API 路径。
prepare_transient_case
trigger_transient_upgrade
wait_app_ready_after_switch "$transient_main" 0:0 "$transient_initial_container_id"
wait_docker_healthy "$transient_main"
transient_new_container_id="$(docker inspect "$transient_main" --format '{{.Id}}')"
test "$transient_new_container_id" != "$transient_initial_container_id"
test "$(docker inspect "$transient_main" --format '{{.Image}}')" = "$candidate_image_id"
docker exec --user 0:0 "$transient_main" \
  python -c 'import sqlite3; c=sqlite3.connect("/config/packbreaker.db", timeout=30); row=c.execute("SELECT probe_value FROM release_upgrade_probe WHERE probe_key=\"upgrade\"").fetchone(); assert row == ("transient-original",), row; c.close()'
wait_transient_terminal
test -n "$(docker inspect "$transient_main" --format '{{range .Mounts}}{{if eq .Destination "/var/run/docker.sock"}}{{.Source}}{{end}}{{end}}')"
test "$(docker inspect "$transient_main" --format '{{index .Config.Labels "com.docker.compose.project"}}')" = "packbreaker-e2e"
test "$(docker inspect "$transient_main" --format '{{index .Config.Labels "com.docker.compose.service"}}')" = "packbreaker"
test "$(docker inspect "$transient_main" --format '{{index .Config.Labels "com.docker.compose.container-number"}}')" = "1"
assert_quiesced_backup_exists "$transient_config"
wait_transient_helper_cleanup

echo "updater E2E passed: baseline_mode=$baseline_mode $baseline_version -> candidate $candidate_version, unhealthy-candidate rollback, and single-container transient helper replacement"
