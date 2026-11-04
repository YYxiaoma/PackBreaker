from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from backend.app.application.site_statistics_driver import SiteStatisticsDriver


@dataclass(frozen=True)
class FakeSite:
    id: str
    credential_configured: bool
    enabled: bool


class FakeSiteService:
    def __init__(self, sites: list[FakeSite], *, failures: set[str] | None = None) -> None:
        self.sites = sites
        self.failures = failures or set()
        self.synced: list[str] = []

    def list_sites(self) -> list[FakeSite]:
        return self.sites

    async def sync_user_profile(self, site_id: str) -> object:
        self.synced.append(site_id)
        if site_id in self.failures:
            raise RuntimeError("synthetic site failure")
        return object()


def test_daily_site_statistics_runs_at_0337_in_configured_timezone() -> None:
    service = FakeSiteService([])
    driver = SiteStatisticsDriver(service, timezone="Asia/Shanghai")
    after = datetime(2026, 10, 20, 3, 36, tzinfo=ZoneInfo("Asia/Shanghai"))

    assert driver.next_run_at(after) == datetime(2026, 10, 19, 19, 37, tzinfo=UTC)


@pytest.mark.asyncio
async def test_daily_sync_includes_disabled_credentialed_sites() -> None:
    service = FakeSiteService(
        [
            FakeSite("enabled-cookie-site", credential_configured=True, enabled=True),
            FakeSite("disabled-api-key-site", credential_configured=True, enabled=False),
            FakeSite("no-credential-site", credential_configured=False, enabled=False),
        ]
    )
    driver = SiteStatisticsDriver(service, timezone="Asia/Shanghai")

    report = await driver.run_once()

    assert report is not None
    assert report.eligible_sites == 2
    assert report.succeeded_sites == 2
    assert report.failed_sites == 0
    assert set(service.synced) == {"enabled-cookie-site", "disabled-api-key-site"}


@pytest.mark.asyncio
async def test_daily_sync_isolates_site_failures() -> None:
    service = FakeSiteService(
        [
            FakeSite("site-ok", credential_configured=True, enabled=True),
            FakeSite("site-fails", credential_configured=True, enabled=False),
        ],
        failures={"site-fails"},
    )
    driver = SiteStatisticsDriver(service, timezone="Asia/Shanghai")

    report = await driver.run_once()

    assert report is not None
    assert report.eligible_sites == 2
    assert report.succeeded_sites == 1
    assert report.failed_sites == 1
    assert set(service.synced) == {"site-ok", "site-fails"}
