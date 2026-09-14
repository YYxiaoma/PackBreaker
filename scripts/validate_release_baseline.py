from __future__ import annotations

import argparse
import json
import re
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASELINE = ROOT / "release-baseline.json"
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
_ALEMBIC_REVISION = re.compile(r"^\d{4}_[a-z0-9_]+$")


@dataclass(frozen=True, slots=True)
class ReleaseBaseline:
    format_version: int
    version: str
    tag: str
    commit: str
    platform: str
    image: str
    image_digest: str
    immutable_image: str
    alembic_revision: str
    release_workflow_run_id: int

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _semver_tuple(value: str, *, label: str) -> tuple[int, int, int]:
    match = _SEMVER.fullmatch(value)
    if match is None:
        raise ValueError(f"{label} 必须是三段数字 SemVer")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def project_version(pyproject: Path = ROOT / "pyproject.toml") -> str:
    payload = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    project = payload.get("project")
    if not isinstance(project, dict):
        raise ValueError("pyproject.toml 缺少 [project]")
    version = project.get("version")
    if not isinstance(version, str) or not version:
        raise ValueError("pyproject.toml project.version 无效")
    _semver_tuple(version, label="project.version")
    return version


def load_release_baseline(
    baseline_path: Path = DEFAULT_BASELINE,
    *,
    pyproject: Path = ROOT / "pyproject.toml",
) -> ReleaseBaseline:
    try:
        payload: Any = json.loads(baseline_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取 release baseline：{exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("release baseline 必须是 JSON object")

    required = {
        "format_version",
        "version",
        "tag",
        "commit",
        "platform",
        "image",
        "image_digest",
        "immutable_image",
        "alembic_revision",
        "release_workflow_run_id",
    }
    if set(payload) != required:
        raise ValueError("release baseline 字段集合与 format_version=1 契约不一致")
    if payload.get("format_version") != 1:
        raise ValueError("release baseline format_version 必须为 1")

    string_fields = required - {"format_version", "release_workflow_run_id"}
    for field in string_fields:
        if not isinstance(payload.get(field), str) or not payload[field]:
            raise ValueError(f"release baseline {field} 必须是非空字符串")
    run_id = payload.get("release_workflow_run_id")
    if not isinstance(run_id, int) or isinstance(run_id, bool) or run_id <= 0:
        raise ValueError("release_workflow_run_id 必须是正整数")

    version = payload["version"]
    assert isinstance(version, str)
    baseline_semver = _semver_tuple(version, label="baseline version")
    current_semver = _semver_tuple(project_version(pyproject), label="project.version")
    if baseline_semver > current_semver:
        raise ValueError("release baseline 不能新于当前 project.version")
    if payload["tag"] != f"v{version}":
        raise ValueError("release baseline tag 与 version 不一致")
    if not _COMMIT.fullmatch(payload["commit"]):
        raise ValueError("release baseline commit 必须是 40 位小写十六进制 SHA")
    if payload["platform"] != "linux/amd64":
        raise ValueError("release baseline platform 必须为 linux/amd64")

    image = payload["image"]
    digest = payload["image_digest"]
    assert isinstance(image, str)
    assert isinstance(digest, str)
    if image != image.lower() or not image.startswith("ghcr.io/") or "@" in image:
        raise ValueError("release baseline image 必须是小写 GHCR 名称且不含 digest")
    if not _DIGEST.fullmatch(digest):
        raise ValueError("release baseline image_digest 必须是完整 sha256 digest")
    if payload["immutable_image"] != f"{image}@{digest}":
        raise ValueError("release baseline immutable_image 与 image/image_digest 不一致")
    if not _ALEMBIC_REVISION.fullmatch(payload["alembic_revision"]):
        raise ValueError("release baseline alembic_revision 格式无效")

    return ReleaseBaseline(**payload)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="校验上一正式 PackBreaker release 的不可变升级基线"
    )
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--json", action="store_true", help="只输出已校验的 baseline JSON")
    args = parser.parse_args()
    try:
        baseline = load_release_baseline(args.baseline.resolve())
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 2
    payload = baseline.as_dict()
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        print(
            json.dumps(
                {"status": "ok", "baseline": payload},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
