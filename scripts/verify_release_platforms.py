from __future__ import annotations

import argparse
import json
import subprocess
from collections.abc import Mapping
from typing import Any

SUPPORTED_PLATFORMS = frozenset({"linux/amd64", "linux/arm64"})


def verify_release_index(payload: object, *, multiarch: bool) -> None:
    """Verify the registry's published index, not the declared release metadata."""
    if not isinstance(payload, dict) or not isinstance(payload.get("manifests"), list):
        raise ValueError("正式镜像必须提供可检查的 OCI/Docker manifest index")
    manifest_list = payload["manifests"]
    if not manifest_list:
        raise ValueError("正式镜像 manifest index 为空")
    actual: list[str] = []
    for descriptor in manifest_list:
        if not isinstance(descriptor, Mapping):
            raise ValueError("manifest descriptor 无效")
        platform = descriptor.get("platform")
        if not isinstance(platform, Mapping):
            raise ValueError("manifest platform 缺失")
        os_name, architecture = platform.get("os"), platform.get("architecture")
        # Buildx provenance attestation descriptors use unknown/unknown; they are not runnable.
        if (os_name, architecture) == ("unknown", "unknown"):
            continue
        if not isinstance(os_name, str) or not isinstance(architecture, str):
            raise ValueError("manifest platform 无效")
        variant = platform.get("variant")
        if variant not in (None, "") and not (
            (os_name, architecture) == ("linux", "arm64") and variant == "v8"
        ):
            raise ValueError("正式镜像不接受未验收的平台变体")
        actual.append(f"{os_name}/{architecture}")
    expected = SUPPORTED_PLATFORMS if multiarch else frozenset({"linux/amd64"})
    if set(actual) != expected or len(actual) != len(expected):
        raise ValueError(f"正式镜像平台不匹配：expected={sorted(expected)}, actual={actual}")


def main() -> int:
    parser = argparse.ArgumentParser(description="验证 GHCR 发布 digest 的真实平台列表")
    parser.add_argument("--image", required=True, help="不可变 image@sha256:digest")
    parser.add_argument("--multiarch", action="store_true")
    args = parser.parse_args()
    if "@sha256:" not in args.image:
        parser.error("必须按完整不可变 digest 检查正式镜像")
    process = subprocess.run(
        ["docker", "buildx", "imagetools", "inspect", "--raw", args.image],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    raw: Any = json.loads(process.stdout)
    verify_release_index(raw, multiarch=args.multiarch)
    print("Release 平台清单校验通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
