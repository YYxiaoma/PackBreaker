from __future__ import annotations

import json
from pathlib import Path

from scripts.validate_acceptance_evidence import validate_manifest

ROOT = Path(__file__).resolve().parents[2]


def test_repository_v1_acceptance_evidence_manifest_is_complete_and_resolvable() -> None:
    manifest = ROOT / "docs" / "v1-acceptance-evidence.json"

    assert validate_manifest(ROOT, manifest) == []
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    statuses = [item["status"] for item in payload["requirements"]]
    assert statuses.count("pending_external") == 0
    assert payload["requirements"][-1]["id"] == "V1-025"
    assert payload["requirements"][-1]["status"] == "covered"


def test_acceptance_evidence_validator_rejects_stale_anchor(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "testing.md").write_text("synthetic criterion\n", encoding="utf-8")
    evidence = tmp_path / "evidence.txt"
    evidence.write_text("current anchor\n", encoding="utf-8")
    requirements = []
    for index in range(1, 26):
        anchor = "stale anchor" if index == 1 else "current anchor"
        requirements.append(
            {
                "id": f"V1-{index:03d}",
                "criterion": "synthetic criterion",
                "status": "covered",
                "evidence": [{"path": "evidence.txt", "contains": anchor}],
            }
        )
    manifest = docs / "v1-acceptance-evidence.json"
    manifest.write_text(
        json.dumps({"format_version": 1, "requirements": requirements}), encoding="utf-8"
    )

    errors = validate_manifest(tmp_path, manifest)

    assert any("evidence 锚点失效" in error for error in errors)
