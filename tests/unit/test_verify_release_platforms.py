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
TAG = "v1.0.0"
COMMIT = "1" * 40


def _child(digit: str = "d") -> dict[str, Any]:
    return {
        "schemaVersion": 2,
        "mediaType": _OCI_MANIFEST,
        "config": {"mediaType": _OCI_CONFIG, "digest": "sha256:" + digit * 64, "size": 432},
        "layers": [{"mediaType": _OCI_LAYER, "digest": "sha256:" + "e" * 64, "size": 654}],
    }


def _config(arch: str = "amd64") -> dict[str, Any]:
    return {
        "os": "linux",
        "architecture": arch,
        "config": {
            "Labels": {
                "org.opencontainers.image.version": TAG[1:],
                "org.opencontainers.image.revision": COMMIT,
            }
        },
        "rootfs": {"type": "layers", "diff_ids": ["sha256:" + "f" * 64]},
    }


def _verify(
    index: object,
    *,
    multiarch: bool = True,
    read_manifest: Callable[[str], object] = lambda _: _child(),
    read_config: Callable[[str], object] | None = None,
    tag: str = TAG,
    commit: str = COMMIT,
) -> None:
    if read_config is None:

        def read_config(digest: str) -> object:
            return _config("amd64" if digest == "sha256:" + "a" * 64 else "arm64")

    verify_release_children(
        index,
        multiarch=multiarch,
        read_manifest=read_manifest,
        read_config=read_config,
        tag=tag,
        commit=commit,
    )


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

    _verify(_child_index(), read_manifest=read_child)
    assert requested == ["sha256:" + "a" * 64, "sha256:" + "b" * 64]


def test_legacy_amd64_release_still_resolves_its_child() -> None:
    index = _child_index()
    index["manifests"] = index["manifests"][:1]
    _verify(index, multiarch=False)


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
        _verify(index)


def test_release_children_rejects_one_digest_claimed_by_two_architectures() -> None:
    index = _child_index()
    index["manifests"][1]["digest"] = index["manifests"][0]["digest"]
    with pytest.raises(ValueError, match="共用同一个"):
        _verify(index)


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
        _verify(_child_index(), read_manifest=lambda _: child)


def test_cli_reads_index_and_each_child_by_exact_digest(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    image = "ghcr.io/yyxiaoma/packbreaker@sha256:" + "f" * 64
    requested: list[list[str]] = []

    def fake_run(args: list[str], **_kwargs: object) -> SimpleNamespace:
        requested.append(args)
        if args[-1] == image:
            payload = _child_index()
        elif "--format" in args:
            payload = _config("amd64" if args[-1].endswith("a" * 64) else "arm64")
        else:
            payload = _child()
        return SimpleNamespace(stdout=json.dumps(payload))

    monkeypatch.setattr("scripts.verify_release_platforms.subprocess.run", fake_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "verify_release_platforms.py",
            "--image",
            image,
            "--multiarch",
            "--tag",
            TAG,
            "--commit",
            COMMIT,
        ],
    )
    assert main() == 0
    assert [args[-1] for args in requested] == [
        image,
        "ghcr.io/yyxiaoma/packbreaker@sha256:" + "a" * 64,
        "ghcr.io/yyxiaoma/packbreaker@sha256:" + "a" * 64,
        "ghcr.io/yyxiaoma/packbreaker@sha256:" + "b" * 64,
        "ghcr.io/yyxiaoma/packbreaker@sha256:" + "b" * 64,
    ]
    assert "实际架构/版本/提交身份" in capsys.readouterr().out


@pytest.mark.parametrize("architecture", ["amd64", "arm64"])
def test_release_children_rejects_mislabeled_actual_architecture(architecture: str) -> None:
    def config_for(digest: str) -> object:
        actual = "amd64" if digest.endswith("a" * 64) else "arm64"
        return _config(architecture if actual != architecture else "riscv64")

    with pytest.raises(ValueError, match="实际镜像配置与索引平台声明不一致"):
        _verify(_child_index(), read_config=config_for)


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (("os",), "windows", "平台声明"),
        (("architecture",), "amd64", "平台声明"),
        (("variant",), "v7", "variant"),
        (("config", "Labels", "org.opencontainers.image.version"), "0.1.9", "版本标签"),
        (("config", "Labels", "org.opencontainers.image.revision"), "2" * 40, "提交标签"),
        (("config", "Labels"), {}, "版本标签"),
        (("rootfs", "diff_ids"), [], "rootfs"),
        (("rootfs", "diff_ids"), ["sha256:short"], "rootfs"),
        (("rootfs", "type"), "empty", "rootfs"),
    ],
)
def test_release_children_rejects_bad_actual_image_config(
    path: tuple[str, ...], value: object, message: str
) -> None:
    config = _config("arm64")
    target = config
    for field in path[:-1]:
        target = target[field]
    target[path[-1]] = value
    with pytest.raises(ValueError, match=message):
        _verify(
            _child_index(),
            read_config=lambda digest: _config() if digest.endswith("a" * 64) else config,
        )


@pytest.mark.parametrize(
    ("tag", "commit"),
    [("v1.0.1", COMMIT), (TAG, "2" * 40), ("bad", COMMIT), (TAG, "short")],
)
def test_release_children_rejects_identity_mismatch_and_invalid_workflow_inputs(
    tag: str, commit: str
) -> None:
    with pytest.raises(ValueError, match="标签|发布 tag|提交 SHA"):
        _verify(_child_index(), tag=tag, commit=commit)
