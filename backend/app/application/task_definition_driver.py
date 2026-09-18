from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from backend.app.application.task_actions import TaskActionActor
from backend.app.application.task_definition_executions import (
    AutoRetryRequest,
    DueLifecycleAdvance,
    DueMonitorScan,
    TaskMonitorScanView,
)
from backend.app.domain.task_definition import TaskExecutionTrigger


class TaskDefinitionAutomationPort(Protocol):
    def list_due_monitor_scans(
        self,
        *,
        now: datetime,
        limit: int,
    ) -> tuple[DueMonitorScan, ...]: ...

    async def scan_monitor(
        self,
        definition_id: str,
        *,
        trigger: TaskExecutionTrigger,
        trace_id: str,
        now: datetime | None = None,
    ) -> TaskMonitorScanView: ...

    def list_due_lifecycle_advances(self, *, limit: int) -> tuple[DueLifecycleAdvance, ...]: ...

    async def advance_execution(
        self,
        definition_id: str,
        execution_id: str,
        *,
        actor: TaskActionActor,
        idempotency_key: str | None,
    ) -> object: ...

    def list_due_auto_retries(
        self,
        *,
        now: datetime,
        limit: int,
    ) -> tuple[AutoRetryRequest, ...]: ...

    async def retry_auto(
        self,
        request: AutoRetryRequest,
        *,
        trace_id: str,
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class TaskDefinitionDriverReport:
    due_scan_count: int
    materialized_scan_count: int
    baseline_count: int
    no_change_count: int
    stability_wait_count: int
    debounce_wait_count: int
    overlap_count: int
    scan_failed_count: int
    due_lifecycle_count: int
    lifecycle_advanced_count: int
    lifecycle_failed_count: int
    due_retry_count: int
    retried_count: int
    retry_failed_count: int


@dataclass(frozen=True, slots=True)
class TaskDefinitionDriverState:
    running: bool
    ticks_started: int
    ticks_completed: int
    ticks_skipped: int
    consecutive_errors: int
    last_error_type: str | None
    last_tick_started_at: datetime | None
    last_tick_completed_at: datetime | None
    last_report: TaskDefinitionDriverReport | None


class TaskDefinitionDriver:
    """周期推进 v0.1.5 监控扫描与失败对象自动重试。"""

    def __init__(
        self,
        service: TaskDefinitionAutomationPort,
        *,
        interval_seconds: float,
        scan_limit: int,
        retry_limit: int,
        lifecycle_limit: int | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds 必须大于 0")
        if scan_limit <= 0:
            raise ValueError("scan_limit 必须大于 0")
        if retry_limit <= 0:
            raise ValueError("retry_limit 必须大于 0")
        if lifecycle_limit is not None and lifecycle_limit <= 0:
            raise ValueError("lifecycle_limit 必须大于 0")
        self._service = service
        self._interval_seconds = interval_seconds
        self._scan_limit = scan_limit
        self._retry_limit = retry_limit
        self._lifecycle_limit = lifecycle_limit or retry_limit
        self._logger = logger or logging.getLogger("packbreaker.task_definition_driver")
        self._tick_lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        self._runner: asyncio.Task[None] | None = None
        self._ticks_started = 0
        self._ticks_completed = 0
        self._ticks_skipped = 0
        self._consecutive_errors = 0
        self._last_error_type: str | None = None
        self._last_tick_started_at: datetime | None = None
        self._last_tick_completed_at: datetime | None = None
        self._last_report: TaskDefinitionDriverReport | None = None

    @property
    def running(self) -> bool:
        return self._runner is not None and not self._runner.done()

    @property
    def state(self) -> TaskDefinitionDriverState:
        return TaskDefinitionDriverState(
            running=self.running,
            ticks_started=self._ticks_started,
            ticks_completed=self._ticks_completed,
            ticks_skipped=self._ticks_skipped,
            consecutive_errors=self._consecutive_errors,
            last_error_type=self._last_error_type,
            last_tick_started_at=self._last_tick_started_at,
            last_tick_completed_at=self._last_tick_completed_at,
            last_report=self._last_report,
        )

    def start(self) -> None:
        if self.running:
            return
        self._stop_event.clear()
        self._runner = asyncio.create_task(
            self._run_loop(),
            name="packbreaker-task-definition-driver",
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

    async def run_once(self) -> TaskDefinitionDriverReport | None:
        if self._tick_lock.locked():
            self._ticks_skipped += 1
            return None
        await self._tick_lock.acquire()
        self._ticks_started += 1
        now = datetime.now(UTC)
        self._last_tick_started_at = now
        try:
            due_scans = await asyncio.to_thread(
                self._service.list_due_monitor_scans,
                now=now,
                limit=self._scan_limit,
            )
            materialized = 0
            baseline = 0
            no_change = 0
            stability_wait = 0
            debounce_wait = 0
            overlap = 0
            scan_failed = 0
            for due in due_scans:
                try:
                    result = await self._service.scan_monitor(
                        due.task_definition_id,
                        trigger=due.trigger,
                        trace_id=str(uuid4()),
                        now=now,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    scan_failed += 1
                    self._logger.exception(
                        "监控任务扫描失败 task_definition_id=%s trigger=%s",
                        due.task_definition_id,
                        due.trigger.value,
                    )
                    continue
                if result.outcome == "MATERIALIZED":
                    materialized += 1
                elif result.outcome == "BASELINE_ESTABLISHED":
                    baseline += 1
                elif result.outcome == "NO_CHANGES":
                    no_change += 1
                elif result.outcome == "STABILITY_WAIT":
                    stability_wait += 1
                elif result.outcome == "DEBOUNCE_WAIT":
                    debounce_wait += 1
                elif result.outcome in {"OVERLAP_SKIPPED", "OVERLAP_QUEUED"}:
                    overlap += 1

            due_lifecycle = await asyncio.to_thread(
                self._service.list_due_lifecycle_advances,
                limit=self._lifecycle_limit,
            )
            lifecycle_advanced = 0
            lifecycle_failed = 0
            for lifecycle in due_lifecycle:
                try:
                    await self._service.advance_execution(
                        lifecycle.task_definition_id,
                        lifecycle.execution_id,
                        actor=TaskActionActor("SYSTEM", "task-definition-driver"),
                        idempotency_key=f"driver-lifecycle-{lifecycle.execution_id}",
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    lifecycle_failed += 1
                    self._logger.exception(
                        "统一任务生命周期自动推进失败 task_definition_id=%s execution_id=%s",
                        lifecycle.task_definition_id,
                        lifecycle.execution_id,
                    )
                    continue
                lifecycle_advanced += 1

            due_retries = await asyncio.to_thread(
                self._service.list_due_auto_retries,
                now=datetime.now(UTC),
                limit=self._retry_limit,
            )
            retried = 0
            retry_failed = 0
            for request in due_retries:
                try:
                    await self._service.retry_auto(request, trace_id=str(uuid4()))
                except asyncio.CancelledError:
                    raise
                except Exception:
                    retry_failed += 1
                    self._logger.exception(
                        "任务失败对象自动重试失败 execution_id=%s attempt=%s",
                        request.execution_id,
                        request.attempt,
                    )
                    continue
                retried += 1

            report = TaskDefinitionDriverReport(
                due_scan_count=len(due_scans),
                materialized_scan_count=materialized,
                baseline_count=baseline,
                no_change_count=no_change,
                stability_wait_count=stability_wait,
                debounce_wait_count=debounce_wait,
                overlap_count=overlap,
                scan_failed_count=scan_failed,
                due_lifecycle_count=len(due_lifecycle),
                lifecycle_advanced_count=lifecycle_advanced,
                lifecycle_failed_count=lifecycle_failed,
                due_retry_count=len(due_retries),
                retried_count=retried,
                retry_failed_count=retry_failed,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._consecutive_errors += 1
            self._last_error_type = type(exc).__name__
            raise
        else:
            self._ticks_completed += 1
            self._consecutive_errors = 0
            self._last_error_type = None
            self._last_report = report
            return report
        finally:
            self._last_tick_completed_at = datetime.now(UTC)
            self._tick_lock.release()

    async def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            with suppress(TimeoutError):
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._interval_seconds,
                )
            if self._stop_event.is_set():
                return
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._logger.exception(
                    "任务定义驱动执行失败 error_type=%s consecutive_errors=%s",
                    self._last_error_type,
                    self._consecutive_errors,
                )
