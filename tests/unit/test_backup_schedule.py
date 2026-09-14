from __future__ import annotations

import asyncio
import threading
from datetime import datetime
from pathlib import Path

import pytest

import backend.app.application.backup_schedule as backup_schedule_module
from backend.app.application.backup_schedule import BackupDriver, BackupScheduleService
from backend.app.config import AppSettings
from backend.app.infrastructure.backups import BackupArtifact, create_consistent_backup
from backend.app.infrastructure.runtime import RuntimeManager


def _runtime(tmp_path: Path) -> tuple[AppSettings, RuntimeManager]:
    config = (tmp_path / "config").resolve()
    data = (tmp_path / "data").resolve()
    config.mkdir(mode=0o700)
    data.mkdir()
    settings = AppSettings(config_dir=config, data_dir=data)
    runtime = RuntimeManager(settings)
    runtime.start()
    return settings, runtime


@pytest.mark.asyncio
async def test_backup_driver_is_disabled_by_default_then_respects_due_window(
    tmp_path: Path,
) -> None:
    settings, runtime = _runtime(tmp_path)
    try:
        policy = BackupScheduleService(runtime.session_factory)
        driver = BackupDriver(policy, settings=settings, interval_seconds=60)

        disabled = await driver.run_once()
        assert disabled is not None
        assert disabled.created is False
        assert disabled.skipped_reason == "DISABLED"
        assert not (settings.config_dir / "backups").exists()

        current = policy.get()
        updated = policy.update(
            expected_version=current.version,
            enabled=True,
            interval_hours=24,
            retention_days=30,
            keep_latest=3,
        )
        assert updated.version == 2

        created = await driver.run_once()
        assert created is not None and created.created is True
        assert created.database_file is not None
        assert policy.get().version == 2

        not_due = await driver.run_once()
        assert not_due is not None
        assert not_due.created is False
        assert not_due.skipped_reason == "NOT_DUE"

        forced = await driver.run_once(force=True)
        assert forced is not None and forced.created is True
        assert len(list((settings.config_dir / "backups").glob("packbreaker-*.db"))) == 2
    finally:
        runtime.stop()


@pytest.mark.asyncio
async def test_backup_driver_rejects_overlapping_manual_and_scheduled_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, runtime = _runtime(tmp_path)
    original = create_consistent_backup
    entered = threading.Event()
    release = threading.Event()

    def slow_backup(
        database_path: Path,
        backup_dir: Path,
        *,
        app_version: str,
        created_at: datetime | None = None,
    ) -> BackupArtifact:
        entered.set()
        assert release.wait(timeout=5)
        return original(
            database_path,
            backup_dir,
            app_version=app_version,
            created_at=created_at,
        )

    monkeypatch.setattr(backup_schedule_module, "create_consistent_backup", slow_backup)
    try:
        policy = BackupScheduleService(runtime.session_factory)
        driver = BackupDriver(policy, settings=settings, interval_seconds=60)
        first = asyncio.create_task(driver.run_once(force=True))
        for _ in range(100):
            if entered.is_set():
                break
            await asyncio.sleep(0.01)
        assert entered.is_set()

        overlapping = await driver.run_once(force=True)
        assert overlapping is None
        assert driver.state.ticks_skipped == 1

        release.set()
        completed = await first
        assert completed is not None and completed.created is True
        assert len(list((settings.config_dir / "backups").glob("packbreaker-*.db"))) == 1
    finally:
        release.set()
        runtime.stop()


@pytest.mark.asyncio
async def test_backup_driver_records_policy_read_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, runtime = _runtime(tmp_path)
    policy = BackupScheduleService(runtime.session_factory)
    driver = BackupDriver(policy, settings=settings, interval_seconds=60)

    def fail_policy_read() -> None:
        raise RuntimeError("synthetic policy read failure")

    monkeypatch.setattr(policy, "get", fail_policy_read)
    try:
        with pytest.raises(RuntimeError, match="synthetic policy read failure"):
            await driver.run_once()
        assert driver.state.consecutive_errors == 1
        assert driver.state.last_error_type == "RuntimeError"
        assert driver.state.last_tick_completed_at is not None
    finally:
        runtime.stop()
