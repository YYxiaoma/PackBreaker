from __future__ import annotations

import json

import httpx2
import pytest

from backend.app.infrastructure.release_updates import (
    OFFICIAL_IMAGE,
    ReleaseUpdateClient,
    ReleaseUpdateError,
    parse_release_manifest,
    semantic_version,
)

_DIGEST = "sha256:" + "7" * 64
_COMMIT = "9" * 40


def _manifest() -> dict[str, object]:
    return {
        "format_version": 1,
        "version": "0.1.2",
        "tag": "v0.1.2",
        "commit": _COMMIT,
        "image": OFFICIAL_IMAGE,
        "image_digest": _DIGEST,
        "immutable_image": f"{OFFICIAL_IMAGE}@{_DIGEST}",
        "platform": "linux/amd64",
    }


def test_semantic_version_and_manifest_identity() -> None:
    assert semantic_version("0.1.12") > semantic_version("0.1.2")
    target = parse_release_manifest(_manifest(), expected_tag="v0.1.2")
    assert target.version == "0.1.2"
    assert target.immutable_image == f"{OFFICIAL_IMAGE}@{_DIGEST}"


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("image", "ghcr.io/attacker/packbreaker", "RELEASE_IMAGE_UNTRUSTED"),
        ("image_digest", "sha256:short", "RELEASE_DIGEST_INVALID"),
        ("platform", "linux/arm64", "RELEASE_PLATFORM_UNSUPPORTED"),
        ("commit", "not-a-commit", "RELEASE_COMMIT_INVALID"),
    ],
)
def test_manifest_rejects_untrusted_identity(field: str, value: str, code: str) -> None:
    payload = _manifest()
    payload[field] = value
    if field in {"image", "image_digest"}:
        payload["immutable_image"] = f"{payload['image']}@{payload['image_digest']}"

    with pytest.raises(ReleaseUpdateError) as exc_info:
        parse_release_manifest(payload, expected_tag="v0.1.2")

    assert exc_info.value.code == code


def test_manifest_rejects_tag_mismatch() -> None:
    with pytest.raises(ReleaseUpdateError) as exc_info:
        parse_release_manifest(_manifest(), expected_tag="v0.1.3")
    assert exc_info.value.code == "RELEASE_IDENTITY_MISMATCH"


def test_release_client_resolves_latest_redirect_then_fetches_manifest() -> None:
    seen: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(str(request.url))
        if request.url.path == "/YYxiaoma/PackBreaker/releases/latest":
            return httpx2.Response(
                302,
                headers={"Location": "https://github.com/YYxiaoma/PackBreaker/releases/tag/v0.1.2"},
            )
        if request.url.path.endswith("/packbreaker-0.1.2.release.json"):
            return httpx2.Response(200, content=json.dumps(_manifest()).encode())
        return httpx2.Response(404)

    target = ReleaseUpdateClient(transport=httpx2.MockTransport(handler)).latest()

    assert target.version == "0.1.2"
    assert len(seen) == 2
    assert seen[0].endswith("/releases/latest")
    assert seen[1].endswith("/packbreaker-0.1.2.release.json")
