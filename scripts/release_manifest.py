from __future__ import annotations

import argparse
import hashlib
import json
import re
import tomllib
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")


def project_version(pyproject: Path = ROOT / "pyproject.toml") -> str:
    payload = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    project = payload.get("project")
    if not isinstance(project, dict):
        raise ValueError("pyproject.toml 缺少 [project]")
    version = project.get("version")
    if not isinstance(version, str) or not version:
        raise ValueError("pyproject.toml project.version 无效")
    return version


def validate_release_tag(tag: str) -> str:
    version = project_version()
    if tag != f"v{version}":
        raise ValueError(f"发布 tag {tag!r} 与项目版本 v{version} 不一致")
    return version


def build_release_manifest(
    *,
    tag: str,
    image: str,
    image_digest: str,
    commit: str,
    sbom_path: Path,
    generated_at: datetime | None = None,
) -> dict[str, object]:
    version = validate_release_tag(tag)
    if not _DIGEST.fullmatch(image_digest):
        raise ValueError("镜像 digest 必须是完整 sha256 digest")
    if not _COMMIT.fullmatch(commit):
        raise ValueError("发布 commit 必须是 40 位小写十六进制 SHA")
    if image != image.lower() or image.startswith("/") or "@" in image:
        raise ValueError("镜像名称必须为不含 digest 的小写规范引用")
    if not sbom_path.is_file():
        raise ValueError("SBOM 文件不存在")

    timestamp = (generated_at or datetime.now(UTC)).astimezone(UTC)
    return {
        "format_version": 1,
        "version": version,
        "tag": tag,
        "commit": commit,
        "platform": "linux/amd64",
        "image": image,
        "image_digest": image_digest,
        "immutable_image": f"{image}@{image_digest}",
        "sbom_file": sbom_path.name,
        "sbom_sha256": _sha256(sbom_path),
        "generated_at": timestamp.isoformat().replace("+00:00", "Z"),
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_timestamp(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("generated-at 必须包含时区")
    return parsed.astimezone(UTC)


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 PackBreaker 发布产物清单")
    parser.add_argument("--tag", required=True)
    parser.add_argument("--validate-tag-only", action="store_true")
    parser.add_argument("--image")
    parser.add_argument("--image-digest")
    parser.add_argument("--commit")
    parser.add_argument("--sbom", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--generated-at")
    args = parser.parse_args()

    try:
        if args.validate_tag_only:
            version = validate_release_tag(args.tag)
            print(json.dumps({"status": "ok", "version": version}, ensure_ascii=False))
            return 0
        if None in (args.image, args.image_digest, args.commit, args.sbom, args.output):
            raise ValueError("生成发布清单必须提供 image、image-digest、commit、sbom 和 output")
        assert isinstance(args.image, str)
        assert isinstance(args.image_digest, str)
        assert isinstance(args.commit, str)
        assert isinstance(args.sbom, Path)
        assert isinstance(args.output, Path)
        payload = build_release_manifest(
            tag=args.tag,
            image=args.image,
            image_digest=args.image_digest,
            commit=args.commit,
            sbom_path=args.sbom,
            generated_at=_parse_timestamp(args.generated_at),
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps({"status": "ok", **payload}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
