"""做种健康巡查的后台调度器。

与 ``SiteStatisticsDriver`` 同一范式：单一 tick 锁、start/stop 生命周期、
可被手动触发一次。区别是它按固定间隔运行，而不是每天固定时刻。
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Protocol

from backend.app.application.errors import ApplicationError
from backend.app.domain.seeding_health import SeedingRunTrigger

_DEFAULT_STARTUP_DELAY_SECONDS = 60.0


class SeedingHealthRunner(Protocol):
    def start_run(self, trigger: SeedingRunTrigger) -> str: ...

    async def run_once(self, trigger: SeedingRunTrigger, *, run_id: str | None = None) -> str: ...


class SeedingHealthDriver:
    def __init__(
        self,
        service: SeedingHealthRunner,
        *,
        interval_hours: int,
        enabled: bool = True,
        startup_delay_seconds: float = _DEFAULT_STARTUP_DELAY_SECONDS,
        logger: logging.Logger | None = None,
    ) -> None:
        self._service = service
        self._interval = timedelta(hours=max(1, interval_hours))
        self._enabled = enabled
        self._startup_delay = max(0.0, startup_delay_seconds)
        self._logger = logger or logging.getLogger("packbreaker.seeding_health_driver")
        self._tick_lock = asyncio.Lock()
        self._busy = False
        self._stop_event = asyncio.Event()
        self._runner: asyncio.Task[None] | None = None
        self._manual_tasks: set[asyncio.Task[None]] = set()
        self._last_started_at: datetime | None = None

    @property
    def running(self) -> bool:
        return self._runner is not None and not self._runner.done()

    @property
    def busy(self) -> bool:
        return self._busy or self._tick_lock.locked()

    def next_run_at(self, after: datetime | None = None) -> datetime | None:
        if not self._enabled:
            return None
        base = self._last_started_at
        if base is None:
            return (after or datetime.now(UTC)) + timedelta(seconds=self._startup_delay)
        return base + self._interval

    def start(self) -> None:
        if not self._enabled or self.running:
            return
        self._logger.info(
            "做种健康巡查已启用 interval_hours=%.0f startup_delay_seconds=%.0f",
            self._interval.total_seconds() / 3600,
            self._startup_delay,
        )
        self._stop_event.clear()
        self._runner = asyncio.create_task(
            self._run_loop(), name="packbreaker-seeding-health-driver"
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
        for task in tuple(self._manual_tasks):
            task.cancel()
        if self._manual_tasks:
            with suppress(asyncio.CancelledError):
                await asyncio.gather(*tuple(self._manual_tasks))

    def start_once(self, trigger: SeedingRunTrigger) -> str:
        """立即创建 run 并后台执行；已有巡查在跑时抛稳定 409。"""
        if self.busy:
            raise ApplicationError(
                code="SEEDING_HEALTH_RUN_ACTIVE",
                status=409,
                title="已有巡查正在执行",
                detail="请等待当前巡查结束后再触发",
            )
        self._busy = True
        self._last_started_at = datetime.now(UTC)
        run_id = self._service.start_run(trigger)
        task = asyncio.create_task(self._execute_once(run_id, trigger))
        self._manual_tasks.add(task)
        task.add_done_callback(self._manual_tasks.discard)
        return run_id

    async def _execute_once(self, run_id: str, trigger: SeedingRunTrigger) -> None:
        try:
            await self._service.run_once(trigger, run_id=run_id)
            self._logger.info("做种健康巡查完成 run_id=%s trigger=%s", run_id, trigger.value)
        finally:
            self._busy = False

    async def run_once(self, trigger: SeedingRunTrigger) -> str | None:
        """定时路径：同步等一轮完成，忙时返回 None。"""
        if self.busy:
            return None
        self._busy = True
        self._last_started_at = datetime.now(UTC)
        try:
            return await self._service.run_once(trigger)
        finally:
            self._busy = False

    async def _run_loop(self) -> None:
        delay = self._startup_delay
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
                return
            except TimeoutError:
                pass
            if self._stop_event.is_set():
                return
            try:
                await self.run_once(SeedingRunTrigger.SCHEDULED)
            except asyncio.CancelledError:
                raise
            except Exception:
                self._logger.exception("做种健康巡查调度失败")
            delay = self._interval.total_seconds()
