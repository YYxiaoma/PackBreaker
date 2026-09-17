from datetime import datetime

import pytest

from backend.app.application.task_definition_driver import TaskDefinitionDriver
from backend.app.application.task_definition_executions import (
    AutoRetryRequest,
    DueMonitorScan,
    TaskMonitorScanView,
)
from backend.app.domain.task_definition import TaskExecutionTrigger


class _FakeAutomation:
    def __init__(self) -> None:
        self.scanned: list[str] = []
        self.retried: list[str] = []

    def list_due_monitor_scans(
        self,
        *,
        now: datetime,
        limit: int,
    ) -> tuple[DueMonitorScan, ...]:
        assert now.tzinfo is not None
        assert limit == 5
        return (DueMonitorScan("definition-1", TaskExecutionTrigger.CRON),)

    async def scan_monitor(
        self,
        definition_id: str,
        *,
        trigger: TaskExecutionTrigger,
        trace_id: str,
        now: datetime | None = None,
    ) -> TaskMonitorScanView:
        assert trigger is TaskExecutionTrigger.CRON
        assert trace_id
        assert now is not None and now.tzinfo is not None
        self.scanned.append(definition_id)
        return TaskMonitorScanView(
            task_definition_id=definition_id,
            trigger=trigger.value,
            outcome="MATERIALIZED",
            discovered_count=2,
            new_count=1,
            next_run_at=None,
            execution=None,
        )

    def list_due_auto_retries(
        self,
        *,
        now: datetime,
        limit: int,
    ) -> tuple[AutoRetryRequest, ...]:
        assert now.tzinfo is not None
        assert limit == 7
        return (AutoRetryRequest("execution-1", frozenset({"item-1"}), 1),)

    async def retry_auto(self, request: AutoRetryRequest, *, trace_id: str) -> object:
        assert trace_id
        self.retried.append(request.execution_id)
        return object()


class _DebounceAutomation(_FakeAutomation):
    async def scan_monitor(
        self,
        definition_id: str,
        *,
        trigger: TaskExecutionTrigger,
        trace_id: str,
        now: datetime | None = None,
    ) -> TaskMonitorScanView:
        assert now is not None
        self.scanned.append(definition_id)
        return TaskMonitorScanView(
            task_definition_id=definition_id,
            trigger=trigger.value,
            outcome="DEBOUNCE_WAIT",
            discovered_count=1,
            new_count=1,
            next_run_at=None,
            execution=None,
        )


@pytest.mark.asyncio
async def test_task_definition_driver_counts_debounce_wait_as_normal_scan() -> None:
    automation = _DebounceAutomation()
    driver = TaskDefinitionDriver(
        automation,
        interval_seconds=15,
        scan_limit=5,
        retry_limit=7,
    )

    report = await driver.run_once()

    assert report is not None
    assert report.debounce_wait_count == 1
    assert report.scan_failed_count == 0
    assert report.materialized_scan_count == 0


@pytest.mark.asyncio
async def test_task_definition_driver_advances_due_scan_and_retry_in_one_tick() -> None:
    automation = _FakeAutomation()
    driver = TaskDefinitionDriver(
        automation,
        interval_seconds=15,
        scan_limit=5,
        retry_limit=7,
    )

    report = await driver.run_once()

    assert report is not None
    assert report.due_scan_count == 1
    assert report.materialized_scan_count == 1
    assert report.scan_failed_count == 0
    assert report.due_retry_count == 1
    assert report.retried_count == 1
    assert report.retry_failed_count == 0
    assert automation.scanned == ["definition-1"]
    assert automation.retried == ["execution-1"]
    assert driver.state.ticks_completed == 1
