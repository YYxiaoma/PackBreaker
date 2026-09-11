from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from backend.app.application.notifications import NotificationDeliveryReport


class NotificationDeliveryPort(Protocol):
    async def deliver_due_once(
        self,
        *,
        limit: int,
        max_attempts: int,
    ) -> NotificationDeliveryReport: ...


@dataclass(frozen=True, slots=True)
class NotificationDriverState:
    running: bool
    ticks_started: int
    ticks_completed: int
    consecutive_errors: int
    last_error_type: str | None
    last_tick_completed_at: datetime | None
    last_scanned_count: int | None
    last_delivered_count: int | None
    last_retry_count: int | None
    last_dead_count: int | None


class NotificationDriver:
    def __init__(
        self,
        delivery: NotificationDeliveryPort,
        *,
        interval_seconds: float,
        limit: int,
        max_attempts: int,
        logger: logging.Logger | None = None,
    ) -> None:
        if interval_seconds <= 0 or limit <= 0 or max_attempts <= 0:
            raise ValueError("通知 driver 配置必须为正数")
        self._delivery = delivery
        self._interval_seconds = interval_seconds
        self._limit = limit
        self._max_attempts = max_attempts
        self._logger = logger or logging.getLogger("packbreaker.notification_driver")
        self._tick_lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        self._runner: asyncio.Task[None] | None = None
        self._ticks_started = 0
        self._ticks_completed = 0
        self._consecutive_errors = 0
        self._last_error_type: str | None = None
        self._last_tick_completed_at: datetime | None = None
        self._last_report: NotificationDeliveryReport | None = None

    @property
    def running(self) -> bool:
        return self._runner is not None and not self._runner.done()

    @property
    def state(self) -> NotificationDriverState:
        report = self._last_report
        return NotificationDriverState(
            running=self.running,
            ticks_started=self._ticks_started,
            ticks_completed=self._ticks_completed,
            consecutive_errors=self._consecutive_errors,
            last_error_type=self._last_error_type,
            last_tick_completed_at=self._last_tick_completed_at,
            last_scanned_count=None if report is None else report.scanned_count,
            last_delivered_count=None if report is None else report.delivered_count,
            last_retry_count=None if report is None else report.retry_count,
            last_dead_count=None if report is None else report.dead_count,
        )

    def start(self) -> None:
        if self.running:
            return
        self._stop_event.clear()
        self._runner = asyncio.create_task(self._run_loop(), name="packbreaker-notification-driver")

    async def stop(self) -> None:
        runner = self._runner
        self._runner = None
        self._stop_event.set()
        if runner is None:
            return
        runner.cancel()
        with suppress(asyncio.CancelledError):
            await runner

    async def run_once(self) -> NotificationDeliveryReport | None:
        if self._tick_lock.locked():
            return None
        await self._tick_lock.acquire()
        self._ticks_started += 1
        try:
            report = await self._delivery.deliver_due_once(
                limit=self._limit,
                max_attempts=self._max_attempts,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._consecutive_errors += 1
            self._last_error_type = type(exc).__name__
            raise
        else:
            self._ticks_completed += 1
            self._consecutive_errors = 0
            self._last_error_type = None
            self._last_report = report
            return report
        finally:
            self._last_tick_completed_at = datetime.now(UTC)
            self._tick_lock.release()

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
                    "notification driver tick failed error_type=%s consecutive_errors=%s",
                    self._last_error_type,
                    self._consecutive_errors,
                )
