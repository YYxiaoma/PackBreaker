from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.errors import ApplicationError
from backend.app.config import AppSettings
from backend.app.infrastructure.backups import (
    BackupArtifact,
    BackupError,
    apply_backup_retention,
    create_consistent_backup,
)
from backend.app.infrastructure.persistence.models import BackupPolicy
from backend.app.versioning import app_version


@dataclass(frozen=True, slots=True)
class BackupPolicyView:
    enabled: bool
    interval_hours: int
    retention_days: int
    keep_latest: int
    version: int
    last_attempt_at: datetime | None
    last_success_at: datetime | None
    last_error_code: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class BackupRunReport:
    created: bool
    skipped_reason: Literal["DISABLED", "NOT_DUE"] | None
    created_at: datetime | None
    database_file: str | None
    database_size_bytes: int | None
    retention_deleted_count: int
    retention_blocked_count: int
    retention_error_code: str | None


@dataclass(frozen=True, slots=True)
class BackupDriverState:
    running: bool
    ticks_started: int
    ticks_completed: int
    ticks_skipped: int
    consecutive_errors: int
    last_error_type: str | None
    last_tick_started_at: datetime | None
    last_tick_completed_at: datetime | None
    last_report: BackupRunReport | None


class BackupScheduleService:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def get(self) -> BackupPolicyView:
        with self._session_factory() as session:
            record = self._get_or_create(session)
            session.commit()
            session.refresh(record)
            return self._view(record)

    def update(
        self,
        *,
        expected_version: int,
        enabled: bool,
        interval_hours: int,
        retention_days: int,
        keep_latest: int,
    ) -> BackupPolicyView:
        self._validate(interval_hours, retention_days, keep_latest)
        with self._session_factory() as session:
            record = self._get_or_create(session)
            if record.version != expected_version:
                raise ApplicationError(
                    code="BACKUP_POLICY_VERSION_CONFLICT",
                    status=412,
                    title="备份策略版本冲突",
                    detail="备份策略已被其他请求修改，请刷新后重试",
                )
            record.enabled = enabled
            record.interval_hours = interval_hours
            record.retention_days = retention_days
            record.keep_latest = keep_latest
            record.version += 1
            record.updated_at = datetime.now(UTC)
            session.commit()
            session.refresh(record)
            return self._view(record)

    def record_attempt(self, at: datetime) -> None:
        with self._session_factory() as session:
            record = self._get_or_create(session)
            record.last_attempt_at = at.astimezone(UTC)
            record.updated_at = datetime.now(UTC)
            session.commit()

    def record_success(self, at: datetime, *, error_code: str | None = None) -> None:
        with self._session_factory() as session:
            record = self._get_or_create(session)
            record.last_success_at = at.astimezone(UTC)
            record.last_error_code = error_code
            record.updated_at = datetime.now(UTC)
            session.commit()

    def record_failure(self, *, error_code: str) -> None:
        with self._session_factory() as session:
            record = self._get_or_create(session)
            record.last_error_code = error_code
            record.updated_at = datetime.now(UTC)
            session.commit()

    @staticmethod
    def _validate(interval_hours: int, retention_days: int, keep_latest: int) -> None:
        if not 1 <= interval_hours <= 168:
            raise ValueError("备份周期必须在 1 到 168 小时之间")
        if not 1 <= retention_days <= 3650:
            raise ValueError("备份保留天数必须在 1 到 3650 天之间")
        if not 1 <= keep_latest <= 100:
            raise ValueError("至少保留份数必须在 1 到 100 之间")

    @staticmethod
    def _get_or_create(session: Session) -> BackupPolicy:
        record = session.get(BackupPolicy, "default")
        if record is not None:
            return record
        now = datetime.now(UTC)
        record = BackupPolicy(
            id="default",
            enabled=False,
            interval_hours=24,
            retention_days=30,
            keep_latest=3,
            version=1,
            last_attempt_at=None,
            last_success_at=None,
            last_error_code=None,
            created_at=now,
            updated_at=now,
        )
        session.add(record)
        try:
            session.flush()
        except IntegrityError:
            session.rollback()
            existing = session.get(BackupPolicy, "default")
            if existing is None:
                raise
            return existing
        return record

    @staticmethod
    def _view(record: BackupPolicy) -> BackupPolicyView:
        return BackupPolicyView(
            enabled=record.enabled,
            interval_hours=record.interval_hours,
            retention_days=record.retention_days,
            keep_latest=record.keep_latest,
            version=record.version,
            last_attempt_at=record.last_attempt_at,
            last_success_at=record.last_success_at,
            last_error_code=record.last_error_code,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )


class BackupDriver:
    def __init__(
        self,
        policy_service: BackupScheduleService,
        *,
        settings: AppSettings,
        interval_seconds: float,
        logger: logging.Logger | None = None,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("备份 driver 周期必须大于 0")
        self._policy_service = policy_service
        self._settings = settings
        self._interval_seconds = interval_seconds
        self._logger = logger or logging.getLogger("packbreaker.backup_driver")
        self._tick_lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        self._runner: asyncio.Task[None] | None = None
        self._ticks_started = 0
        self._ticks_completed = 0
        self._ticks_skipped = 0
        self._consecutive_errors = 0
        self._last_error_type: str | None = None
        self._last_tick_started_at: datetime | None = None
        self._last_tick_completed_at: datetime | None = None
        self._last_report: BackupRunReport | None = None

    @property
    def running(self) -> bool:
        return self._runner is not None and not self._runner.done()

    @property
    def state(self) -> BackupDriverState:
        return BackupDriverState(
            running=self.running,
            ticks_started=self._ticks_started,
            ticks_completed=self._ticks_completed,
            ticks_skipped=self._ticks_skipped,
            consecutive_errors=self._consecutive_errors,
            last_error_type=self._last_error_type,
            last_tick_started_at=self._last_tick_started_at,
            last_tick_completed_at=self._last_tick_completed_at,
            last_report=self._last_report,
        )

    def start(self) -> None:
        if self.running:
            return
        self._stop_event.clear()
        self._runner = asyncio.create_task(self._run_loop(), name="packbreaker-backup-driver")

    async def stop(self) -> None:
        runner = self._runner
        self._runner = None
        self._stop_event.set()
        if runner is None:
            return
        runner.cancel()
        with suppress(asyncio.CancelledError):
            await runner

    async def run_once(self, *, force: bool = False) -> BackupRunReport | None:
        if self._tick_lock.locked():
            self._ticks_skipped += 1
            return None
        await self._tick_lock.acquire()
        self._ticks_started += 1
        now = datetime.now(UTC)
        self._last_tick_started_at = now
        try:
            policy = await asyncio.to_thread(self._policy_service.get)
            if not force and not policy.enabled:
                return self._complete_skipped("DISABLED")
            if (
                not force
                and policy.last_success_at is not None
                and now < policy.last_success_at + timedelta(hours=policy.interval_hours)
            ):
                return self._complete_skipped("NOT_DUE")

            await asyncio.to_thread(self._policy_service.record_attempt, now)
            try:
                artifact = await asyncio.to_thread(
                    create_consistent_backup,
                    self._settings.database_path,
                    self._settings.config_dir / "backups",
                    app_version=app_version(),
                )
            except Exception:
                await asyncio.to_thread(
                    self._policy_service.record_failure,
                    error_code="BACKUP_CREATE_FAILED",
                )
                raise

            retention_error_code: str | None = None
            deleted_count = 0
            blocked_count = 0
            try:
                retention = await asyncio.to_thread(
                    apply_backup_retention,
                    self._settings.config_dir / "backups",
                    retention_days=policy.retention_days,
                    keep_latest=policy.keep_latest,
                )
                deleted_count = len(retention.deleted_stems)
                blocked_count = retention.plan.blocked_count
                if blocked_count:
                    retention_error_code = "BACKUP_RETENTION_BLOCKED"
            except (BackupError, OSError):
                retention_error_code = "BACKUP_RETENTION_FAILED"

            await asyncio.to_thread(
                self._policy_service.record_success,
                artifact.created_at,
                error_code=retention_error_code,
            )
            report = self._success_report(
                artifact,
                deleted_count=deleted_count,
                blocked_count=blocked_count,
                retention_error_code=retention_error_code,
            )
            self._last_report = report
            self._ticks_completed += 1
            self._consecutive_errors = 0
            self._last_error_type = None
            return report
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._consecutive_errors += 1
            self._last_error_type = type(exc).__name__
            raise
        finally:
            self._last_tick_completed_at = datetime.now(UTC)
            self._tick_lock.release()

    def _complete_skipped(self, reason: Literal["DISABLED", "NOT_DUE"]) -> BackupRunReport:
        report = BackupRunReport(False, reason, None, None, None, 0, 0, None)
        self._last_report = report
        self._ticks_completed += 1
        self._consecutive_errors = 0
        self._last_error_type = None
        return report

    @staticmethod
    def _success_report(
        artifact: BackupArtifact,
        *,
        deleted_count: int,
        blocked_count: int,
        retention_error_code: str | None,
    ) -> BackupRunReport:
        return BackupRunReport(
            True,
            None,
            artifact.created_at,
            artifact.database_path.name,
            artifact.database_size_bytes,
            deleted_count,
            blocked_count,
            retention_error_code,
        )

    async def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            with suppress(TimeoutError):
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._interval_seconds)
            if self._stop_event.is_set():
                return
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._logger.exception(
                    "备份驱动执行失败 error_type=%s consecutive_errors=%s",
                    self._last_error_type,
                    self._consecutive_errors,
                )
