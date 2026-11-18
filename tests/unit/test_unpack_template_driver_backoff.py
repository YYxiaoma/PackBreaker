from __future__ import annotations

import asyncio
import time
from typing import Any, cast

import pytest

from backend.app.application.unpack_template_driver import UnpackTemplateImportDriver
from backend.app.application.unpack_templates import UnpackTemplateService


class _FailingService:
    """每次都抛数据库层错误的假服务，模拟 database is locked 连续失败。"""

    def __init__(self) -> None:
        self.calls: list[float] = []

    async def process_next_import_item(self, _site_service: object) -> bool:
        self.calls.append(time.monotonic())
        raise RuntimeError("database is locked")


def test_driver_backs_off_after_repeated_failures() -> None:
    service = _FailingService()
    driver = UnpackTemplateImportDriver(
        cast(UnpackTemplateService, service),
        cast(Any, None),
        idle_interval_seconds=0.01,
        max_failure_backoff_seconds=0.08,
    )

    async def scenario() -> None:
        driver.start()
        await asyncio.sleep(0.36)
        await driver.stop()

    asyncio.run(scenario())

    # 无退避时 0.36s 会重试约 36 次；指数退避后只会有个位数次数。
    assert 3 <= len(service.calls) <= 8, service.calls


def test_driver_rejects_backoff_smaller_than_idle_interval() -> None:
    with pytest.raises(ValueError):
        UnpackTemplateImportDriver(
            cast(UnpackTemplateService, _FailingService()),
            cast(Any, None),
            idle_interval_seconds=1.0,
            max_failure_backoff_seconds=0.5,
        )
