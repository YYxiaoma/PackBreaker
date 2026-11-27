from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from backend.app.application.media_coverage_driver import MediaCoverageDriver


class _Service:
    def get_scope(self) -> object:
        return SimpleNamespace(selection_token="v1.synthetic")

    async def create_run(self, selection_token: str) -> str:
        assert selection_token == "v1.synthetic"
        return "run-1"


def test_media_coverage_driver_runs_at_five_am() -> None:
    driver = MediaCoverageDriver(_Service(), timezone="Asia/Shanghai")
    next_run = driver.next_run_at(datetime(2026, 1, 1, tzinfo=UTC))
    local = next_run.astimezone(ZoneInfo("Asia/Shanghai"))

    assert local.hour == 5
    assert local.minute == 0
    assert asyncio.run(driver.run_once()) == "run-1"
