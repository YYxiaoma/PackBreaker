from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from backend.app.application.cookiecloud import CookieCloudSettingView, CookieCloudSyncView


class CookieCloudSyncPort(Protocol):
    def get(self) -> CookieCloudSettingView: ...

    async def sync_now(self) -> tuple[CookieCloudSettingView, CookieCloudSyncView]: ...


@dataclass(frozen=True, slots=True)
class CookieCloudDriverState:
    running: bool
    ticks_started: int
    ticks_completed: int
    ticks_skipped: int
    consecutive_errors: int
    last_error_type: str | None
    last_tick_completed_at: datetime | None
    last_report: CookieCloudSyncView | None


class CookieCloudDriver:
    def __init__(
        self,
        service: CookieCloudSyncPort,
        *,
        interval_seconds: float,
        logger: logging.Logger | None = None,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("CookieCloud driver 周期必须大于 0")
        self._service = service
        self._interval_seconds = interval_seconds
        self._logger = logger or logging.getLogger("packbreaker.cookiecloud_driver")
        self._tick_lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        self._runner: asyncio.Task[None] | None = None
        self._ticks_started = 0
        self._ticks_completed = 0
        self._ticks_skipped = 0
        self._consecutive_errors = 0
        self._last_error_type: str | None = None
        self._last_tick_completed_at: datetime | None = None
        self._last_report: CookieCloudSyncView | None = None

    @property
    def running(self) -> bool:
        return self._runner is not None and not self._runner.done()

    @property
    def state(self) -> CookieCloudDriverState:
        return CookieCloudDriverState(
            running=self.running,
            ticks_started=self._ticks_started,
            ticks_completed=self._ticks_completed,
            ticks_skipped=self._ticks_skipped,
            consecutive_errors=self._consecutive_errors,
            last_error_type=self._last_error_type,
            last_tick_completed_at=self._last_tick_completed_at,
            last_report=self._last_report,
        )

    def start(self) -> None:
        if self.running:
            return
        self._stop_event.clear()
        self._runner = asyncio.create_task(
            self._run_loop(),
            name="packbreaker-cookiecloud-driver",
        )

    async def stop(self) -> None:
        runner = self._runner
        self._runner = None
        self._stop_event.set()
        if runner is None:
            return
        runner.cancel()
        with suppress(asyncio.CancelledError):
            await runner

    async def run_once(self, *, force: bool = False) -> CookieCloudSyncView | None:
        if self._tick_lock.locked():
            self._ticks_skipped += 1
            return None
        await self._tick_lock.acquire()
        self._ticks_started += 1
        try:
            config = await asyncio.to_thread(self._service.get)
            now = datetime.now(UTC)
            if not force and (not config.enabled or not config.auto_sync):
                self._complete_skipped()
                return None
            if (
                not force
                and config.last_sync_at is not None
                and now < config.last_sync_at + timedelta(minutes=config.sync_interval_minutes)
            ):
                self._complete_skipped()
                return None
            _record, report = await self._service.sync_now()
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

    def _complete_skipped(self) -> None:
        self._ticks_skipped += 1
        self._ticks_completed += 1
        self._consecutive_errors = 0
        self._last_error_type = None
        return None

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
                    "CookieCloud 自动同步失败 error_type=%s consecutive_errors=%s",
                    self._last_error_type,
                    self._consecutive_errors,
                )
