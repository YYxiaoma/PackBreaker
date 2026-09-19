from __future__ import annotations

import argparse
import json
import re
import subprocess
from collections.abc import Callable, Mapping
from typing import Any

SUPPORTED_PLATFORMS = frozenset({"linux/amd64", "linux/arm64"})
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_IMAGE_MANIFEST_TYPES = frozenset(
    {
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    }
)
_IMAGE_CONFIG_TYPES = frozenset(
    {"application/vnd.oci.image.config.v1+json", "application/vnd.docker.container.image.v1+json"}
)
_LAYER_TYPES = frozenset(
    {
        "application/vnd.oci.image.layer.v1.tar",
        "application/vnd.oci.image.layer.v1.tar+gzip",
        "application/vnd.oci.image.layer.v1.tar+zstd",
        "application/vnd.docker.image.rootfs.diff.tar.gzip",
        "application/vnd.docker.image.rootfs.foreign.diff.tar.gzip",
    }
)


def _descriptor(value: object, *, media_types: frozenset[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} descriptor 缺失")
    media_type = value.get("mediaType")
    if not isinstance(media_type, str) or media_type not in media_types:
        raise ValueError(f"{label} mediaType 不受支持")
    digest = value.get("digest")
    if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
        raise ValueError(f"{label} digest 无效")
    size = value.get("size")
    if type(size) is not int or size <= 0:
        raise ValueError(f"{label} size 无效")
    return value


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


def verify_release_children(
    payload: object, *, multiarch: bool, read_manifest: Callable[[str], object]
) -> None:
    """Resolve each runnable child by immutable digest and validate its actual manifest.

    OCI platform descriptors are claims supplied by the index. This gate checks
    that their child manifests really resolve to executable image manifests;
    checking each config's architecture and runtime labels remains a separate
    native-platform release acceptance requirement.
    """
    verify_release_index(payload, multiarch=multiarch)
    assert isinstance(payload, dict)
    descriptors = payload["manifests"]
    seen_digests: set[str] = set()
    for item in descriptors:
        platform = item["platform"]
        if (platform["os"], platform["architecture"]) == ("unknown", "unknown"):
            continue  # Buildx provenance/attestation descriptor is not a runnable image.
        arch = f"{platform['os']}/{platform['architecture']}"
        descriptor = _descriptor(item, media_types=_IMAGE_MANIFEST_TYPES, label=arch)
        digest = descriptor["digest"]
        if digest in seen_digests:
            raise ValueError("不同平台不得共用同一个子镜像 digest")
        seen_digests.add(digest)
        child = read_manifest(digest)
        if not isinstance(child, Mapping) or child.get("schemaVersion") != 2:
            raise ValueError(f"{arch} 子镜像不是有效的 schemaVersion 2 manifest")
        child_media_type = child.get("mediaType")
        if not isinstance(child_media_type, str) or child_media_type not in _IMAGE_MANIFEST_TYPES:
            raise ValueError(f"{arch} 子镜像 mediaType 不受支持")
        _descriptor(child.get("config"), media_types=_IMAGE_CONFIG_TYPES, label=f"{arch} config")
        layers = child.get("layers")
        if not isinstance(layers, list) or not layers:
            raise ValueError(f"{arch} 子镜像缺少运行层")
        for index, layer in enumerate(layers):
            _descriptor(layer, media_types=_LAYER_TYPES, label=f"{arch} layer[{index}]")


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
    repository = args.image.split("@", 1)[0]

    def read_child_manifest(digest: str) -> object:
        child_process = subprocess.run(
            ["docker", "buildx", "imagetools", "inspect", "--raw", f"{repository}@{digest}"],
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        return json.loads(child_process.stdout)

    verify_release_children(raw, multiarch=args.multiarch, read_manifest=read_child_manifest)
    print("Release 平台索引及每个平台不可变子镜像 manifest 校验通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
