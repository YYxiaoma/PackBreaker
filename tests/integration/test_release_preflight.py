from __future__ import annotations

import os
from pathlib import Path

from backend.app.config import AppSettings
from backend.app.infrastructure.release_preflight import run_release_preflight
from backend.app.infrastructure.runtime import RuntimeManager


def _settings(tmp_path: Path) -> AppSettings:
    return AppSettings(
        config_dir=(tmp_path / "config").resolve(),
        data_dir=(tmp_path / "data").resolve(),
        secret_key_file=None,
    )


def test_release_preflight_passes_without_touching_media_tree(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.data_dir.mkdir(parents=True)
    media = settings.data_dir / "do-not-read-or-change.mkv"
    media.write_bytes(b"synthetic-media-canary")
    before = media.stat()
    runtime = RuntimeManager(settings)
    runtime.start()
    runtime.stop()

    report = run_release_preflight(settings, app_version="0.1.0-test")

    assert report.ready is True
    assert {check.code for check in report.checks} >= {
        "CONFIG_DIR_OK",
        "DATABASE_OK",
        "SECRET_KEY_OK",
        "DATA_ROOT_OK",
        "BACKUP_EXERCISE_OK",
    }
    after = media.stat()
    assert media.read_bytes() == b"synthetic-media-canary"
    assert (after.st_ino, after.st_size, after.st_mtime_ns) == (
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )
    assert not (settings.config_dir / "backups" / "preflight").exists()


def test_release_preflight_does_not_create_missing_secret(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.config_dir.mkdir(parents=True, mode=0o700)
    settings.data_dir.mkdir(parents=True)

    report = run_release_preflight(
        settings,
        app_version="0.1.0-test",
        exercise_backup=False,
    )

    assert report.ready is False
    assert any(check.code == "SECRET_KEY_UNSAFE" for check in report.checks)
    assert not settings.resolved_secret_key_file.exists()


def test_release_preflight_blocks_overbroad_secret_permissions(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.data_dir.mkdir(parents=True)
    runtime = RuntimeManager(settings)
    runtime.start()
    runtime.stop()
    os.chmod(settings.resolved_secret_key_file, 0o644)

    report = run_release_preflight(
        settings,
        app_version="0.1.0-test",
        exercise_backup=False,
    )

    assert report.ready is False
    assert any(check.code == "SECRET_KEY_UNSAFE" for check in report.checks)
