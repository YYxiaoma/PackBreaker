from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.errors import ApplicationError
from backend.app.application.sites import EnabledSiteAdapter
from backend.app.application.task_actions import (
    CancelTaskAction,
    TaskActionActor,
    TaskActionService,
)
from backend.app.application.task_cancellation import (
    TaskCancellationRequest,
    TaskCancellationResult,
)
from backend.app.application.task_linking import TaskLinkingResult
from backend.app.application.task_recovery import RecoveryOutcome, TaskRecoveryCoordinator
from backend.app.application.tasks import TaskAnalysisService
from backend.app.domain.site_adapter import SiteConnectionResult, TorrentDetails, TorrentPayload
from backend.app.domain.site_search import SearchPage, SearchQuery, SiteSearchCapabilities
from backend.app.domain.task_state import (
    PRE_SIDE_EFFECT_CANCELLATION_SCHEMA_VERSION,
    TaskCancellationMode,
    TaskStatus,
)
from backend.app.domain.task_units import SourceTaskFile, identify_task_units
from backend.app.infrastructure.persistence.base import Base
from backend.app.infrastructure.persistence.database import (
    create_session_factory,
    create_sqlite_engine,
)
from backend.app.infrastructure.persistence.models import (
    OperationJournal,
    PreflightSnapshotRecord,
    TaskCandidateRecord,
    TaskEvent,
)
from backend.app.infrastructure.persistence.repositories import (
    OperationIntent,
    OperationJournalRepository,
    TaskCreate,
    TaskRepository,
)


class _BlockingSearchAdapter:
    def __init__(self, started: asyncio.Event, release: asyncio.Event) -> None:
        self._started = started
        self._release = release

    async def capabilities(self) -> SiteSearchCapabilities:
        return SiteSearchCapabilities()

    async def test_connection(self) -> SiteConnectionResult:
        return SiteConnectionResult("blocking")

    async def search(self, query: SearchQuery) -> SearchPage:
        self._started.set()
        await self._release.wait()
        return SearchPage("blocking", query.page, (), False, 0)

    async def fetch_details(self, torrent_id: str) -> TorrentDetails:
        raise AssertionError(f"取消后不应 fetch_details: {torrent_id}")

    async def fetch_torrent(self, torrent_id: str) -> TorrentPayload:
        raise AssertionError(f"取消后不应 fetch_torrent: {torrent_id}")


class _Provider:
    def __init__(self, adapter: _BlockingSearchAdapter) -> None:
        self._binding = EnabledSiteAdapter("cfg-blocking", 1, "blocking", adapter)

    def enabled_adapters(self) -> tuple[EnabledSiteAdapter, ...]:
        return (self._binding,)

    def enabled_site_versions(self) -> tuple[tuple[str, int], ...]:
        return (("cfg-blocking", 1),)


class _LinkingMustNotRun:
    def execute(self, unit_id: str, *, execution_plan_id: str) -> TaskLinkingResult:
        raise AssertionError("分析阶段取消不应进入 linking")


class _CancellationMustNotRun:
    async def execute(self, request: TaskCancellationRequest) -> TaskCancellationResult:
        raise AssertionError(f"分析阶段取消不应进入资源回滚 coordinator: {request.task_id}")


class _RecoveryLinkingMustNotRun:
    def execute(self, unit_id: str, *, execution_plan_id: str) -> object:
        raise AssertionError(f"遗留分析取消不应进入 linking: {unit_id}/{execution_plan_id}")


class _RecoveryAsyncMustNotRun:
    async def execute(self, unit_id: str, *, execution_plan_id: str) -> object:
        raise AssertionError(f"遗留分析取消不应进入副作用 stage: {unit_id}/{execution_plan_id}")


class _RecoveryCancellationMustNotRun:
    async def resume(self, task_id: str) -> object:
        raise AssertionError(f"协作式分析取消不应进入资源回滚恢复: {task_id}")


def _recovery(factory: sessionmaker[Session]) -> TaskRecoveryCoordinator:
    return TaskRecoveryCoordinator(
        factory,
        _RecoveryLinkingMustNotRun(),
        _RecoveryAsyncMustNotRun(),
        _RecoveryAsyncMustNotRun(),
        _RecoveryAsyncMustNotRun(),
        _RecoveryCancellationMustNotRun(),
    )


def _prepare_cancelling_task(
    tmp_path: Path,
    requested_from: TaskStatus,
) -> tuple[Engine, sessionmaker[Session], str]:
    engine = create_sqlite_engine(tmp_path / f"startup-{requested_from.value.lower()}.db")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    with factory() as session:
        repository = TaskRepository(session)
        task, _ = repository.create_or_get(
            TaskCreate(
                "PACKAGE_UNPACK",
                "source",
                f"startup-{requested_from.value.lower()}",
                "movie:startup-cancel",
                f"trace-{requested_from.value.lower()}",
            )
        )
        stages = (
            (TaskStatus.ANALYZING, "ANALYSIS_STARTED"),
            (TaskStatus.SEARCHING, "ANALYSIS_SEARCHING"),
            (TaskStatus.MATCHING, "ANALYSIS_MATCHING"),
            (TaskStatus.VERIFYING, "ANALYSIS_VERIFYING"),
        )
        for status, event_type in stages:
            task = repository.transition(
                task_id=task.id,
                expected_version=task.version,
                to_status=status,
                event_type=event_type,
                reason="构造遗留分析取消测试状态",
            )
            if status is requested_from:
                break
        analysis_version = task.version
        task = repository.transition(
            task_id=task.id,
            expected_version=task.version,
            to_status=TaskStatus.CANCELLING,
            event_type="CANCELLATION_STARTED",
            reason="构造进程崩溃前已登记的协作式取消",
            checkpoint={
                "schema_version": PRE_SIDE_EFFECT_CANCELLATION_SCHEMA_VERSION,
                "stage": TaskStatus.CANCELLING.value,
                "mode": TaskCancellationMode.COOPERATIVE_ANALYSIS.value,
                "requested_from_status": requested_from.value,
                "remove_downloader_task": False,
                "rollback_created_resources": False,
                "analysis_version": analysis_version,
            },
        )
        session.commit()
        task_id = task.id
    return engine, factory, task_id


@pytest.mark.asyncio
async def test_running_analysis_cooperatively_cancels_after_search_checkpoint(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    source_root = data_root / "movie"
    source_root.mkdir(parents=True)
    source_file = source_root / "Movie.2026.mkv"
    content = b"0123456789abcdef"
    source_file.write_bytes(content)
    unit = identify_task_units((SourceTaskFile(source_file.name, len(content)),))[0]

    engine = create_sqlite_engine(tmp_path / "analysis-cancel.db")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    with factory() as session:
        task, _ = TaskRepository(session).create_or_get(
            TaskCreate(
                "PACKAGE_UNPACK",
                "source",
                "analysis-cancel-source",
                unit.normalized_unit_key,
                "analysis-cancel-trace",
            )
        )
        session.commit()
        task_id = task.id

    search_started = asyncio.Event()
    release_search = asyncio.Event()
    provider = _Provider(_BlockingSearchAdapter(search_started, release_search))
    analysis = TaskAnalysisService(factory, provider, data_root=data_root)
    actions = TaskActionService(factory, _LinkingMustNotRun(), _CancellationMustNotRun())

    analysis_run = asyncio.create_task(analysis.analyze(task_id, source_root="movie"))
    try:
        await asyncio.wait_for(search_started.wait(), timeout=3)
        with factory() as session:
            stored_task = TaskRepository(session).get(task_id)
            assert stored_task is not None
            assert stored_task.status == TaskStatus.SEARCHING.value
            analysis_version = stored_task.version

        accepted = await actions.cancel(
            CancelTaskAction(task_id, False, False),
            actor=TaskActionActor("admin_session", "analysis-cancel-actor"),
            idempotency_key="analysis-cancel-request",
        )
        assert accepted.status is TaskStatus.CANCELLING
        assert accepted.execution_plan_id is None
        with factory() as session:
            stored_task = TaskRepository(session).get(task_id)
            assert stored_task is not None
            assert stored_task.status == TaskStatus.CANCELLING.value
            assert stored_task.checkpoint["mode"] == "COOPERATIVE_ANALYSIS"
            assert stored_task.checkpoint["requested_from_status"] == TaskStatus.SEARCHING.value
            assert stored_task.checkpoint["analysis_version"] == analysis_version

        release_search.set()
        with pytest.raises(ApplicationError) as cancelled:
            await asyncio.wait_for(analysis_run, timeout=3)
        assert cancelled.value.code == "ANALYSIS_CANCELLED"

        with factory() as session:
            stored_task = TaskRepository(session).get(task_id)
            assert stored_task is not None
            assert stored_task.status == TaskStatus.CANCELLED.value
            assert stored_task.checkpoint["mode"] == "COOPERATIVE_ANALYSIS"
            assert stored_task.checkpoint["stage"] == TaskStatus.CANCELLED.value
            assert session.scalar(select(func.count()).select_from(OperationJournal)) == 0
            assert session.scalar(select(func.count()).select_from(PreflightSnapshotRecord)) == 0
            assert session.scalar(select(func.count()).select_from(TaskCandidateRecord)) == 0
            events = list(
                session.scalars(
                    select(TaskEvent)
                    .where(TaskEvent.task_id == task_id)
                    .order_by(TaskEvent.created_at, TaskEvent.id)
                )
            )
        assert [event.event_type for event in events[-5:]] == [
            "ANALYSIS_STARTED",
            "ANALYSIS_SEARCHING",
            "TASK_CANCEL_REQUESTED",
            "CANCELLATION_STARTED",
            "CANCELLATION_COMPLETED",
        ]
    finally:
        release_search.set()
        if not analysis_run.done():
            analysis_run.cancel()
            with pytest.raises(asyncio.CancelledError):
                await analysis_run
        engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "requested_from",
    [
        TaskStatus.ANALYZING,
        TaskStatus.SEARCHING,
        TaskStatus.MATCHING,
        TaskStatus.VERIFYING,
    ],
)
async def test_startup_recovery_completes_abandoned_cooperative_analysis_cancel(
    tmp_path: Path,
    requested_from: TaskStatus,
) -> None:
    engine, factory, task_id = _prepare_cancelling_task(tmp_path, requested_from)
    try:
        report = await _recovery(factory).reconcile_once(recover_abandoned_analysis=True)

        assert report.scanned_count == 1
        assert report.completed_count == 1
        assert report.blocked_count == 0
        item = report.items[0]
        assert item.initial_status is TaskStatus.CANCELLING
        assert item.final_status is TaskStatus.CANCELLED
        assert item.execution_plan_id is None
        assert item.outcome is RecoveryOutcome.COMPLETED
        assert item.steps == (TaskStatus.CANCELLING,)
        with factory() as session:
            task = TaskRepository(session).get(task_id)
            assert task is not None
            assert task.status == TaskStatus.CANCELLED.value
            assert task.checkpoint["stage"] == TaskStatus.CANCELLED.value
            assert task.checkpoint["mode"] == TaskCancellationMode.COOPERATIVE_ANALYSIS.value
            assert OperationJournalRepository(session).list_for_task(task_id) == []
            latest = TaskRepository(session).latest_event(task_id)
            assert latest is not None
            assert latest.event_type == "CANCELLATION_COMPLETED"
            assert latest.from_status == TaskStatus.CANCELLING.value
            assert latest.to_status == TaskStatus.CANCELLED.value
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_periodic_recovery_ignores_live_cooperative_analysis_cancel(tmp_path: Path) -> None:
    engine, factory, task_id = _prepare_cancelling_task(tmp_path, TaskStatus.SEARCHING)
    try:
        report = await _recovery(factory).reconcile_once()

        assert report.scanned_count == 0
        with factory() as session:
            task = TaskRepository(session).get(task_id)
            assert task is not None and task.status == TaskStatus.CANCELLING.value
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_startup_recovery_blocks_malformed_or_resource_cancelling_checkpoint(
    tmp_path: Path,
) -> None:
    engine, factory, task_id = _prepare_cancelling_task(tmp_path, TaskStatus.MATCHING)
    try:
        with factory() as session:
            task = TaskRepository(session).get(task_id)
            assert task is not None
            task.checkpoint = {
                "schema_version": "packbreaker-cancellation-checkpoint-v1",
                "stage": TaskStatus.ROLLING_BACK.value,
                "remove_downloader_task": True,
                "rollback_created_resources": True,
            }
            session.commit()

        report = await _recovery(factory).reconcile_once(recover_abandoned_analysis=True)

        assert report.blocked_count == 1
        assert report.items[0].error_code == "RECOVERY_CANCELLING_EVIDENCE_INVALID"
        assert report.items[0].final_status is TaskStatus.CANCELLING
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_startup_recovery_blocks_cooperative_cancel_when_operation_journal_exists(
    tmp_path: Path,
) -> None:
    engine, factory, task_id = _prepare_cancelling_task(tmp_path, TaskStatus.VERIFYING)
    try:
        with factory() as session:
            OperationJournalRepository(session).record_intent(
                OperationIntent(
                    task_id=task_id,
                    idempotency_key="unexpected-side-effect",
                    operation_type="CREATE_HARDLINK",
                    target={"resource": "synthetic"},
                    intent={"synthetic": True},
                )
            )
            session.commit()

        report = await _recovery(factory).reconcile_once(recover_abandoned_analysis=True)

        assert report.blocked_count == 1
        assert report.items[0].error_code == "CANCELLATION_EVIDENCE_CONFLICT"
        assert report.items[0].final_status is TaskStatus.CANCELLING
    finally:
        engine.dispose()
