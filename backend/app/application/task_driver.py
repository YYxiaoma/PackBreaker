from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from backend.app.application.task_recovery import TaskRecoveryReport


class TaskRecoveryPort(Protocol):
    async def reconcile_once(
        self,
        *,
        limit: int,
        max_steps_per_task: int,
    ) -> TaskRecoveryReport: ...


@dataclass(frozen=True, slots=True)
class ActiveTaskDriverState:
    running: bool
    ticks_started: int
    ticks_completed: int
    ticks_skipped: int
    consecutive_errors: int
    last_error_type: str | None
    last_tick_started_at: datetime | None
    last_tick_completed_at: datetime | None
    last_scanned_count: int | None
    last_completed_count: int | None
    last_waiting_count: int | None
    last_blocked_count: int | None
    last_truncated: bool | None


class ActiveTaskDriver:
    """周期唤醒活动任务；业务推进完全委托给可恢复的 TaskRecoveryCoordinator。"""

    def __init__(
        self,
        recovery: TaskRecoveryPort,
        *,
        interval_seconds: float,
        limit: int,
        max_steps_per_task: int,
        logger: logging.Logger | None = None,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds 必须大于 0")
        if limit <= 0:
            raise ValueError("limit 必须大于 0")
        if max_steps_per_task <= 0:
            raise ValueError("max_steps_per_task 必须大于 0")
        self._recovery = recovery
        self._interval_seconds = interval_seconds
        self._limit = limit
        self._max_steps_per_task = max_steps_per_task
        self._logger = logger or logging.getLogger("packbreaker.task_driver")
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
        self._last_report: TaskRecoveryReport | None = None

    @property
    def running(self) -> bool:
        return self._runner is not None and not self._runner.done()

    @property
    def state(self) -> ActiveTaskDriverState:
        report = self._last_report
        return ActiveTaskDriverState(
            running=self.running,
            ticks_started=self._ticks_started,
            ticks_completed=self._ticks_completed,
            ticks_skipped=self._ticks_skipped,
            consecutive_errors=self._consecutive_errors,
            last_error_type=self._last_error_type,
            last_tick_started_at=self._last_tick_started_at,
            last_tick_completed_at=self._last_tick_completed_at,
            last_scanned_count=None if report is None else report.scanned_count,
            last_completed_count=None if report is None else report.completed_count,
            last_waiting_count=None if report is None else report.waiting_count,
            last_blocked_count=None if report is None else report.blocked_count,
            last_truncated=None if report is None else report.truncated,
        )

    def start(self) -> None:
        if self.running:
            return
        self._stop_event.clear()
        self._runner = asyncio.create_task(self._run_loop(), name="packbreaker-active-task-driver")

    async def stop(self) -> None:
        runner = self._runner
        self._runner = None
        self._stop_event.set()
        if runner is None:
            return
        runner.cancel()
        with suppress(asyncio.CancelledError):
            await runner

    async def run_once(self) -> TaskRecoveryReport | None:
        if self._tick_lock.locked():
            self._ticks_skipped += 1
            return None

        await self._tick_lock.acquire()
        self._ticks_started += 1
        self._last_tick_started_at = datetime.now(UTC)
        try:
            report = await self._recovery.reconcile_once(
                limit=self._limit,
                max_steps_per_task=self._max_steps_per_task,
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
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._interval_seconds,
                )
            if self._stop_event.is_set():
                return
            try:
                report = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._logger.exception(
                    "active task driver tick failed error_type=%s consecutive_errors=%s",
                    self._last_error_type,
                    self._consecutive_errors,
                )
                continue
            if report is not None and report.blocked_count:
                self._logger.warning(
                    "active task driver blocked tasks=%s scanned=%s truncated=%s",
                    report.blocked_count,
                    report.scanned_count,
                    report.truncated,
                )
