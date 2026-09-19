"""Fail closed on a generated Release's local assets before publishing them.

This is a local consistency gate, not proof of a registry's published digest,
SBOM content provenance, or the success of a real platform upgrade.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
from pathlib import Path
from typing import Any

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
_CHECKSUM_LINE = re.compile(r"^([0-9a-f]{64})  ([A-Za-z0-9._-]+)$")
_MULTIARCH = ["linux/amd64", "linux/arm64"]


def _read_regular_file(path: Path) -> bytes:
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError(f"发布资产必须是普通文件，不能是符号链接：{path.name}")
    return path.read_bytes()


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(_read_regular_file(path))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"发布资产无法读取为 JSON object：{path.name}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"发布资产必须是 JSON object：{path.name}")
    return payload


def verify_release_evidence(
    release_dir: Path, *, tag: str, image: str, image_digest: str, commit: str
) -> None:
    """Cross-check the three generated assets against independent workflow inputs."""
    match = _TAG.fullmatch(tag)
    if match is None:
        raise ValueError("发布 tag 必须为 vX.Y.Z")
    if not _DIGEST.fullmatch(image_digest) or not _COMMIT.fullmatch(commit):
        raise ValueError("发布镜像 digest 或提交 SHA 无效")
    if not re.fullmatch(r"ghcr\.io/[a-z0-9._/-]+", image) or ".." in image:
        raise ValueError("发布镜像名称必须为规范 GHCR 路径")
    version = tag[1:]
    manifest_name = f"packbreaker-{version}.release.json"
    sbom_name = f"packbreaker-{version}.spdx.json"
    expected_assets = {manifest_name, sbom_name, "SHA256SUMS"}
    if not release_dir.is_dir() or release_dir.is_symlink():
        raise ValueError("发布资产目录必须是普通目录")
    if {entry.name for entry in release_dir.iterdir()} != expected_assets:
        raise ValueError("发布资产文件集合不匹配：必须只有发布清单、SPDX SBOM、SHA256SUMS")

    manifest_file = release_dir / manifest_name
    sbom_file = release_dir / sbom_name
    manifest = _read_json_object(manifest_file)
    sbom = _read_json_object(sbom_file)
    expected_multiarch = tuple(int(part) for part in match.groups()) >= (1, 0, 0)
    fields = {
        "format_version",
        "version",
        "tag",
        "commit",
        "image",
        "image_digest",
        "immutable_image",
        "sbom_file",
        "sbom_sha256",
        "generated_at",
        "platforms" if expected_multiarch else "platform",
    }
    if set(manifest) != fields:
        raise ValueError("发布清单字段集合不匹配")
    expected_values: dict[str, object] = {
        "format_version": 2 if expected_multiarch else 1,
        "version": version,
        "tag": tag,
        "commit": commit,
        "image": image,
        "image_digest": image_digest,
        "immutable_image": f"{image}@{image_digest}",
        "sbom_file": sbom_name,
        "platforms" if expected_multiarch else "platform": (
            _MULTIARCH if expected_multiarch else "linux/amd64"
        ),
    }
    for key, value in expected_values.items():
        if manifest[key] != value or isinstance(manifest[key], bool):
            raise ValueError(f"发布清单 {key} 与发布输入不一致")
    if not isinstance(manifest["generated_at"], str) or not re.fullmatch(
        r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", manifest["generated_at"]
    ):
        raise ValueError("发布清单 generated_at 必须为 UTC 时间戳")
    if sbom.get("spdxVersion") != "SPDX-2.3":
        raise ValueError("发布 SBOM 必须是 SPDX 2.3 JSON")

    file_hashes = {
        name: hashlib.sha256(_read_regular_file(release_dir / name)).hexdigest()
        for name in (manifest_name, sbom_name)
    }
    if manifest["sbom_sha256"] != file_hashes[sbom_name]:
        raise ValueError("发布清单与 SBOM 内容哈希不匹配")
    try:
        checksum_lines = _read_regular_file(release_dir / "SHA256SUMS").decode("ascii").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError("SHA256SUMS 内容不可读取") from exc
    parsed: dict[str, str] = {}
    for line in checksum_lines:
        item = _CHECKSUM_LINE.fullmatch(line)
        if item is None:
            raise ValueError("SHA256SUMS 行格式无效")
        digest, filename = item.groups()
        if filename in parsed:
            raise ValueError("SHA256SUMS 不允许重复文件名")
        parsed[filename] = digest
    if parsed != file_hashes:
        raise ValueError("SHA256SUMS 与发布资产内容不一致")


def main() -> int:
    parser = argparse.ArgumentParser(description="发布前核对 SBOM、清单、校验和与不可变镜像身份")
    parser.add_argument("--release-dir", type=Path, required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--image-digest", required=True)
    parser.add_argument("--commit", required=True)
    args = parser.parse_args()
    try:
        verify_release_evidence(
            args.release_dir,
            tag=args.tag,
            image=args.image,
            image_digest=args.image_digest,
            commit=args.commit,
        )
    except (OSError, ValueError) as exc:
        parser.exit(2, f"发布资产一致性门禁未通过：{exc}\n")
    print("Release 发布资产一致性门禁通过（本地文件；不代表 GHCR 双架构运行已验收）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
