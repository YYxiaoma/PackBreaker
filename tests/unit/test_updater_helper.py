from __future__ import annotations

import json
from pathlib import Path

from backend.app.updater_helper import UpdaterStateStore


def test_state_store_starts_idle_when_no_prior_state_exists(tmp_path: Path) -> None:
    store = UpdaterStateStore(tmp_path / "state.json", helper_version="0.1.1")

    status = store.initial()

    assert status.phase == "idle"
    assert status.rollback_performed is False
    assert (tmp_path / "state.json").is_file()


def test_state_store_marks_interrupted_active_upgrade_for_manual_recovery(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "phase": "starting",
                "message": "starting",
                "request_id": "request-001",
                "current_version": "0.1.1",
                "target_version": "0.1.2",
                "target_image": "ghcr.io/yyxiaoma/packbreaker@sha256:" + "8" * 64,
            }
        ),
        encoding="utf-8",
    )
    store = UpdaterStateStore(state_path, helper_version="0.1.1")

    status = store.initial()

    assert status.phase == "manual_recovery_required"
    assert status.rollback_performed is True
    assert status.request_id == "request-001"


def test_state_store_fails_closed_when_state_file_is_corrupt(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text("not-json", encoding="utf-8")
    store = UpdaterStateStore(state_path, helper_version="0.1.1")

    status = store.initial()

    assert status.phase == "manual_recovery_required"
    assert status.rollback_performed is True
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert persisted["phase"] == "manual_recovery_required"
