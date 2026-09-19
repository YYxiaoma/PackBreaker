from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

import scripts.release_manifest as release_manifest
from scripts.verify_release_evidence import verify_release_evidence

IMAGE = "ghcr.io/yyxiaoma/packbreaker"
DIGEST = "sha256:" + "a" * 64
COMMIT = "b" * 40
TAG = "v1.0.0"


def _assets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, version: str = "1.0.0") -> Path:
    monkeypatch.setattr(release_manifest, "project_version", lambda: version)
    release_dir = tmp_path / "release"
    release_dir.mkdir()
    sbom = release_dir / f"packbreaker-{version}.spdx.json"
    sbom.write_text('{"spdxVersion":"SPDX-2.3","name":"synthetic"}\n', encoding="utf-8")
    manifest = release_manifest.build_release_manifest(
        tag=f"v{version}",
        image=IMAGE,
        image_digest=DIGEST,
        commit=COMMIT,
        sbom_path=sbom,
        generated_at=datetime(2026, 9, 20, 0, 0, tzinfo=UTC),
    )
    (release_dir / f"packbreaker-{version}.release.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    _checksums(release_dir)
    return release_dir


def _checksums(release_dir: Path) -> None:
    rows = []
    for file in sorted(release_dir.glob("*.json")):
        rows.append(f"{hashlib.sha256(file.read_bytes()).hexdigest()}  {file.name}")
    (release_dir / "SHA256SUMS").write_text("\n".join(rows) + "\n", encoding="ascii")


def _verify(release_dir: Path, **overrides: str) -> None:
    args = {"tag": TAG, "image": IMAGE, "image_digest": DIGEST, "commit": COMMIT}
    args.update(overrides)
    verify_release_evidence(release_dir, **args)


def test_v100_release_evidence_cross_checks_two_platforms_and_all_assets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _verify(_assets(tmp_path, monkeypatch))


def test_legacy_release_evidence_still_accepts_single_amd64_platform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release_dir = _assets(tmp_path, monkeypatch, version="0.1.9")
    _verify(release_dir, tag="v0.1.9")
    manifest_file = release_dir / "packbreaker-0.1.9.release.json"
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    manifest["platform"] = "multi"
    manifest_file.write_text(json.dumps(manifest), encoding="utf-8")
    _checksums(release_dir)
    with pytest.raises(ValueError, match="发布清单 platform"):
        _verify(release_dir, tag="v0.1.9")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("format_version", 1),
        ("platforms", ["linux/amd64"]),
        ("platforms", ["linux/arm64", "linux/amd64"]),
        ("platforms", ["linux/amd64", "linux/arm64", "linux/arm/v7"]),
        ("image_digest", "sha256:" + "c" * 64),
        ("immutable_image", f"{IMAGE}@sha256:" + "c" * 64),
        ("commit", "c" * 40),
        ("sbom_file", "other.spdx.json"),
        ("tag", "v0.1.9"),
        ("version", "0.1.9"),
        ("generated_at", "2026-09-20T00:00:00"),
    ],
)
def test_release_evidence_rejects_wrong_platform_or_identity_even_with_recomputed_checksums(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, value: object
) -> None:
    release_dir = _assets(tmp_path, monkeypatch)
    manifest_file = release_dir / "packbreaker-1.0.0.release.json"
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    manifest[field] = value
    manifest_file.write_text(json.dumps(manifest), encoding="utf-8")
    _checksums(release_dir)
    with pytest.raises(ValueError, match="发布清单"):
        _verify(release_dir)


def test_release_evidence_detects_sbom_tamper_and_mismatched_checksums(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release_dir = _assets(tmp_path, monkeypatch)
    (release_dir / "packbreaker-1.0.0.spdx.json").write_text(
        '{"spdxVersion":"SPDX-2.3","name":"tampered"}', encoding="utf-8"
    )
    with pytest.raises(ValueError, match="SBOM 内容哈希"):
        _verify(release_dir)

    manifest_file = release_dir / "packbreaker-1.0.0.release.json"
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    manifest["sbom_sha256"] = hashlib.sha256(
        (release_dir / "packbreaker-1.0.0.spdx.json").read_bytes()
    ).hexdigest()
    manifest_file.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="SHA256SUMS"):
        _verify(release_dir)


@pytest.mark.parametrize(
    "failure", ["missing", "extra", "duplicate", "symlink", "bad-spdx", "bad-checksum"]
)
def test_release_evidence_rejects_incomplete_or_unsafe_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    release_dir = _assets(tmp_path, monkeypatch)
    if failure == "missing":
        (release_dir / "SHA256SUMS").unlink()
    elif failure == "extra":
        (release_dir / "unverified.bin").write_bytes(b"extra")
    elif failure == "duplicate":
        sums = release_dir / "SHA256SUMS"
        sums.write_bytes(sums.read_bytes() + sums.read_bytes().splitlines(keepends=True)[0])
    elif failure == "symlink":
        sbom = release_dir / "packbreaker-1.0.0.spdx.json"
        source = tmp_path / "external-sbom.json"
        source.write_bytes(sbom.read_bytes())
        sbom.unlink()
        sbom.symlink_to(source)
    elif failure == "bad-spdx":
        (release_dir / "packbreaker-1.0.0.spdx.json").write_text(
            '{"spdxVersion":"SPDX-2.2"}', encoding="utf-8"
        )
    elif failure == "bad-checksum":
        sums = release_dir / "SHA256SUMS"
        sums.write_text(
            sums.read_text(encoding="ascii").replace(
                "  packbreaker-1.0.0.spdx.json", " *other.json"
            ),
            encoding="ascii",
        )
    with pytest.raises((OSError, ValueError)):
        _verify(release_dir)


def test_release_evidence_rejects_mismatched_external_workflow_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release_dir = _assets(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="发布清单"):
        _verify(release_dir, image_digest="sha256:" + "c" * 64)
    with pytest.raises(ValueError, match="发布清单"):
        _verify(release_dir, commit="c" * 40)
    with pytest.raises(ValueError, match="发布清单"):
        _verify(release_dir, tag="v1.0.1")
