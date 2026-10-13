from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from uuid import UUID

from scripts.validate_v112_site_release_gate import (
    COMPLETE,
    MANIFEST,
    PHASES,
    RECEIPTS_DIR,
    SITES,
    validate_manifest,
    validate_source_unchanged,
)


def _fixture(tmp_path: Path, *, complete: bool) -> tuple[Path, dict[str, object]]:
    root = tmp_path
    docs = root / "docs"
    docs.mkdir()
    rows: list[dict[str, object]] = []
    for index, key in enumerate(sorted(SITES), start=1):
        row: dict[str, object] = {
            "site_key": key,
            "status": "PENDING_REAL_VALIDATION",
            "receipt_path": None,
            "receipt_sha256": None,
        }
        if complete:
            item_id = f"00000000-0000-4000-8000-{index:012d}"
            assert str(UUID(item_id)) == item_id
            reference = "https://github.com/YYxiaoma/PackBreaker/issues/5#issuecomment-123"
            receipt = {
                "site_key": key,
                "execution_id": item_id,
                "source_revision": "a" * 40,
                "evidence_ref": reference,
                "phases": {
                    phase: {
                        "status": "PASSED",
                        "execution_id": item_id,
                        "evidence_ref": reference,
                    }
                    for phase in PHASES
                },
            }
            path = root / RECEIPTS_DIR / f"{key}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            raw = (json.dumps(receipt, ensure_ascii=False, sort_keys=True) + "\n").encode()
            path.write_bytes(raw)
            row.update(
                status=COMPLETE,
                receipt_path=str(RECEIPTS_DIR / f"{key}.json"),
                receipt_sha256=hashlib.sha256(raw).hexdigest(),
            )
        rows.append(row)
    manifest: dict[str, object] = {
        "format_version": 1,
        "release_tag": "v1.1.2",
        "policy": "ALL_SEVEN_SITES_REAL_E2E_REQUIRED",
        "publication_approval": {
            "status": "APPROVED" if complete else "NOT_GRANTED",
            "evidence_ref": (
                "https://github.com/YYxiaoma/PackBreaker/issues/5#issuecomment-approval"
                if complete
                else None
            ),
        },
        "sites": rows,
    }
    (root / MANIFEST).write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return root, manifest


def _write(root: Path, payload: dict[str, object]) -> None:
    (root / MANIFEST).write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")


def test_pending_inventory_validates_but_release_is_blocked(tmp_path: Path) -> None:
    root, _ = _fixture(tmp_path, complete=False)
    errors, source = validate_manifest(root, root / MANIFEST, require_ready=False)
    assert errors == [] and source is None
    errors, source = validate_manifest(root, root / MANIFEST, require_ready=True)
    assert len(errors) >= 7 and source is None
    assert "PTerClub" in " ".join(errors)


def test_seven_complete_same_source_same_execution_proof_passes_offline(tmp_path: Path) -> None:
    root, _ = _fixture(tmp_path, complete=True)
    errors, source = validate_manifest(root, root / MANIFEST, require_ready=True)
    assert errors == [] and source == "a" * 40


def test_all_sites_complete_still_cannot_publish_without_explicit_approval(tmp_path: Path) -> None:
    root, payload = _fixture(tmp_path, complete=True)
    payload["publication_approval"] = {"status": "NOT_GRANTED", "evidence_ref": None}
    _write(root, payload)
    errors, _ = validate_manifest(root, root / MANIFEST, require_ready=True)
    assert any("尚未独立批准" in error for error in errors)


def test_missing_or_duplicate_site_fails_closed(tmp_path: Path) -> None:
    root, payload = _fixture(tmp_path, complete=True)
    rows = payload["sites"]
    assert isinstance(rows, list)
    rows.pop()
    _write(root, payload)
    errors, _ = validate_manifest(root, root / MANIFEST, require_ready=True)
    assert any("七站必须完整" in error for error in errors)
    rows.append(rows[0])
    _write(root, payload)
    errors, _ = validate_manifest(root, root / MANIFEST, require_ready=True)
    assert any("重复" in error for error in errors)


def test_tampered_receipt_digest_rejected(tmp_path: Path) -> None:
    root, _ = _fixture(tmp_path, complete=True)
    (root / RECEIPTS_DIR / "CarPT.json").write_text('{"fraudulent": true}', encoding="utf-8")
    errors, _ = validate_manifest(root, root / MANIFEST, require_ready=True)
    assert any("CarPT" in error and "摘要" in error for error in errors)


def test_symlink_receipt_rejected(tmp_path: Path) -> None:
    root, _ = _fixture(tmp_path, complete=True)
    receipt = root / RECEIPTS_DIR / "CarPT.json"
    real = root / RECEIPTS_DIR / "copy.json"
    shutil.copyfile(receipt, real)
    receipt.unlink()
    receipt.symlink_to(real)
    errors, _ = validate_manifest(root, root / MANIFEST, require_ready=True)
    assert any("CarPT" in error and "符号链接" in error for error in errors)


def test_symlinked_receipts_directory_cannot_escape_repo(tmp_path: Path) -> None:
    root, _ = _fixture(tmp_path, complete=True)
    original = root / RECEIPTS_DIR
    moved = tmp_path / "outside-receipts"
    original.rename(moved)
    original.symlink_to(moved, target_is_directory=True)
    errors, _ = validate_manifest(root, root / MANIFEST, require_ready=True)
    assert any("符号链接" in error for error in errors)


def test_partial_old_history_cannot_be_labeled_complete(tmp_path: Path) -> None:
    root, payload = _fixture(tmp_path, complete=True)
    receipt = root / RECEIPTS_DIR / "CarPT.json"
    obj = json.loads(receipt.read_text(encoding="utf-8"))
    obj["phases"]["client_verify_start"]["status"] = "PENDING"
    raw = (json.dumps(obj, ensure_ascii=False) + "\n").encode()
    receipt.write_bytes(raw)
    rows = payload["sites"]
    assert isinstance(rows, list)
    for row in rows:
        if row["site_key"] == "CarPT":
            row["receipt_sha256"] = hashlib.sha256(raw).hexdigest()
    _write(root, payload)
    errors, _ = validate_manifest(root, root / MANIFEST, require_ready=True)
    assert any("CarPT" in error and "阶段未通过" in error for error in errors)


def test_mixed_source_commits_fail_even_if_every_site_complete(tmp_path: Path) -> None:
    root, payload = _fixture(tmp_path, complete=True)
    receipt = root / RECEIPTS_DIR / "U2.json"
    obj = json.loads(receipt.read_text(encoding="utf-8"))
    obj["source_revision"] = "b" * 40
    raw = (json.dumps(obj, ensure_ascii=False) + "\n").encode()
    receipt.write_bytes(raw)
    rows = payload["sites"]
    assert isinstance(rows, list)
    for row in rows:
        if row["site_key"] == "U2":
            row["receipt_sha256"] = hashlib.sha256(raw).hexdigest()
    _write(root, payload)
    errors, _ = validate_manifest(root, root / MANIFEST, require_ready=True)
    assert any("同一份源码候选" in error for error in errors)


def test_cross_execution_phase_cannot_borrow_another_run(tmp_path: Path) -> None:
    root, payload = _fixture(tmp_path, complete=True)
    receipt = root / RECEIPTS_DIR / "U2.json"
    obj = json.loads(receipt.read_text(encoding="utf-8"))
    obj["phases"]["piece_hash"]["execution_id"] = "00000000-0000-4000-8000-999999999999"
    raw = (json.dumps(obj, ensure_ascii=False) + "\n").encode()
    receipt.write_bytes(raw)
    rows = payload["sites"]
    assert isinstance(rows, list)
    for row in rows:
        if row["site_key"] == "U2":
            row["receipt_sha256"] = hashlib.sha256(raw).hexdigest()
    _write(root, payload)
    errors, _ = validate_manifest(root, root / MANIFEST, require_ready=True)
    assert any("U2" in error and "同一个真实 execution" in error for error in errors)


def test_invalid_or_non_https_evidence_ref_rejected(tmp_path: Path) -> None:
    root, payload = _fixture(tmp_path, complete=True)
    receipt = root / RECEIPTS_DIR / "TANGPT.json"
    obj = json.loads(receipt.read_text(encoding="utf-8"))
    obj["evidence_ref"] = "http://localhost/pretend"
    raw = (json.dumps(obj, ensure_ascii=False) + "\n").encode()
    receipt.write_bytes(raw)
    rows = payload["sites"]
    assert isinstance(rows, list)
    for row in rows:
        if row["site_key"] == "TANGPT":
            row["receipt_sha256"] = hashlib.sha256(raw).hexdigest()
    _write(root, payload)
    errors, _ = validate_manifest(root, root / MANIFEST, require_ready=True)
    assert any("TANGPT" in error and "外部验收" in error for error in errors)


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def test_release_code_identity_must_not_change_after_real_e2e(tmp_path: Path) -> None:
    root, _ = _fixture(tmp_path, complete=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "tests@example.invalid")
    _git(root, "config", "user.name", "Test")
    (root / "backend").mkdir()
    (root / "backend" / "app.py").write_text("version = 1\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "candidate frozen")
    accepted = _git(root, "rev-parse", "HEAD")
    assert validate_source_unchanged(root, accepted) == []
    (root / "backend" / "app.py").write_text("version = 2\n", encoding="utf-8")
    _git(root, "add", "backend/app.py")
    _git(root, "commit", "-qm", "modified production source after acceptance")
    assert any("非证据变更" in message for message in validate_source_unchanged(root, accepted))


def test_release_rejects_uncommitted_changes(tmp_path: Path) -> None:
    root, _ = _fixture(tmp_path, complete=False)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "tests@example.invalid")
    _git(root, "config", "user.name", "Test")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "candidate")
    accepted = _git(root, "rev-parse", "HEAD")
    (root / "docs" / "scratch.txt").write_text("uncommitted", encoding="utf-8")
    assert any("未提交" in message for message in validate_source_unchanged(root, accepted))
