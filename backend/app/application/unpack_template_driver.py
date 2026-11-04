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
        logger: logging.Logger | None = None,
    ) -> None:
        if idle_interval_seconds <= 0:
            raise ValueError("模板导入驱动空闲周期必须大于 0")
        self._service = service
        self._site_service = site_service
        self._idle_interval_seconds = idle_interval_seconds
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
        while not self._stopping.is_set():
            try:
                progressed = await self._service.process_next_import_item(self._site_service)
            except asyncio.CancelledError:
                raise
            except Exception:
                self._logger.exception("模板种子元数据导入驱动发生内部错误")
                progressed = False
            if progressed:
                continue
            with suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=self._idle_interval_seconds)
