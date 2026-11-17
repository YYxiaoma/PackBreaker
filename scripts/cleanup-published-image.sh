#!/usr/bin/env bash
# 发布失败时删除本次刚推送的镜像 tag。
#
# 发布 job 采用「不可覆盖」策略：同名版本镜像一旦存在，下次重跑会在
# "Refuse an existing immutable version image" 直接失败。而推送之后的
# ARM64 原生验证等门禁如果失败，镜像就会留在 registry 里把后续重跑全部挡住。
# 这个脚本只删「本次失败运行刚推上去的那个版本」，不碰任何历史发布。
#
# 凭据缺失时只告警不失败：清理是尽力而为，不能掩盖真正的发布失败原因。
set -euo pipefail

tag="${1:?usage: cleanup-published-image.sh <vX.Y.Z>}"
if [[ ! "$tag" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "Invalid release tag for cleanup: $tag" >&2
  exit 2
fi
version="${tag#v}"

repository="${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
package="$(tr '[:upper:]' '[:lower:]' <<<"${repository##*/}")"
ghcr_image="ghcr.io/${repository,,}"
dockerhub_namespace="${DOCKERHUB_NAMESPACE:-yyxiaoma01}"
dockerhub_repository="${DOCKERHUB_REPOSITORY:-packbreaker}"

cleaned=0

if [[ -n "${GH_TOKEN:-}" ]]; then
  echo "Deleting $ghcr_image:$version"
  versions_json="$(mktemp)"
  curl --silent --show-error --fail --max-time 30 \
    -H "Authorization: Bearer $GH_TOKEN" \
    -H "Accept: application/vnd.github+json" \
    "https://api.github.com/user/packages/container/${package}/versions?per_page=100" \
    >"$versions_json"
  mapfile -t version_ids < <(python3 - "$version" <"$versions_json" <<'PY'
import json
import sys

target = sys.argv[1]
try:
    payload = json.load(sys.stdin)
except json.JSONDecodeError:
    raise SystemExit(0)
if not isinstance(payload, list):
    raise SystemExit(0)
for item in payload:
    tags = (item.get("metadata") or {}).get("container", {}).get("tags") or []
    if target in tags:
        print(item["id"])
PY
)
  rm -f "$versions_json"
  for version_id in "${version_ids[@]:-}"; do
    [[ -n "$version_id" ]] || continue
    code="$(curl --silent --output /dev/null --write-out '%{http_code}' --max-time 30 \
      -X DELETE -H "Authorization: Bearer $GH_TOKEN" -H "Accept: application/vnd.github+json" \
      "https://api.github.com/user/packages/container/${package}/versions/${version_id}")"
    echo "  ghcr version ${version_id} -> HTTP ${code}"
    [[ "$code" == "204" ]] && cleaned=1
  done
else
  echo "::warning::未提供 GH_TOKEN，跳过 ghcr 清理（需人工删除 $ghcr_image:$version）"
fi

if [[ -n "${DOCKERHUB_USERNAME:-}" && -n "${DH_TOKEN:-}" ]]; then
  echo "Deleting docker.io/${dockerhub_namespace}/${dockerhub_repository}:$version"
  # Docker Hub 的删除接口不接受 PAT 的 Basic/Bearer 鉴权（会返回 401），
  # 必须先用 /v2/users/login/ 换成 JWT 再调用。
  jwt="$(curl --silent --show-error --fail --max-time 30 \
    -H 'Content-Type: application/json' \
    -d "$(printf '{"username":"%s","password":"%s"}' "$DOCKERHUB_USERNAME" "$DH_TOKEN")" \
    https://hub.docker.com/v2/users/login/ \
    | python3 -c 'import json,sys; print(json.load(sys.stdin).get("token",""))')"
  if [[ -z "$jwt" ]]; then
    echo "::warning::Docker Hub 登录失败，跳过镜像清理"
  else
    code="$(curl --silent --output /dev/null --write-out '%{http_code}' --max-time 30 \
      -X DELETE -H "Authorization: Bearer $jwt" \
      "https://hub.docker.com/v2/repositories/${dockerhub_namespace}/${dockerhub_repository}/tags/${version}/")"
    echo "  dockerhub tag -> HTTP ${code}"
    [[ "$code" == "204" ]] && cleaned=1
  fi
else
  echo "::warning::未提供 Docker Hub 凭据，跳过镜像清理"
fi

if [[ "$cleaned" -eq 0 ]]; then
  echo "::warning::本次未删除任何镜像，重跑前请人工确认 $version 是否仍存在于 registry"
fi
echo "Cleanup finished for $tag"
