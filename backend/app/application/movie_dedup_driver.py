from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol


class MovieDedupAutomationPort(Protocol):
    def list_runnable_job_ids(self, *, limit: int = 8) -> tuple[str, ...]: ...

    def reconcile_job_operations(self, job_id: str, *, limit: int = 1) -> bool: ...

    def advance(
        self,
        job_id: str,
        *,
        scan_batch_size: int = 250,
        match_batch_size: int = 100,
        verify_batch_size: int = 2,
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class MovieDedupDriverState:
    running: bool
    ticks_started: int
    ticks_completed: int
    ticks_skipped: int
    last_error_type: str | None
    last_tick_completed_at: datetime | None


class MovieDedupDriver:
    """后台有界推进影片去重扫描/匹配/校验，不依赖浏览器连接。"""

    def __init__(
        self,
        service: MovieDedupAutomationPort,
        *,
        interval_seconds: float,
        job_limit: int,
        scan_batch_size: int,
        match_batch_size: int,
        verify_batch_size: int,
        logger: logging.Logger | None = None,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("影片去重 driver 周期必须大于 0")
        if min(job_limit, scan_batch_size, match_batch_size, verify_batch_size) <= 0:
            raise ValueError("影片去重 driver 批次参数必须大于 0")
        self._service = service
        self._interval_seconds = interval_seconds
        self._job_limit = job_limit
        self._scan_batch_size = scan_batch_size
        self._match_batch_size = match_batch_size
        self._verify_batch_size = verify_batch_size
        self._logger = logger or logging.getLogger("packbreaker.movie_dedup_driver")
        self._tick_lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        self._runner: asyncio.Task[None] | None = None
        self._ticks_started = 0
        self._ticks_completed = 0
        self._ticks_skipped = 0
        self._last_error_type: str | None = None
        self._last_tick_completed_at: datetime | None = None

    @property
    def running(self) -> bool:
        return self._runner is not None and not self._runner.done()

    @property
    def state(self) -> MovieDedupDriverState:
        return MovieDedupDriverState(
            running=self.running,
            ticks_started=self._ticks_started,
            ticks_completed=self._ticks_completed,
            ticks_skipped=self._ticks_skipped,
            last_error_type=self._last_error_type,
            last_tick_completed_at=self._last_tick_completed_at,
        )

    def start(self) -> None:
        if self.running:
            return
        self._stop_event.clear()
        self._runner = asyncio.create_task(
            self._run_loop(),
            name="packbreaker-movie-dedup-driver",
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

    async def run_once(self) -> int | None:
        if self._tick_lock.locked():
            self._ticks_skipped += 1
            return None
        await self._tick_lock.acquire()
        self._ticks_started += 1
        advanced = 0
        try:
            job_ids = await asyncio.to_thread(
                self._service.list_runnable_job_ids,
                limit=self._job_limit,
            )
            for job_id in job_ids:
                try:
                    recovered = await asyncio.to_thread(
                        self._service.reconcile_job_operations,
                        job_id,
                        limit=1,
                    )
                    if not recovered:
                        await asyncio.to_thread(
                            self._service.advance,
                            job_id,
                            scan_batch_size=self._scan_batch_size,
                            match_batch_size=self._match_batch_size,
                            verify_batch_size=self._verify_batch_size,
                        )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._last_error_type = type(exc).__name__
                    self._logger.exception(
                        "影片去重后台推进失败 job_id=%s error_type=%s",
                        job_id,
                        self._last_error_type,
                    )
                    continue
                advanced += 1
            self._ticks_completed += 1
            if advanced:
                self._last_error_type = None
            return advanced
        finally:
            self._last_tick_completed_at = datetime.now(UTC)
            self._tick_lock.release()

    async def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error_type = type(exc).__name__
                self._logger.exception(
                    "影片去重 driver tick 失败 error_type=%s",
                    self._last_error_type,
                )
            with suppress(TimeoutError):
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._interval_seconds)
