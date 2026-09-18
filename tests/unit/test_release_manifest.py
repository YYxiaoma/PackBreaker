from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from scripts.release_manifest import build_release_manifest, validate_release_tag


def test_release_tag_must_match_project_version() -> None:
    assert validate_release_tag("v0.1.6") == "0.1.6"
    with pytest.raises(ValueError, match="项目版本"):
        validate_release_tag("v0.2.0")


def test_release_manifest_binds_version_digest_commit_and_sbom(tmp_path: Path) -> None:
    sbom = tmp_path / "packbreaker-0.1.6.spdx.json"
    sbom.write_text('{"spdxVersion":"SPDX-2.3"}\n', encoding="utf-8")

    payload = build_release_manifest(
        tag="v0.1.6",
        image="ghcr.io/yyxiaoma/packbreaker",
        image_digest=f"sha256:{'a' * 64}",
        commit="b" * 40,
        sbom_path=sbom,
        generated_at=datetime(2026, 9, 14, 8, 0, tzinfo=UTC),
    )

    assert payload["version"] == "0.1.6"
    assert payload["immutable_image"] == f"ghcr.io/yyxiaoma/packbreaker@sha256:{'a' * 64}"
    assert payload["platform"] == "linux/amd64"
    assert payload["sbom_file"] == sbom.name
    assert len(str(payload["sbom_sha256"])) == 64
    assert payload["generated_at"] == "2026-09-14T08:00:00Z"


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
            tag="v0.1.6",
            image="ghcr.io/yyxiaoma/packbreaker",
            image_digest="sha256:short",
            commit="b" * 40,
            sbom_path=sbom,
        )
    with pytest.raises(ValueError, match="小写规范引用"):
        build_release_manifest(
            tag="v0.1.6",
            image="ghcr.io/YYxiaoma/PackBreaker",
            image_digest=f"sha256:{'a' * 64}",
            commit="b" * 40,
            sbom_path=sbom,
        )
