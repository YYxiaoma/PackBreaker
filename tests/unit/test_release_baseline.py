from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.validate_release_baseline import load_release_baseline

ROOT = Path(__file__).resolve().parents[2]


def test_repository_release_baseline_is_immutable_v016() -> None:
    baseline = load_release_baseline()

    assert baseline.version == "0.1.6"
    assert baseline.tag == "v0.1.6"
    assert baseline.commit == "a148aef5c2062829246c8a9f85e76c846da213ed"
    assert baseline.alembic_revision == "0026_ai_agent_v016"
    assert baseline.release_workflow_run_id == 35297829243
    assert baseline.immutable_image == (
        "ghcr.io/yyxiaoma/packbreaker@"
        "sha256:b250b4dd945648fca884989d4c6ce14692dea839d4364f462d6080806357f13d"
    )


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
    payload["version"] = "0.1.8"
    payload["tag"] = "v0.1.8"
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
