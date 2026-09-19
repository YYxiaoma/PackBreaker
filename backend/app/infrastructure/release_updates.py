from __future__ import annotations

import platform as host_platform
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx2

OFFICIAL_RELEASES_URL = "https://github.com/YYxiaoma/PackBreaker/releases/latest"
OFFICIAL_IMAGE = "ghcr.io/yyxiaoma/packbreaker"
_RELEASE_TAG = re.compile(r"^v(?P<version>\d+\.\d+\.\d+)$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_DUAL_PLATFORMS = ("linux/amd64", "linux/arm64")


class ReleaseUpdateError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class ReleaseTarget:
    version: str
    tag: str
    commit: str
    image: str
    image_digest: str
    immutable_image: str
    platform: str


def semantic_version(value: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)", value)
    if match is None:
        raise ReleaseUpdateError("RELEASE_VERSION_INVALID", "发布版本必须使用 x.y.z 格式")
    return tuple(int(match.group(part)) for part in ("major", "minor", "patch"))  # type: ignore[return-value]


def parse_release_manifest(payload: object, *, expected_tag: str) -> ReleaseTarget:
    if not isinstance(payload, dict):
        raise ReleaseUpdateError("RELEASE_MANIFEST_INVALID", "release manifest 必须是 JSON object")
    manifest = payload
    tag = _required_string(manifest, "tag")
    version = _required_string(manifest, "version")
    tag_match = _RELEASE_TAG.fullmatch(tag)
    if tag_match is None or tag_match.group("version") != version or tag != expected_tag:
        raise ReleaseUpdateError(
            "RELEASE_IDENTITY_MISMATCH", "release manifest 的 tag/version 身份不一致"
        )
    semantic_version(version)

    image = _required_string(manifest, "image").lower()
    if image != OFFICIAL_IMAGE:
        raise ReleaseUpdateError("RELEASE_IMAGE_UNTRUSTED", "release manifest 指向了非官方镜像仓库")
    digest = _required_string(manifest, "image_digest").lower()
    if _DIGEST.fullmatch(digest) is None:
        raise ReleaseUpdateError("RELEASE_DIGEST_INVALID", "release manifest 镜像 digest 无效")
    immutable = _required_string(manifest, "immutable_image").lower()
    if immutable != f"{OFFICIAL_IMAGE}@{digest}":
        raise ReleaseUpdateError(
            "RELEASE_IMAGE_IDENTITY_MISMATCH", "不可变镜像引用与 image/digest 不一致"
        )
    if manifest.get("format_version") == 1:
        if semantic_version(version) >= (1, 0, 0):
            raise ReleaseUpdateError(
                "RELEASE_MANIFEST_INVALID", "v1.0.0 起正式发布必须使用双架构清单"
            )
        platform = _required_string(manifest, "platform")
        if platform != "linux/amd64":
            raise ReleaseUpdateError(
                "RELEASE_PLATFORM_UNSUPPORTED", "旧版单架构发布仅接受 linux/amd64 镜像"
            )
    elif manifest.get("format_version") == 2:
        if semantic_version(version) < (1, 0, 0):
            raise ReleaseUpdateError(
                "RELEASE_MANIFEST_INVALID", "双架构发布格式要求 v1.0.0 或更新版本"
            )
        platforms = manifest.get("platforms")
        if platforms != list(_DUAL_PLATFORMS) or "platform" in manifest:
            raise ReleaseUpdateError(
                "RELEASE_PLATFORM_UNSUPPORTED", "正式双架构发布必须声明 linux/amd64 和 linux/arm64"
            )
        platform = runtime_platform()
        if platform not in _DUAL_PLATFORMS:
            raise ReleaseUpdateError(
                "RELEASE_PLATFORM_UNSUPPORTED", "当前 Linux CPU 架构不在正式发布支持范围"
            )
    else:
        raise ReleaseUpdateError("RELEASE_MANIFEST_INVALID", "release manifest 格式版本无效")
    commit = _required_string(manifest, "commit").lower()
    if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise ReleaseUpdateError("RELEASE_COMMIT_INVALID", "release manifest commit 无效")
    return ReleaseTarget(
        version=version,
        tag=tag,
        commit=commit,
        image=image,
        image_digest=digest,
        immutable_image=immutable,
        platform=platform,
    )


def runtime_platform() -> str:
    """从实际容器内的 Linux 架构选择 OCI manifest 的平台，不允许跨架构替换。"""
    if host_platform.system() != "Linux":
        return "unsupported"
    machine = host_platform.machine().lower()
    if machine in {"x86_64", "amd64"}:
        return "linux/amd64"
    if machine in {"aarch64", "arm64"}:
        return "linux/arm64"
    return "unsupported"


class ReleaseUpdateClient:
    def __init__(
        self, *, timeout_seconds: float = 8.0, transport: httpx2.BaseTransport | None = None
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("release 检查超时必须大于 0")
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    def latest(self) -> ReleaseTarget:
        try:
            with httpx2.Client(
                timeout=self._timeout_seconds,
                follow_redirects=False,
                transport=self._transport,
                headers={"User-Agent": "PackBreaker-Updater"},
            ) as client:
                response = client.get(OFFICIAL_RELEASES_URL)
                if response.status_code not in {301, 302, 303, 307, 308}:
                    raise ReleaseUpdateError(
                        "RELEASE_LATEST_LOOKUP_FAILED",
                        f"GitHub latest release 未返回重定向（HTTP {response.status_code}）",
                    )
                location = response.headers.get("location", "")
                tag = _tag_from_release_location(location)
                version = tag.removeprefix("v")
                asset_url = (
                    f"https://github.com/YYxiaoma/PackBreaker/releases/download/{tag}/"
                    f"packbreaker-{version}.release.json"
                )
                manifest_response = client.get(asset_url, follow_redirects=True)
                if manifest_response.status_code != 200:
                    raise ReleaseUpdateError(
                        "RELEASE_MANIFEST_FETCH_FAILED",
                        f"正式 release manifest 下载失败（HTTP {manifest_response.status_code}）",
                    )
                try:
                    payload: Any = manifest_response.json()
                except ValueError as exc:
                    raise ReleaseUpdateError(
                        "RELEASE_MANIFEST_INVALID",
                        "正式 release manifest 不是合法 JSON",
                    ) from exc
        except ReleaseUpdateError:
            raise
        except httpx2.HTTPError as exc:
            raise ReleaseUpdateError(
                "RELEASE_NETWORK_FAILED", "无法连接 GitHub 正式发布通道"
            ) from exc
        return parse_release_manifest(payload, expected_tag=tag)


def _tag_from_release_location(value: str) -> str:
    parsed = urlparse(value)
    path = parsed.path if parsed.scheme else value.split("?", 1)[0]
    prefix = "/YYxiaoma/PackBreaker/releases/tag/"
    if not path.startswith(prefix):
        raise ReleaseUpdateError(
            "RELEASE_LATEST_LOCATION_INVALID", "GitHub latest release 重定向目标无效"
        )
    tag = path.removeprefix(prefix).rstrip("/")
    if _RELEASE_TAG.fullmatch(tag) is None:
        raise ReleaseUpdateError("RELEASE_TAG_INVALID", "GitHub latest release tag 不符合 vX.Y.Z")
    return tag


def _required_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip() or value.strip() != value:
        raise ReleaseUpdateError("RELEASE_MANIFEST_INVALID", f"release manifest 字段 {key} 无效")
    return value
