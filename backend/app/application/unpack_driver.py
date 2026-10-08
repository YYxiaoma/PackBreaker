from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol


class UnpackDiscoveryAutomationPort(Protocol):
    def list_due_execution_ids(self, *, limit: int = 20) -> tuple[str, ...]: ...

    def discover_next_page(self, execution_id: str, *, limit: int = 100) -> object: ...


class UnpackMonitorAutomationPort(Protocol):
    def list_due_definition_ids(self, *, limit: int = 20) -> tuple[str, ...]: ...

    async def trigger_due_definition(self, definition_id: str) -> object: ...


class UnpackMatchAutomationPort(Protocol):
    def list_matching_execution_ids(self, *, limit: int = 20) -> tuple[str, ...]: ...

    async def match_next_batch(self, execution_id: str, *, limit: int = 20) -> object: ...


class UnpackContentVerificationAutomationPort(Protocol):
    def list_verifiable_execution_ids(self, *, limit: int = 20) -> tuple[str, ...]: ...

    async def verify_next_batch(self, execution_id: str, *, limit: int = 5) -> object: ...


class UnpackAuxiliaryAutomationPort(Protocol):
    def list_auxiliary_execution_ids(self, *, limit: int = 20) -> tuple[str, ...]: ...

    async def advance_next_batch(self, execution_id: str, *, limit: int = 2) -> object: ...


class UnpackExecutionPlanAutomationPort(Protocol):
    def list_plannable_execution_ids(self, *, limit: int = 20) -> tuple[str, ...]: ...

    async def plan_next_batch(self, execution_id: str, *, limit: int = 5) -> object: ...


class UnpackMaterializationAutomationPort(Protocol):
    def list_materializable_execution_ids(self, *, limit: int = 20) -> tuple[str, ...]: ...

    def materialize_next_batch(self, execution_id: str, *, limit: int = 5) -> object: ...


class UnpackSeedingAutomationPort(Protocol):
    def list_seedable_execution_ids(self, *, limit: int = 20) -> tuple[str, ...]: ...

    async def advance_next_batch(self, execution_id: str, *, limit: int = 5) -> object: ...


class UnpackResultNotificationPort(Protocol):
    def project_unpack_execution_results(self, execution_ids: tuple[str, ...]) -> int: ...


@dataclass(frozen=True, slots=True)
class UnpackDriverState:
    running: bool
    ticks_started: int
    ticks_completed: int
    ticks_skipped: int
    last_error_type: str | None
    last_tick_completed_at: datetime | None


class UnpackDriver:
    """后台有界推进数据拆包媒体发现；匹配阶段由后续 coordinator 接管。"""

    def __init__(
        self,
        service: UnpackDiscoveryAutomationPort,
        monitor_service: UnpackMonitorAutomationPort | None = None,
        match_service: UnpackMatchAutomationPort | None = None,
        content_verification_service: UnpackContentVerificationAutomationPort | None = None,
        auxiliary_service: UnpackAuxiliaryAutomationPort | None = None,
        execution_plan_service: UnpackExecutionPlanAutomationPort | None = None,
        materialization_service: UnpackMaterializationAutomationPort | None = None,
        seeding_service: UnpackSeedingAutomationPort | None = None,
        result_notification_service: UnpackResultNotificationPort | None = None,
        *,
        interval_seconds: float,
        execution_limit: int,
        discovery_batch_size: int,
        match_batch_size: int = 20,
        content_verification_batch_size: int = 5,
        auxiliary_batch_size: int = 2,
        execution_plan_batch_size: int = 5,
        materialization_batch_size: int = 5,
        seeding_batch_size: int = 5,
        logger: logging.Logger | None = None,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("数据拆包 driver 周期必须大于 0")
        if (
            execution_limit <= 0
            or discovery_batch_size <= 0
            or match_batch_size <= 0
            or content_verification_batch_size <= 0
            or auxiliary_batch_size <= 0
            or execution_plan_batch_size <= 0
            or materialization_batch_size <= 0
            or seeding_batch_size <= 0
        ):
            raise ValueError("数据拆包 driver 批次参数必须大于 0")
        self._service = service
        self._monitor_service = monitor_service
        self._match_service = match_service
        self._content_verification_service = content_verification_service
        self._auxiliary_service = auxiliary_service
        self._execution_plan_service = execution_plan_service
        self._materialization_service = materialization_service
        self._seeding_service = seeding_service
        self._result_notification_service = result_notification_service
        self._interval_seconds = interval_seconds
        self._execution_limit = execution_limit
        self._discovery_batch_size = discovery_batch_size
        self._match_batch_size = match_batch_size
        self._content_verification_batch_size = content_verification_batch_size
        self._auxiliary_batch_size = auxiliary_batch_size
        self._execution_plan_batch_size = execution_plan_batch_size
        self._materialization_batch_size = materialization_batch_size
        self._seeding_batch_size = seeding_batch_size
        self._logger = logger or logging.getLogger("packbreaker.unpack_driver")
        self._tick_lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        self._runner: asyncio.Task[None] | None = None
        self._ticks_started = 0
        self._ticks_completed = 0
        self._ticks_skipped = 0
        self._last_error_type: str | None = None
        self._last_tick_completed_at: datetime | None = None

    @property
    def running(self) -> bool:
        return self._runner is not None and not self._runner.done()

    @property
    def state(self) -> UnpackDriverState:
        return UnpackDriverState(
            running=self.running,
            ticks_started=self._ticks_started,
            ticks_completed=self._ticks_completed,
            ticks_skipped=self._ticks_skipped,
            last_error_type=self._last_error_type,
            last_tick_completed_at=self._last_tick_completed_at,
        )

    def start(self) -> None:
        if self.running:
            return
        self._stop_event.clear()
        self._runner = asyncio.create_task(
            self._run_loop(),
            name="packbreaker-unpack-driver",
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

    async def run_once(self) -> int | None:
        if self._tick_lock.locked():
            self._ticks_skipped += 1
            return None
        await self._tick_lock.acquire()
        self._ticks_started += 1
        advanced = 0
        matching_execution_ids: tuple[str, ...] = ()
        verifying_execution_ids: tuple[str, ...] = ()
        auxiliary_execution_ids: tuple[str, ...] = ()
        planning_execution_ids: tuple[str, ...] = ()
        materializing_execution_ids: tuple[str, ...] = ()
        seeding_execution_ids: tuple[str, ...] = ()
        try:
            if self._monitor_service is not None:
                definition_ids = await asyncio.to_thread(
                    self._monitor_service.list_due_definition_ids,
                    limit=self._execution_limit,
                )
                for definition_id in definition_ids:
                    try:
                        await self._monitor_service.trigger_due_definition(definition_id)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        self._last_error_type = type(exc).__name__
                        self._logger.exception(
                            "监控拆包调度失败 definition_id=%s error_type=%s",
                            definition_id,
                            self._last_error_type,
                        )
                        continue
                    advanced += 1

            execution_ids = await asyncio.to_thread(
                self._service.list_due_execution_ids,
                limit=self._execution_limit,
            )
            for execution_id in execution_ids:
                try:
                    await asyncio.to_thread(
                        self._service.discover_next_page,
                        execution_id,
                        limit=self._discovery_batch_size,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._last_error_type = type(exc).__name__
                    self._logger.exception(
                        "数据拆包媒体发现后台推进失败 execution_id=%s error_type=%s",
                        execution_id,
                        self._last_error_type,
                    )
                    continue
                advanced += 1

            if self._match_service is not None:
                matching_execution_ids = await asyncio.to_thread(
                    self._match_service.list_matching_execution_ids,
                    limit=self._execution_limit,
                )
                for execution_id in matching_execution_ids:
                    try:
                        await self._match_service.match_next_batch(
                            execution_id,
                            limit=self._match_batch_size,
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        self._last_error_type = type(exc).__name__
                        self._logger.exception(
                            "数据拆包匹配后台推进失败 execution_id=%s error_type=%s",
                            execution_id,
                            self._last_error_type,
                        )
                        continue
                    advanced += 1

            if self._content_verification_service is not None:
                verifying_execution_ids = await asyncio.to_thread(
                    self._content_verification_service.list_verifiable_execution_ids,
                    limit=self._execution_limit,
                )
                for execution_id in verifying_execution_ids:
                    try:
                        await self._content_verification_service.verify_next_batch(
                            execution_id,
                            limit=self._content_verification_batch_size,
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        self._last_error_type = type(exc).__name__
                        self._logger.exception(
                            "数据拆包内容验证后台推进失败 execution_id=%s error_type=%s",
                            execution_id,
                            self._last_error_type,
                        )
                        continue
                    advanced += 1

            if self._auxiliary_service is not None:
                auxiliary_execution_ids = await asyncio.to_thread(
                    self._auxiliary_service.list_auxiliary_execution_ids,
                    limit=self._execution_limit,
                )
                for execution_id in auxiliary_execution_ids:
                    try:
                        await self._auxiliary_service.advance_next_batch(
                            execution_id,
                            limit=self._auxiliary_batch_size,
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        self._last_error_type = type(exc).__name__
                        self._logger.exception(
                            "数据拆包辅助文件补齐后台推进失败 execution_id=%s error_type=%s",
                            execution_id,
                            self._last_error_type,
                        )
                        continue
                    advanced += 1

            if self._execution_plan_service is not None:
                planning_execution_ids = await asyncio.to_thread(
                    self._execution_plan_service.list_plannable_execution_ids,
                    limit=self._execution_limit,
                )
                for execution_id in planning_execution_ids:
                    try:
                        await self._execution_plan_service.plan_next_batch(
                            execution_id,
                            limit=self._execution_plan_batch_size,
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        self._last_error_type = type(exc).__name__
                        self._logger.exception(
                            "数据拆包执行计划后台推进失败 execution_id=%s error_type=%s",
                            execution_id,
                            self._last_error_type,
                        )
                        continue
                    advanced += 1

            if self._materialization_service is not None:
                materializing_execution_ids = await asyncio.to_thread(
                    self._materialization_service.list_materializable_execution_ids,
                    limit=self._execution_limit,
                )
                for execution_id in materializing_execution_ids:
                    try:
                        await asyncio.to_thread(
                            self._materialization_service.materialize_next_batch,
                            execution_id,
                            limit=self._materialization_batch_size,
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        self._last_error_type = type(exc).__name__
                        self._logger.exception(
                            "数据拆包文件落位后台推进失败 execution_id=%s error_type=%s",
                            execution_id,
                            self._last_error_type,
                        )
                        continue
                    advanced += 1

            if self._seeding_service is not None:
                seeding_execution_ids = await asyncio.to_thread(
                    self._seeding_service.list_seedable_execution_ids,
                    limit=self._execution_limit,
                )
                for execution_id in seeding_execution_ids:
                    try:
                        await self._seeding_service.advance_next_batch(
                            execution_id,
                            limit=self._seeding_batch_size,
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        self._last_error_type = type(exc).__name__
                        self._logger.exception(
                            "数据拆包最终辅种后台推进失败 execution_id=%s error_type=%s",
                            execution_id,
                            self._last_error_type,
                        )
                        continue
                    advanced += 1
            touched_execution_ids = tuple(
                sorted(
                    set(execution_ids)
                    | set(matching_execution_ids)
                    | set(verifying_execution_ids)
                    | set(auxiliary_execution_ids)
                    | set(planning_execution_ids)
                    | set(materializing_execution_ids)
                    | set(seeding_execution_ids)
                )
            )
            if self._result_notification_service is not None and touched_execution_ids:
                try:
                    await asyncio.to_thread(
                        self._result_notification_service.project_unpack_execution_results,
                        touched_execution_ids,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._last_error_type = type(exc).__name__
                    self._logger.exception(
                        "数据拆包终态通知投影失败 execution_count=%s error_type=%s",
                        len(touched_execution_ids),
                        self._last_error_type,
                    )
            self._ticks_completed += 1
            if advanced:
                self._last_error_type = None
            return advanced
        finally:
            self._last_tick_completed_at = datetime.now(UTC)
            self._tick_lock.release()

    async def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error_type = type(exc).__name__
                self._logger.exception(
                    "数据拆包 driver tick 失败 error_type=%s",
                    self._last_error_type,
                )
            with suppress(TimeoutError):
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._interval_seconds,
                )
