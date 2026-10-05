from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from backend.app.application.cookiecloud import CookieCloudSettingView, CookieCloudSyncView
from backend.app.application.cookiecloud_driver import CookieCloudDriver
from backend.app.domain.cookiecloud import (
    CookieCloudConnectionStatus,
    CookieCloudCryptoType,
    CookieCloudSyncStatus,
)


def _setting(
    *,
    enabled: bool = True,
    auto_sync: bool = True,
    last_sync_at: datetime | None = None,
    updated_at: datetime | None = None,
    cron: str = "*/30 * * * *",
) -> CookieCloudSettingView:
    now = datetime.now(UTC)
    return CookieCloudSettingView(
        enabled=enabled,
        server_url="https://cookie.example.test",
        uuid="synthetic",
        password_configured=True,
        auto_sync=auto_sync,
        sync_cron_expression=cron,
        request_timeout_seconds=15,
        connection_status=CookieCloudConnectionStatus.OK,
        last_test_at=None,
        last_sync_at=last_sync_at,
        last_sync_status=CookieCloudSyncStatus.NEVER,
        last_sync_error_code=None,
        source_domains=0,
        source_cookies=0,
        eligible_sites=0,
        matched_sites=0,
        updated_sites=0,
        unchanged_sites=0,
        unmatched_domains=0,
        version=1,
        created_at=now - timedelta(hours=2),
        updated_at=updated_at or now - timedelta(hours=2),
    )


class _FakeService:
    def __init__(self, setting: CookieCloudSettingView) -> None:
        self.setting = setting
        self.sync_calls = 0

    def get(self) -> CookieCloudSettingView:
        return self.setting

    async def sync_now(self) -> tuple[CookieCloudSettingView, CookieCloudSyncView]:
        self.sync_calls += 1
        now = datetime.now(UTC)
        report = CookieCloudSyncView(
            synced_at=now,
            crypto_type=CookieCloudCryptoType.AES_128_CBC_FIXED,
            source_domains=3,
            source_cookies=4,
            eligible_sites=2,
            matched_sites=2,
            updated_sites=1,
            unchanged_sites=1,
            unmatched_domains=1,
            update_time=None,
        )
        return self.setting, report


@pytest.mark.asyncio
async def test_cookiecloud_driver_skips_disabled_or_auto_sync_off() -> None:
    for setting in (_setting(enabled=False), _setting(auto_sync=False)):
        service = _FakeService(setting)
        driver = CookieCloudDriver(service, interval_seconds=60, timezone="Asia/Shanghai")
        assert await driver.run_once() is None
        assert service.sync_calls == 0
        assert driver.state.ticks_skipped == 1
        assert driver.state.consecutive_errors == 0


@pytest.mark.asyncio
async def test_cookiecloud_driver_runs_when_cron_is_due_and_skips_before_next_hit() -> None:
    service = _FakeService(_setting(cron="* * * * *"))
    driver = CookieCloudDriver(service, interval_seconds=60, timezone="Asia/Shanghai")
    report = await driver.run_once()
    assert report is not None and report.updated_sites == 1
    assert service.sync_calls == 1
    assert driver.state.ticks_completed == 1

    service.setting = _setting(
        last_sync_at=datetime.now(UTC),
        updated_at=datetime.now(UTC) - timedelta(hours=1),
        cron="*/30 * * * *",
    )
    assert await driver.run_once() is None
    assert service.sync_calls == 1
    assert driver.state.ticks_skipped == 1


@pytest.mark.asyncio
async def test_cookiecloud_driver_force_bypasses_schedule() -> None:
    service = _FakeService(_setting(enabled=False, auto_sync=False, last_sync_at=datetime.now(UTC)))
    driver = CookieCloudDriver(service, interval_seconds=60, timezone="Asia/Shanghai")
    report = await driver.run_once(force=True)
    assert report is not None
    assert service.sync_calls == 1
