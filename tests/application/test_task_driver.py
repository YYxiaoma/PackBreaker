from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from backend.app.application.task_driver import ActiveTaskDriver
from backend.app.application.task_recovery import TaskRecoveryReport


def _report(*, scanned: int = 0, blocked: int = 0) -> TaskRecoveryReport:
    return TaskRecoveryReport(
        items=(),
        scanned_count=scanned,
        completed_count=0,
        waiting_count=scanned - blocked,
        blocked_count=blocked,
        truncated=False,
    )


@dataclass
class _Recovery:
    calls: int = 0
    fail_once: bool = False

    async def reconcile_once(self, *, limit: int, max_steps_per_task: int) -> TaskRecoveryReport:
        assert limit == 7
        assert max_steps_per_task == 2
        self.calls += 1
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("synthetic driver failure")
        return _report(scanned=1)


@pytest.mark.asyncio
async def test_driver_run_once_records_sanitized_state() -> None:
    recovery = _Recovery()
    driver = ActiveTaskDriver(
        recovery,
        interval_seconds=60,
        limit=7,
        max_steps_per_task=2,
    )

    report = await driver.run_once()

    assert report is not None and report.scanned_count == 1
    assert driver.state.ticks_started == 1
    assert driver.state.ticks_completed == 1
    assert driver.state.consecutive_errors == 0
    assert driver.state.last_error_type is None
    assert driver.state.last_scanned_count == 1


@pytest.mark.asyncio
async def test_driver_skips_overlapping_tick() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    class BlockingRecovery:
        calls = 0

        async def reconcile_once(
            self, *, limit: int, max_steps_per_task: int
        ) -> TaskRecoveryReport:
            self.calls += 1
            entered.set()
            await release.wait()
            return _report()

    recovery = BlockingRecovery()
    driver = ActiveTaskDriver(
        recovery,
        interval_seconds=60,
        limit=7,
        max_steps_per_task=2,
    )
    first = asyncio.create_task(driver.run_once())
    await entered.wait()

    second = await driver.run_once()
    release.set()
    await first

    assert second is None
    assert recovery.calls == 1
    assert driver.state.ticks_skipped == 1


@pytest.mark.asyncio
async def test_driver_loop_survives_unexpected_tick_error() -> None:
    recovery = _Recovery(fail_once=True)
    driver = ActiveTaskDriver(
        recovery,
        interval_seconds=0.01,
        limit=7,
        max_steps_per_task=2,
    )
    driver.start()
    try:
        for _ in range(100):
            if recovery.calls >= 2:
                break
            await asyncio.sleep(0.005)
        assert recovery.calls >= 2
        assert driver.running is True
        assert driver.state.ticks_completed >= 1
        assert driver.state.consecutive_errors == 0
    finally:
        await driver.stop()

    assert driver.running is False


@pytest.mark.asyncio
async def test_driver_stop_cancels_inflight_tick() -> None:
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    class BlockingRecovery:
        async def reconcile_once(
            self, *, limit: int, max_steps_per_task: int
        ) -> TaskRecoveryReport:
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
            raise AssertionError("blocking recovery unexpectedly resumed")

    driver = ActiveTaskDriver(
        BlockingRecovery(),
        interval_seconds=0.01,
        limit=7,
        max_steps_per_task=2,
    )
    driver.start()
    await entered.wait()

    await driver.stop()

    assert cancelled.is_set()
    assert driver.running is False
    assert driver.state.ticks_started == 1
    assert driver.state.ticks_completed == 0
