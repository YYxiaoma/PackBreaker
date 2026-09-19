from __future__ import annotations

import json
import sys
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.verify_release_platforms import main, verify_release_children, verify_release_index

_OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
_OCI_CONFIG = "application/vnd.oci.image.config.v1+json"
_OCI_LAYER = "application/vnd.oci.image.layer.v1.tar+gzip"


def _child(digit: str = "d") -> dict[str, Any]:
    return {
        "schemaVersion": 2,
        "mediaType": _OCI_MANIFEST,
        "config": {"mediaType": _OCI_CONFIG, "digest": "sha256:" + digit * 64, "size": 432},
        "layers": [{"mediaType": _OCI_LAYER, "digest": "sha256:" + "e" * 64, "size": 654}],
    }


def _child_index() -> dict[str, Any]:
    return {
        "manifests": [
            {
                "mediaType": _OCI_MANIFEST,
                "digest": "sha256:" + "a" * 64,
                "size": 123,
                "platform": {"os": "linux", "architecture": "amd64"},
            },
            {
                "mediaType": _OCI_MANIFEST,
                "digest": "sha256:" + "b" * 64,
                "size": 456,
                "platform": {"os": "linux", "architecture": "arm64", "variant": "v8"},
            },
            {
                "mediaType": _OCI_MANIFEST,
                "digest": "sha256:" + "c" * 64,
                "size": 111,
                "platform": {"os": "unknown", "architecture": "unknown"},
            },
        ]
    }


def _index(*platforms: tuple[str, str]) -> dict[str, object]:
    return {"manifests": [{"platform": {"os": os, "architecture": arch}} for os, arch in platforms]}


def test_multiarch_index_accepts_only_two_real_platforms_and_provenance() -> None:
    verify_release_index(
        _index(("linux", "amd64"), ("unknown", "unknown"), ("linux", "arm64")),
        multiarch=True,
    )
    verify_release_index(_index(("linux", "amd64")), multiarch=False)
    arm_v8 = {
        "manifests": [
            {"platform": {"os": "linux", "architecture": "amd64"}},
            {"platform": {"os": "linux", "architecture": "arm64", "variant": "v8"}},
        ]
    }
    verify_release_index(arm_v8, multiarch=True)


@pytest.mark.parametrize(
    "index",
    [
        _index(("linux", "amd64")),
        _index(("linux", "arm64")),
        _index(("linux", "amd64"), ("linux", "amd64"), ("linux", "arm64")),
        _index(("linux", "amd64"), ("linux", "arm64"), ("linux", "arm/v7")),
        {"schemaVersion": 2},
        {"manifests": []},
    ],
)
def test_multiarch_index_rejects_missing_duplicate_or_extra_platforms(index: object) -> None:
    with pytest.raises(ValueError):
        verify_release_index(index, multiarch=True)


def test_release_children_resolves_both_immutable_platform_digests_not_attestation() -> None:
    requested: list[str] = []

    def read_child(digest: str) -> object:
        requested.append(digest)
        return _child()

    verify_release_children(_child_index(), multiarch=True, read_manifest=read_child)
    assert requested == ["sha256:" + "a" * 64, "sha256:" + "b" * 64]


def test_legacy_amd64_release_still_resolves_its_child() -> None:
    index = _child_index()
    index["manifests"] = index["manifests"][:1]
    verify_release_children(index, multiarch=False, read_manifest=lambda _: _child())


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("digest", "sha256:short", "digest"),
        ("size", 0, "size"),
        ("size", True, "size"),
        ("mediaType", "application/vnd.oci.image.index.v1+json", "mediaType"),
    ],
)
def test_release_children_fail_closed_on_invalid_platform_descriptor(
    field: str, value: object, message: str
) -> None:
    index = _child_index()
    index["manifests"][1][field] = value
    with pytest.raises(ValueError, match=message):
        verify_release_children(index, multiarch=True, read_manifest=lambda _: _child())


def test_release_children_rejects_one_digest_claimed_by_two_architectures() -> None:
    index = _child_index()
    index["manifests"][1]["digest"] = index["manifests"][0]["digest"]
    with pytest.raises(ValueError, match="共用同一个"):
        verify_release_children(index, multiarch=True, read_manifest=lambda _: _child())


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda child: child.update(schemaVersion=1), "schemaVersion"),
        (
            lambda child: child.update(mediaType="application/vnd.oci.image.index.v1+json"),
            "mediaType",
        ),
        (lambda child: child.pop("config"), "config"),
        (lambda child: child["config"].update(digest="bad"), "digest"),
        (lambda child: child["config"].update(size=-1), "size"),
        (lambda child: child.update(layers=[]), "运行层"),
        (lambda child: child["layers"][0].update(mediaType="text/plain"), "mediaType"),
        (lambda child: child["layers"][0].update(digest="sha256:nope"), "digest"),
        (lambda child: child["layers"][0].update(size=0), "size"),
    ],
)
def test_release_children_rejects_missing_or_non_image_child_manifests(
    mutation: Callable[[dict[str, Any]], object], message: str
) -> None:
    child = _child()
    mutation(child)
    with pytest.raises(ValueError, match=message):
        verify_release_children(_child_index(), multiarch=True, read_manifest=lambda _: child)


def test_cli_reads_index_and_each_child_by_exact_digest(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    image = "ghcr.io/yyxiaoma/packbreaker@sha256:" + "f" * 64
    requested: list[list[str]] = []

    def fake_run(args: list[str], **_kwargs: object) -> SimpleNamespace:
        requested.append(args)
        payload = _child_index() if args[-1] == image else _child()
        return SimpleNamespace(stdout=json.dumps(payload))

    monkeypatch.setattr("scripts.verify_release_platforms.subprocess.run", fake_run)
    monkeypatch.setattr(
        sys, "argv", ["verify_release_platforms.py", "--image", image, "--multiarch"]
    )
    assert main() == 0
    assert [args[-1] for args in requested] == [
        image,
        "ghcr.io/yyxiaoma/packbreaker@sha256:" + "a" * 64,
        "ghcr.io/yyxiaoma/packbreaker@sha256:" + "b" * 64,
    ]
    assert "每个平台不可变子镜像" in capsys.readouterr().out
