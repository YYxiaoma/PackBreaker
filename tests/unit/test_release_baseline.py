from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.validate_release_baseline import load_release_baseline, project_version

ROOT = Path(__file__).resolve().parents[2]


def test_repository_release_baseline_is_immutable_v019() -> None:
    baseline = load_release_baseline()

    assert baseline.version == "0.1.9"
    assert baseline.tag == "v0.1.9"
    assert baseline.commit == "cc70391cb42adc8755637d1cf23d407902e30dfe"
    assert baseline.alembic_revision == "0029_v018_compatibility"
    assert baseline.release_workflow_run_id == 35429394091
    assert baseline.immutable_image == (
        "ghcr.io/yyxiaoma/packbreaker@"
        "sha256:3bf7eee3825821a5fef28338b7f1ce749cbae353bcd3a4ef81883ee6a021261d"
    )


def test_v100_multiplatform_baseline_contract(tmp_path: Path) -> None:
    payload = json.loads((ROOT / "release-baseline.json").read_text(encoding="utf-8"))
    payload.update(
        format_version=2,
        version="1.0.0",
        tag="v1.0.0",
        platform="multi",
        platforms=["linux/amd64", "linux/arm64"],
    )
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps(payload), encoding="utf-8")
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nversion = "1.0.0"\n', encoding="utf-8")
    result = load_release_baseline(baseline, pyproject=pyproject)
    assert result.platforms == ("linux/amd64", "linux/arm64")
    assert result.as_dict()["platforms"] == ("linux/amd64", "linux/arm64")

    payload["platforms"] = ["linux/amd64"]
    baseline.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="必须包含"):
        load_release_baseline(baseline, pyproject=pyproject)


def test_release_baseline_rejects_mutable_or_mismatched_identity(tmp_path: Path) -> None:
    payload = json.loads((ROOT / "release-baseline.json").read_text(encoding="utf-8"))
    payload["image_digest"] = "sha256:short"
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="完整 sha256"):
        load_release_baseline(baseline)

    payload = json.loads((ROOT / "release-baseline.json").read_text(encoding="utf-8"))
    payload["tag"] = "v0.1.2"
    baseline.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="tag 与 version"):
        load_release_baseline(baseline)


def test_release_baseline_cannot_be_newer_than_project_version(tmp_path: Path) -> None:
    payload = json.loads((ROOT / "release-baseline.json").read_text(encoding="utf-8"))
    major, minor, patch = (int(part) for part in project_version().split("."))
    future_version = f"{major}.{minor}.{patch + 1}"
    payload["version"] = future_version
    payload["tag"] = f"v{future_version}"
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="不能新于"):
        load_release_baseline(baseline)


def test_cross_image_gate_restores_baseline_backup_instead_of_downgrading_database() -> None:
    script = (ROOT / "scripts" / "check-release-upgrade.sh").read_text(encoding="utf-8")

    assert 'docker pull "$baseline_image"' in script
    assert "release_upgrade_probe" in script
    assert "backend.app.maintenance backup" in script
    assert "backend.app.maintenance restore-backup" in script
    assert "--confirm-replace-current-database" in script
    assert "alembic downgrade" not in script
