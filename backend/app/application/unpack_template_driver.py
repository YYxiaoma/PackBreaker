from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from backend.app.application.sites import SiteService
from backend.app.application.unpack_templates import UnpackTemplateService


class UnpackTemplateImportDriver:
    """在后台逐个获取种子详情，支持进程重启后继续未完成的导入。"""

    def __init__(
        self,
        service: UnpackTemplateService,
        site_service: SiteService,
        *,
        idle_interval_seconds: float = 1.0,
        max_failure_backoff_seconds: float = 30.0,
        logger: logging.Logger | None = None,
    ) -> None:
        if idle_interval_seconds <= 0:
            raise ValueError("模板导入驱动空闲周期必须大于 0")
        if max_failure_backoff_seconds < idle_interval_seconds:
            raise ValueError("模板导入驱动失败退避上限不能小于空闲周期")
        self._service = service
        self._site_service = site_service
        self._idle_interval_seconds = idle_interval_seconds
        self._max_failure_backoff_seconds = max_failure_backoff_seconds
        self._logger = logger or logging.getLogger("packbreaker.unpack.templates")
        self._stopping = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name="unpack-template-import-driver")

    async def stop(self) -> None:
        self._stopping.set()
        task = self._task
        if task is None:
            return
        with suppress(asyncio.CancelledError):
            await task
        self._task = None

    async def _run(self) -> None:
        failure_streak = 0
        while not self._stopping.is_set():
            try:
                progressed = await self._service.process_next_import_item(self._site_service)
                failure_streak = 0
            except asyncio.CancelledError:
                raise
            except Exception:
                failure_streak += 1
                # 数据库被长事务占用（database is locked）这类瞬时故障会连续出现，
                # 每轮都打完整堆栈没有意义：首轮与前几次记录，之后按退避节奏抽样记录。
                if failure_streak == 1 or failure_streak % 5 == 0:
                    self._logger.exception(
                        "模板种子元数据导入驱动发生内部错误（连续第 %s 次，将退避重试）",
                        failure_streak,
                    )
                else:
                    self._logger.debug(
                        "模板种子元数据导入驱动仍在失败重试（第 %s 次）", failure_streak
                    )
                progressed = False
            if progressed:
                continue
            delay = self._idle_interval_seconds
            if failure_streak > 0:
                # 指数退避 2s / 4s / 8s … 上限 30s，避免锁竞争时每秒刷日志。
                delay = min(
                    self._max_failure_backoff_seconds,
                    self._idle_interval_seconds * float(2 ** min(failure_streak, 6)),
                )
            with suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=delay)
