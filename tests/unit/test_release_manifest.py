from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

import scripts.release_manifest as release_manifest
from scripts.release_manifest import build_release_manifest, project_version, validate_release_tag


def test_release_tag_must_match_project_version() -> None:
    version = project_version()
    assert validate_release_tag(f"v{version}") == version
    with pytest.raises(ValueError, match="项目版本"):
        validate_release_tag("v9.9.9")


def test_release_manifest_binds_version_digest_commit_and_sbom(tmp_path: Path) -> None:
    version = project_version()
    sbom = tmp_path / f"packbreaker-{version}.spdx.json"
    sbom.write_text('{"spdxVersion":"SPDX-2.3"}\n', encoding="utf-8")

    payload = build_release_manifest(
        tag=f"v{version}",
        image="ghcr.io/yyxiaoma/packbreaker",
        image_digest=f"sha256:{'a' * 64}",
        commit="b" * 40,
        sbom_path=sbom,
        generated_at=datetime(2026, 9, 14, 8, 0, tzinfo=UTC),
    )

    assert payload["version"] == version
    assert payload["immutable_image"] == f"ghcr.io/yyxiaoma/packbreaker@sha256:{'a' * 64}"
    if tuple(int(part) for part in version.split(".")) >= (1, 0, 0):
        assert payload["format_version"] == 2
        assert payload["platforms"] == ["linux/amd64", "linux/arm64"]
    else:
        assert payload["format_version"] == 1
        assert payload["platform"] == "linux/amd64"
    assert payload["sbom_file"] == sbom.name
    assert len(str(payload["sbom_sha256"])) == 64
    assert payload["generated_at"] == "2026-09-14T08:00:00Z"


def test_v100_manifest_declares_both_platforms_and_immutable_index(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(release_manifest, "project_version", lambda: "1.0.0")
    sbom = tmp_path / "sbom.json"
    sbom.write_text("{}", encoding="utf-8")
    manifest = build_release_manifest(
        tag="v1.0.0",
        image="ghcr.io/yyxiaoma/packbreaker",
        image_digest="sha256:" + "a" * 64,
        commit="b" * 40,
        sbom_path=sbom,
    )
    assert manifest["format_version"] == 2
    assert manifest["platforms"] == ["linux/amd64", "linux/arm64"]
    assert "platform" not in manifest


def test_release_manifest_rejects_tag_version_mismatch(tmp_path: Path) -> None:
    sbom = tmp_path / "sbom.json"
    sbom.write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="项目版本"):
        build_release_manifest(
            tag="v9.9.9",
            image="ghcr.io/yyxiaoma/packbreaker",
            image_digest=f"sha256:{'a' * 64}",
            commit="b" * 40,
            sbom_path=sbom,
        )


def test_release_manifest_rejects_mutable_or_invalid_identity(tmp_path: Path) -> None:
    sbom = tmp_path / "sbom.json"
    sbom.write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="完整 sha256"):
        build_release_manifest(
            tag=f"v{project_version()}",
            image="ghcr.io/yyxiaoma/packbreaker",
            image_digest="sha256:short",
            commit="b" * 40,
            sbom_path=sbom,
        )
    with pytest.raises(ValueError, match="小写规范引用"):
        build_release_manifest(
            tag=f"v{project_version()}",
            image="ghcr.io/YYxiaoma/PackBreaker",
            image_digest=f"sha256:{'a' * 64}",
            commit="b" * 40,
            sbom_path=sbom,
        )
