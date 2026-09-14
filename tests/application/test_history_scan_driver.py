from pathlib import Path

import pytest
from sqlalchemy import func, select

from backend.app.application.history_scan_driver import HistoryScanDriver
from backend.app.application.history_scans import HistoryScanService
from backend.app.domain.history_scan import HistoryMediaKind, HistoryScanStatus
from backend.app.infrastructure.persistence.base import Base
from backend.app.infrastructure.persistence.database import (
    create_session_factory,
    create_sqlite_engine,
)
from backend.app.infrastructure.persistence.models import HistoryScanMaterialization


@pytest.mark.asyncio
async def test_history_scan_driver_advances_only_started_scans_without_materializing(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    movies = data_root / "movies"
    movies.mkdir(parents=True)
    (movies / "a.mkv").write_bytes(b"a")
    (movies / "b.mkv").write_bytes(b"bb")

    engine = create_sqlite_engine(tmp_path / "history-driver.db")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    service = HistoryScanService(factory, data_root=data_root)
    ready = service.create(
        root_path="/data/movies",
        media_kind=HistoryMediaKind.MOVIE,
        extensions=(".mkv",),
        exclude_patterns=(),
    )
    driver = HistoryScanDriver(service, interval_seconds=60, scan_limit=2, batch_size=1)

    idle = await driver.run_once()
    assert idle is not None and idle.inspected_count == 0
    started = service.start(ready.id, expected_version=ready.version)

    first = await driver.run_once()
    assert first is not None
    assert first.inspected_count == 1
    assert first.advanced_count == 1
    assert first.processed_file_count == 1
    assert first.completed_count == 0
    middle = service.get(started.id)
    assert middle.status is HistoryScanStatus.SCANNING
    assert middle.cursor == "a.mkv"

    second = await driver.run_once()
    assert second is not None
    assert second.processed_file_count == 1
    assert second.completed_count == 1
    completed = service.get(started.id)
    assert completed.status is HistoryScanStatus.DONE
    assert completed.discovered_count == 2
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(HistoryScanMaterialization)) == 0
    engine.dispose()


@pytest.mark.asyncio
async def test_history_scan_cancel_preserves_cursor_and_removes_scan_from_driver_queue(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    movies = data_root / "movies"
    movies.mkdir(parents=True)
    (movies / "a.mkv").write_bytes(b"a")
    (movies / "b.mkv").write_bytes(b"bb")

    engine = create_sqlite_engine(tmp_path / "history-cancel.db")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    service = HistoryScanService(factory, data_root=data_root)
    ready = service.create(
        root_path="/data/movies",
        media_kind=HistoryMediaKind.MOVIE,
        extensions=(".mkv",),
        exclude_patterns=(),
    )
    started = service.start(ready.id, expected_version=ready.version)
    first = service.scan_batch(started.id, expected_version=started.version, limit=1)
    cancelled = service.cancel(first.scan.id, expected_version=first.scan.version)

    assert cancelled.status is HistoryScanStatus.CANCELLED
    assert cancelled.cursor == "a.mkv"
    assert cancelled.discovered_count == 1
    driver = HistoryScanDriver(service, interval_seconds=60, scan_limit=2, batch_size=10)
    report = await driver.run_once()
    assert report is not None and report.inspected_count == 0

    restarted = service.start(cancelled.id, expected_version=cancelled.version)
    assert restarted.status is HistoryScanStatus.SCANNING
    assert restarted.generation == cancelled.generation + 1
    assert restarted.cursor is None
    engine.dispose()
