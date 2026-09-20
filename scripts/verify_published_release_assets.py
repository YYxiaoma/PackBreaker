"""Read back published GitHub Release assets before advancing mutable image tags.

The gh commands only read release metadata and download assets into a fresh
temporary directory. No Release or registry mutation is performed here.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from scripts.verify_release_evidence import verify_release_evidence

_TAG = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+\Z")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
_Runner = Callable[..., subprocess.CompletedProcess[str]]


def verify_published_release_assets(
    *,
    tag: str,
    repository: str,
    image: str,
    image_digest: str,
    commit: str,
    run: _Runner = subprocess.run,
) -> None:
    if _TAG.fullmatch(tag) is None or _REPOSITORY.fullmatch(repository) is None:
        raise ValueError("发布 tag 或 GitHub 仓库格式无效")
    version = tag.removeprefix("v")
    expected = {
        f"packbreaker-{version}.release.json",
        f"packbreaker-{version}.spdx.json",
        "SHA256SUMS",
    }
    details = run(
        [
            "gh",
            "release",
            "view",
            tag,
            "--repo",
            repository,
            "--json",
            "tagName,isDraft,isPrerelease,assets",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    try:
        metadata: Any = json.loads(details.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("GitHub Release 元数据不是有效 JSON") from exc
    if not isinstance(metadata, dict) or metadata.get("tagName") != tag:
        raise ValueError("GitHub Release tag 与发布身份不匹配")
    if metadata.get("isDraft") is not False or metadata.get("isPrerelease") is not False:
        raise ValueError("GitHub Release 尚未正式发布")
    assets = metadata.get("assets")
    if not isinstance(assets, list) or len(assets) != len(expected):
        raise ValueError("GitHub Release 资产数量不匹配")
    sizes: dict[str, int] = {}
    for item in assets:
        if not isinstance(item, dict):
            raise ValueError("GitHub Release 资产元数据无效")
        name, size = item.get("name"), item.get("size")
        if name not in expected or name in sizes or type(size) is not int or size <= 0:
            raise ValueError("GitHub Release 资产名称、大小或唯一性不匹配")
        sizes[name] = size
    if set(sizes) != expected:
        raise ValueError("GitHub Release 资产集合不匹配")

    with tempfile.TemporaryDirectory(prefix="packbreaker-release-readback-") as directory:
        run(
            ["gh", "release", "download", tag, "--repo", repository, "--dir", directory],
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
        release_dir = Path(directory)
        verify_release_evidence(
            release_dir,
            tag=tag,
            image=image,
            image_digest=image_digest,
            commit=commit,
        )
        if any((release_dir / name).stat().st_size != size for name, size in sizes.items()):
            raise ValueError("GitHub Release 下载资产与 API 元数据大小不一致")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="发布后只读复核 GitHub Release 资产与不可变镜像身份"
    )
    parser.add_argument("--tag", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--image-digest", required=True)
    parser.add_argument("--commit", required=True)
    args = parser.parse_args()
    try:
        verify_published_release_assets(
            tag=args.tag,
            repository=args.repository,
            image=args.image,
            image_digest=args.image_digest,
            commit=args.commit,
        )
    except (OSError, ValueError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        parser.exit(2, f"GitHub Release 已上传资产复核失败，不推进 stable/latest：{exc}\n")
    print("已上传 GitHub Release 资产只读复核通过：不可变镜像身份、SBOM 和校验和一致")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
