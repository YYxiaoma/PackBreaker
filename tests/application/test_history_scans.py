from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import func, select

from backend.app.application.errors import ApplicationError
from backend.app.application.history_scans import HistoryScanService
from backend.app.domain.history_scan import HistoryMediaKind, HistoryScanStatus
from backend.app.infrastructure.persistence.base import Base
from backend.app.infrastructure.persistence.database import (
    create_session_factory,
    create_sqlite_engine,
)
from backend.app.infrastructure.persistence.models import HistoryScanFile


def test_history_scan_is_incremental_and_pause_resume_preserves_cursor(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    data_root.mkdir()
    engine = create_sqlite_engine(tmp_path / "test.db")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    service = HistoryScanService(factory, data_root=data_root)
    movies = data_root / "movies"
    movies.mkdir()
    (movies / "a.mkv").write_bytes(b"a")
    (movies / "b.mp4").write_bytes(b"bb")
    (movies / "sample.mkv").write_bytes(b"excluded")
    (movies / "notes.txt").write_text("ignored")
    (data_root / "outside.mkv").write_bytes(b"outside")
    (movies / "link.mkv").symlink_to(data_root / "outside.mkv")

    created = service.create(
        root_path="/data/movies",
        media_kind=HistoryMediaKind.MOVIE,
        extensions=("mkv", ".mp4"),
        exclude_patterns=("sample",),
    )
    assert created.status is HistoryScanStatus.READY
    assert created.root_relative_path == "movies"
    assert created.extensions == (".mkv", ".mp4")

    started = service.start(created.id, expected_version=created.version)
    first = service.scan_batch(started.id, expected_version=started.version, limit=1)
    assert first.processed_count == 1
    assert first.has_more is True
    assert first.scan.cursor == "a.mkv"
    assert first.scan.new_count == 1

    with pytest.raises(ApplicationError) as stale_retry:
        service.scan_batch(started.id, expected_version=started.version, limit=1)
    assert stale_retry.value.code == "HISTORY_SCAN_VERSION_CONFLICT"
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(HistoryScanFile)) == 1

    paused = service.pause(first.scan.id, expected_version=first.scan.version)
    assert paused.status is HistoryScanStatus.PAUSED
    assert paused.cursor == "a.mkv"
    resumed = service.resume(paused.id, expected_version=paused.version)
    second = service.scan_batch(resumed.id, expected_version=resumed.version, limit=10)
    assert second.has_more is False
    assert second.scan.status is HistoryScanStatus.DONE
    assert second.scan.new_count == 2
    assert second.scan.discovered_count == 2

    repeated = service.start(second.scan.id, expected_version=second.scan.version)
    repeated_result = service.scan_batch(
        repeated.id,
        expected_version=repeated.version,
        limit=10,
    )
    assert repeated_result.scan.status is HistoryScanStatus.DONE
    assert repeated_result.scan.new_count == 0
    assert repeated_result.scan.changed_count == 0
    assert repeated_result.scan.unchanged_count == 2

    (movies / "b.mp4").write_bytes(b"changed-size")
    (movies / "c.mkv").write_bytes(b"ccc")
    third = service.start(repeated_result.scan.id, expected_version=repeated_result.scan.version)
    third_result = service.scan_batch(third.id, expected_version=third.version, limit=10)
    assert third_result.scan.new_count == 1
    assert third_result.scan.changed_count == 1
    assert third_result.scan.unchanged_count == 1

    with factory() as session:
        assert session.scalar(select(func.count()).select_from(HistoryScanFile)) == 3
        paths = tuple(
            session.scalars(
                select(HistoryScanFile.relative_path).order_by(HistoryScanFile.relative_path)
            )
        )
    assert paths == ("a.mkv", "b.mp4", "c.mkv")
