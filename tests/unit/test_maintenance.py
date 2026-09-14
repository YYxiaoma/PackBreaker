from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.app.config import AppSettings
from backend.app.infrastructure.backups import verify_backup
from backend.app.infrastructure.runtime import RuntimeManager
from backend.app.maintenance import main


def test_backup_command_creates_verifiable_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_dir = (tmp_path / "config").resolve()
    data_dir = (tmp_path / "data").resolve()
    settings = AppSettings(config_dir=config_dir, data_dir=data_dir, secret_key_file=None)
    runtime = RuntimeManager(settings)
    runtime.start()
    runtime.stop()
    monkeypatch.setenv("PACKBREAKER_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("PACKBREAKER_DATA_DIR", str(data_dir))

    assert main(["backup"]) == 0
    payload = json.loads(capsys.readouterr().out)
    backup_dir = Path(payload["backup_dir"])
    database_path = backup_dir / payload["database_file"]
    manifest_path = backup_dir / payload["manifest_file"]

    assert payload["status"] == "ok"
    assert database_path.is_file()
    assert manifest_path.is_file()
    assert verify_backup(database_path, manifest_path).database_sha256 == payload["database_sha256"]


def test_verify_backup_command_reports_error_for_missing_file(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing = tmp_path / "missing.db"
    assert main(["verify-backup", str(missing)]) == 2
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["status"] == "error"
    assert "不存在" in payload["error"]


def test_restore_command_requires_explicit_confirmation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing = tmp_path / "missing.db"
    assert main(["restore-backup", str(missing)]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "error"
    assert "confirm-replace-current-database" in payload["error"]


def test_backup_retention_apply_requires_explicit_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_dir = (tmp_path / "config").resolve()
    data_dir = (tmp_path / "data").resolve()
    monkeypatch.setenv("PACKBREAKER_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("PACKBREAKER_DATA_DIR", str(data_dir))

    assert main(["backup-retention", "--apply"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "error"
    assert "confirm-delete-expired-backups" in payload["error"]


def test_preflight_command_reports_blocked_without_creating_runtime_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_dir = (tmp_path / "config").resolve()
    data_dir = (tmp_path / "data").resolve()
    config_dir.mkdir(mode=0o700)
    data_dir.mkdir()
    monkeypatch.setenv("PACKBREAKER_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("PACKBREAKER_DATA_DIR", str(data_dir))

    assert main(["preflight", "--skip-backup-exercise"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "blocked"
    assert not (config_dir / "secret.key").exists()
    assert not (config_dir / "packbreaker.db").exists()


def test_backup_retention_preview_does_not_create_backup_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_dir = (tmp_path / "config").resolve()
    data_dir = (tmp_path / "data").resolve()
    config_dir.mkdir(mode=0o700)
    data_dir.mkdir()
    monkeypatch.setenv("PACKBREAKER_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("PACKBREAKER_DATA_DIR", str(data_dir))

    assert main(["backup-retention"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert payload["mode"] == "preview"
    assert payload["delete_count"] == 0
    assert not (config_dir / "backups").exists()


def test_preflight_command_reports_ready_for_initialized_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_dir = (tmp_path / "config").resolve()
    data_dir = (tmp_path / "data").resolve()
    data_dir.mkdir()
    settings = AppSettings(config_dir=config_dir, data_dir=data_dir, secret_key_file=None)
    runtime = RuntimeManager(settings)
    runtime.start()
    runtime.stop()
    monkeypatch.setenv("PACKBREAKER_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("PACKBREAKER_DATA_DIR", str(data_dir))

    assert main(["preflight"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ready"
    assert any(check["code"] == "BACKUP_EXERCISE_OK" for check in payload["checks"])
