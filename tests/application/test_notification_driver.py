from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from backend.app.application.notification_driver import NotificationDriver
from backend.app.application.notifications import NotificationDeliveryReport


@dataclass
class _Delivery:
    calls: int = 0

    async def deliver_due_once(
        self, *, limit: int, max_attempts: int
    ) -> NotificationDeliveryReport:
        assert limit == 9
        assert max_attempts == 3
        self.calls += 1
        return NotificationDeliveryReport(1, 1, 0, 0)


@pytest.mark.asyncio
async def test_notification_driver_records_only_sanitized_counts() -> None:
    delivery = _Delivery()
    driver = NotificationDriver(delivery, interval_seconds=60, limit=9, max_attempts=3)

    report = await driver.run_once()

    assert report is not None and report.delivered_count == 1
    assert driver.state.ticks_started == 1
    assert driver.state.ticks_completed == 1
    assert driver.state.last_delivered_count == 1
    assert driver.state.last_error_type is None


@pytest.mark.asyncio
async def test_notification_driver_skips_overlapping_tick() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    class BlockingDelivery:
        calls = 0

        async def deliver_due_once(
            self, *, limit: int, max_attempts: int
        ) -> NotificationDeliveryReport:
            self.calls += 1
            entered.set()
            await release.wait()
            return NotificationDeliveryReport(0, 0, 0, 0)

    delivery = BlockingDelivery()
    driver = NotificationDriver(delivery, interval_seconds=60, limit=9, max_attempts=3)
    first = asyncio.create_task(driver.run_once())
    await entered.wait()

    second = await driver.run_once()
    release.set()
    await first

    assert second is None
    assert delivery.calls == 1
