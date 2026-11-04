from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from backend.app.application.errors import ApplicationError
from backend.app.domain.task_definition import next_cron_run

_SITE_STATISTICS_CRON = "37 3 * * *"
_SITE_STATISTICS_CONCURRENCY = 4


class SiteStatisticsSite(Protocol):
    @property
    def id(self) -> str: ...

    @property
    def credential_configured(self) -> bool: ...


class SiteStatisticsService(Protocol):
    def list_sites(self) -> Sequence[SiteStatisticsSite]: ...

    async def sync_user_profile(self, site_id: str) -> object: ...


@dataclass(frozen=True, slots=True)
class SiteStatisticsRunReport:
    eligible_sites: int
    succeeded_sites: int
    failed_sites: int


class SiteStatisticsDriver:
    """Refresh configured site statistics daily, regardless of enabled state."""

    def __init__(
        self,
        service: SiteStatisticsService,
        *,
        timezone: str,
        logger: logging.Logger | None = None,
    ) -> None:
        self._service = service
        self._timezone = timezone
        self._logger = logger or logging.getLogger("packbreaker.site_statistics_driver")
        self._tick_lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        self._runner: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._runner is not None and not self._runner.done()

    def next_run_at(self, after: datetime | None = None) -> datetime:
        """Return the next 03:37 run in UTC for the configured application timezone."""
        return next_cron_run(
            _SITE_STATISTICS_CRON,
            after or datetime.now(UTC),
            timezone=self._timezone,
        )

    def start(self) -> None:
        if self.running:
            return
        self._stop_event.clear()
        self._runner = asyncio.create_task(
            self._run_loop(),
            name="packbreaker-site-statistics-driver",
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

    async def run_once(self) -> SiteStatisticsRunReport | None:
        """Run one refresh pass; exposed for deterministic tests and controlled diagnostics."""
        if self._tick_lock.locked():
            return None
        await self._tick_lock.acquire()
        try:
            sites = await asyncio.to_thread(self._service.list_sites)
            # SiteView.credential_configured covers Cookie and API Key credentials.
            # Do not filter on enabled: disabled sites still need fresh statistics.
            eligible = [site for site in sites if site.credential_configured]
            semaphore = asyncio.Semaphore(_SITE_STATISTICS_CONCURRENCY)

            async def sync_site(site: SiteStatisticsSite) -> bool:
                async with semaphore:
                    try:
                        await self._service.sync_user_profile(site.id)
                        return True
                    except asyncio.CancelledError:
                        raise
                    except ApplicationError as exc:
                        self._logger.warning(
                            "每日站点统计同步失败 site_id=%s error_code=%s",
                            site.id,
                            exc.code,
                        )
                    except Exception as exc:
                        self._logger.warning(
                            "每日站点统计同步失败 site_id=%s error_type=%s",
                            site.id,
                            type(exc).__name__,
                        )
                    return False

            results = await asyncio.gather(*(sync_site(site) for site in eligible))
            report = SiteStatisticsRunReport(
                eligible_sites=len(eligible),
                succeeded_sites=sum(results),
                failed_sites=len(results) - sum(results),
            )
            self._logger.info(
                "每日站点统计同步完成 eligible=%d succeeded=%d failed=%d timezone=%s",
                report.eligible_sites,
                report.succeeded_sites,
                report.failed_sites,
                self._timezone,
            )
            return report
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
                try:
                    await self.run_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self._logger.exception("每日站点统计调度失败")
