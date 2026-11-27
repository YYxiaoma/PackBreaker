from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any, Protocol

from backend.app.domain.task_definition import next_cron_run

_MEDIA_COVERAGE_CRON = "0 5 * * *"


class MediaCoverageDriverService(Protocol):
    def get_scope(self) -> Any | None: ...

    async def create_run(self, selection_token: str) -> str: ...


class MediaCoverageDriver:
    """Re-scan the saved local-library scope every night at 05:00 app time."""

    def __init__(
        self,
        service: MediaCoverageDriverService,
        *,
        timezone: str,
        logger: logging.Logger | None = None,
    ) -> None:
        self._service = service
        self._timezone = timezone
        self._logger = logger or logging.getLogger("packbreaker.media_coverage_driver")
        self._tick_lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        self._runner: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._runner is not None and not self._runner.done()

    def next_run_at(self, after: datetime | None = None) -> datetime:
        return next_cron_run(
            _MEDIA_COVERAGE_CRON,
            after or datetime.now(UTC),
            timezone=self._timezone,
        )

    def start(self) -> None:
        if self.running:
            return
        self._stop_event.clear()
        self._runner = asyncio.create_task(
            self._run_loop(), name="packbreaker-media-coverage-driver"
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

    async def run_once(self) -> str | None:
        if self._tick_lock.locked():
            return None
        await self._tick_lock.acquire()
        try:
            scope = await asyncio.to_thread(self._service.get_scope)
            if scope is None:
                self._logger.info("每日辅种覆盖率扫描跳过：未保存扫描目录")
                return None
            run_id = await self._service.create_run(scope.selection_token)
            self._logger.info("每日辅种覆盖率扫描已启动 run_id=%s", run_id)
            return run_id
        except Exception as exc:  # noqa: BLE001 - scheduler must stay alive
            self._logger.warning("每日辅种覆盖率扫描启动失败 error=%s", type(exc).__name__)
            return None
        finally:
            self._tick_lock.release()

    async def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            next_run = self.next_run_at()
            delay_seconds = max(0.0, (next_run - datetime.now(UTC)).total_seconds())
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=delay_seconds)
            except TimeoutError:
                if self._stop_event.is_set():
                    return
                await self.run_once()
