from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.app.infrastructure.updater_protocol import UPDATER_PROTOCOL_VERSION, UpdaterStatus
from backend.app.updater_helper import HelperRequestError, UpdaterStateStore, _run_oneshot


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


def test_oneshot_persists_early_request_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / "config"
    requests_dir = config_dir / "transient-updater" / "requests"
    requests_dir.mkdir(parents=True, mode=0o700)
    docker_socket = tmp_path / "docker.sock"
    docker_socket.touch()
    state_path = config_dir / "transient-updater" / "state.json"
    request_path = requests_dir / "broken.json"
    request_path.write_text("{broken", encoding="utf-8")
    UpdaterStateStore(state_path, helper_version="0.1.4").set(
        UpdaterStatus(
            protocol_version=UPDATER_PROTOCOL_VERSION,
            helper_version="0.1.4",
            phase="accepted",
            message="accepted",
            request_id="request-early-failure",
            current_version="0.1.4",
            target_version="0.1.5",
            target_image="ghcr.io/yyxiaoma/packbreaker@sha256:" + "8" * 64,
        )
    )
    monkeypatch.setenv("PACKBREAKER_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("PACKBREAKER_DOCKER_SOCKET", str(docker_socket))
    monkeypatch.setenv("PACKBREAKER_UPDATER_TARGET_CONTAINER", "packbreaker")
    monkeypatch.setenv("PACKBREAKER_UPDATER_ALLOWED_IMAGE", "ghcr.io/yyxiaoma/packbreaker")
    monkeypatch.setenv("PACKBREAKER_UPDATER_STATE_FILE", str(state_path))

    with pytest.raises(HelperRequestError) as exc_info:
        _run_oneshot(request_path)

    assert exc_info.value.code == "UPDATER_REQUEST_UNREADABLE"
    status = UpdaterStateStore(state_path, helper_version="0.1.4").get()
    assert status.phase == "failed"
    assert "UPDATER_REQUEST_UNREADABLE" in status.message
    assert request_path.exists() is False


def test_oneshot_preserves_invalid_backup_name_in_failed_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / "config"
    requests_dir = config_dir / "transient-updater" / "requests"
    requests_dir.mkdir(parents=True, mode=0o700)
    docker_socket = tmp_path / "docker.sock"
    docker_socket.touch()
    state_path = config_dir / "transient-updater" / "state.json"
    request_path = requests_dir / "invalid-backup.json"
    invalid_backup = "unexpected-backup.db"
    request_path.write_text(
        json.dumps(
            {
                "request_id": "request-invalid-backup",
                "current_version": "0.1.4",
                "target_version": "0.1.5",
                "target_image": "ghcr.io/yyxiaoma/packbreaker@sha256:" + "8" * 64,
                "backup_database_file": invalid_backup,
                "backup_manifest_file": "unexpected-backup.json",
                "grace_seconds": 0.5,
            }
        ),
        encoding="utf-8",
    )
    UpdaterStateStore(state_path, helper_version="0.1.4").set(
        UpdaterStatus(
            protocol_version=UPDATER_PROTOCOL_VERSION,
            helper_version="0.1.4",
            phase="accepted",
            message="accepted",
            request_id="request-invalid-backup",
            current_version="0.1.4",
            target_version="0.1.5",
            target_image="ghcr.io/yyxiaoma/packbreaker@sha256:" + "8" * 64,
            backup_database_file=invalid_backup,
        )
    )
    monkeypatch.setenv("PACKBREAKER_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("PACKBREAKER_DOCKER_SOCKET", str(docker_socket))
    monkeypatch.setenv("PACKBREAKER_UPDATER_TARGET_CONTAINER", "packbreaker")
    monkeypatch.setenv("PACKBREAKER_UPDATER_ALLOWED_IMAGE", "ghcr.io/yyxiaoma/packbreaker")
    monkeypatch.setenv("PACKBREAKER_UPDATER_STATE_FILE", str(state_path))

    with pytest.raises(HelperRequestError) as exc_info:
        _run_oneshot(request_path)

    assert exc_info.value.code == "UPDATER_BACKUP_INVALID"
    status = UpdaterStateStore(state_path, helper_version="0.1.4").get()
    assert status.phase == "failed"
    assert status.backup_database_file == invalid_backup
    assert "UPDATER_BACKUP_INVALID" in status.message
    assert request_path.exists() is False
