from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from backend.app.application.errors import ApplicationError
from backend.app.application.history_scans import HistoryScanBatchResult, HistoryScanView


class HistoryScanPort(Protocol):
    def list_scanning(self, *, limit: int) -> tuple[HistoryScanView, ...]: ...

    def scan_batch(
        self,
        scan_id: str,
        *,
        expected_version: int,
        limit: int,
    ) -> HistoryScanBatchResult: ...


@dataclass(frozen=True, slots=True)
class HistoryScanDriverReport:
    inspected_count: int
    advanced_count: int
    processed_file_count: int
    completed_count: int
    raced_count: int
    failed_count: int
    truncated: bool


@dataclass(frozen=True, slots=True)
class HistoryScanDriverState:
    running: bool
    ticks_started: int
    ticks_completed: int
    ticks_skipped: int
    consecutive_errors: int
    last_error_type: str | None
    last_tick_started_at: datetime | None
    last_tick_completed_at: datetime | None
    last_inspected_count: int | None
    last_advanced_count: int | None
    last_processed_file_count: int | None
    last_completed_count: int | None
    last_raced_count: int | None
    last_failed_count: int | None
    last_truncated: bool | None


class HistoryScanDriver:
    """只推进显式处于 SCANNING 的历史扫描；不 materialize，也不触发任务执行。"""

    def __init__(
        self,
        scans: HistoryScanPort,
        *,
        interval_seconds: float,
        scan_limit: int,
        batch_size: int,
        logger: logging.Logger | None = None,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds 必须大于 0")
        if scan_limit <= 0:
            raise ValueError("scan_limit 必须大于 0")
        if batch_size <= 0:
            raise ValueError("batch_size 必须大于 0")
        self._scans = scans
        self._interval_seconds = interval_seconds
        self._scan_limit = scan_limit
        self._batch_size = batch_size
        self._logger = logger or logging.getLogger("packbreaker.history_scan_driver")
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
        self._last_report: HistoryScanDriverReport | None = None

    @property
    def running(self) -> bool:
        return self._runner is not None and not self._runner.done()

    @property
    def state(self) -> HistoryScanDriverState:
        report = self._last_report
        return HistoryScanDriverState(
            running=self.running,
            ticks_started=self._ticks_started,
            ticks_completed=self._ticks_completed,
            ticks_skipped=self._ticks_skipped,
            consecutive_errors=self._consecutive_errors,
            last_error_type=self._last_error_type,
            last_tick_started_at=self._last_tick_started_at,
            last_tick_completed_at=self._last_tick_completed_at,
            last_inspected_count=None if report is None else report.inspected_count,
            last_advanced_count=None if report is None else report.advanced_count,
            last_processed_file_count=None if report is None else report.processed_file_count,
            last_completed_count=None if report is None else report.completed_count,
            last_raced_count=None if report is None else report.raced_count,
            last_failed_count=None if report is None else report.failed_count,
            last_truncated=None if report is None else report.truncated,
        )

    def start(self) -> None:
        if self.running:
            return
        self._stop_event.clear()
        self._runner = asyncio.create_task(
            self._run_loop(),
            name="packbreaker-history-scan-driver",
        )

    async def stop(self) -> None:
        runner = self._runner
        self._runner = None
        self._stop_event.set()
        if runner is None:
            return
        with suppress(asyncio.CancelledError):
            await runner

    async def run_once(self) -> HistoryScanDriverReport | None:
        if self._tick_lock.locked():
            self._ticks_skipped += 1
            return None
        await self._tick_lock.acquire()
        self._ticks_started += 1
        self._last_tick_started_at = datetime.now(UTC)
        try:
            scans = await asyncio.to_thread(
                self._scans.list_scanning,
                limit=self._scan_limit + 1,
            )
            truncated = len(scans) > self._scan_limit
            inspected = scans[: self._scan_limit]
            advanced_count = 0
            processed_file_count = 0
            completed_count = 0
            raced_count = 0
            failed_count = 0
            for scan in inspected:
                try:
                    result = await asyncio.to_thread(
                        self._scans.scan_batch,
                        scan.id,
                        expected_version=scan.version,
                        limit=self._batch_size,
                    )
                except ApplicationError as exc:
                    if exc.code in {
                        "HISTORY_SCAN_VERSION_CONFLICT",
                        "HISTORY_SCAN_STATE_INVALID",
                    }:
                        raced_count += 1
                        continue
                    failed_count += 1
                    self._logger.warning(
                        "历史扫描任务推进失败 scan_id=%s code=%s",
                        scan.id,
                        exc.code,
                    )
                    continue
                advanced_count += 1
                processed_file_count += result.processed_count
                if not result.has_more:
                    completed_count += 1
            report = HistoryScanDriverReport(
                inspected_count=len(inspected),
                advanced_count=advanced_count,
                processed_file_count=processed_file_count,
                completed_count=completed_count,
                raced_count=raced_count,
                failed_count=failed_count,
                truncated=truncated,
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
                    "历史扫描驱动执行失败 error_type=%s consecutive_errors=%s",
                    self._last_error_type,
                    self._consecutive_errors,
                )
