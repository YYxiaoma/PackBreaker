from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
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
from backend.app.infrastructure.persistence.models import (
    HistoryScanFile,
    HistoryScanMaterialization,
    TaskUnitRecord,
    UnpackTask,
)


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


def test_history_scan_materializes_each_file_snapshot_once_and_changed_snapshot_creates_new_task(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    movies = data_root / "movies"
    movies.mkdir(parents=True)
    movie = movies / "Movie.2026.mkv"
    episode = movies / "Show.S01E01.mkv"
    movie.write_bytes(b"movie-v1")
    episode.write_bytes(b"episode")

    engine = create_sqlite_engine(tmp_path / "materialize.db")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    service = HistoryScanService(factory, data_root=data_root)

    created = service.create(
        root_path="/data/movies",
        media_kind=HistoryMediaKind.MOVIE,
        extensions=(".mkv",),
        exclude_patterns=(),
    )
    started = service.start(created.id, expected_version=created.version)
    scanned = service.scan_batch(started.id, expected_version=started.version, limit=10)
    assert scanned.scan.status is HistoryScanStatus.DONE

    first = service.materialize(scanned.scan.id, expected_version=scanned.scan.version, limit=1)
    assert first.processed_count == 1
    assert first.task_created_count == 1
    assert first.task_reused_count == 0
    assert first.skipped_count == 0
    assert first.remaining_count == 1
    assert first.items[0].status.value == "MATERIALIZED"
    assert first.items[0].unit_kind == "MOVIE"
    assert first.items[0].source_root == "movies"
    assert first.items[0].task_id is not None

    second = service.materialize(scanned.scan.id, expected_version=scanned.scan.version, limit=10)
    assert second.processed_count == 1
    assert second.task_created_count == 0
    assert second.skipped_count == 1
    assert second.remaining_count == 0
    assert second.items[0].status.value == "SKIPPED"
    assert second.items[0].reason_code == "MEDIA_KIND_MISMATCH"

    replay = service.materialize(scanned.scan.id, expected_version=scanned.scan.version, limit=10)
    assert replay.processed_count == 0
    assert replay.remaining_count == 0

    with factory() as session:
        assert session.scalar(select(func.count()).select_from(UnpackTask)) == 1
        assert session.scalar(select(func.count()).select_from(TaskUnitRecord)) == 1
        assert session.scalar(select(func.count()).select_from(HistoryScanMaterialization)) == 2

    repeated = service.start(scanned.scan.id, expected_version=scanned.scan.version)
    repeated_scan = service.scan_batch(repeated.id, expected_version=repeated.version, limit=10)
    assert repeated_scan.scan.new_count == 0
    assert repeated_scan.scan.changed_count == 0
    repeated_materialize = service.materialize(
        repeated_scan.scan.id,
        expected_version=repeated_scan.scan.version,
        limit=10,
    )
    assert repeated_materialize.processed_count == 0

    movie.write_bytes(b"movie-v2-changed")
    changed = service.start(repeated_scan.scan.id, expected_version=repeated_scan.scan.version)
    changed_scan = service.scan_batch(changed.id, expected_version=changed.version, limit=10)
    assert changed_scan.scan.changed_count == 1
    changed_materialize = service.materialize(
        changed_scan.scan.id,
        expected_version=changed_scan.scan.version,
        limit=10,
    )
    assert changed_materialize.processed_count == 1
    assert changed_materialize.task_created_count == 1
    assert changed_materialize.remaining_count == 0

    with factory() as session:
        assert session.scalar(select(func.count()).select_from(UnpackTask)) == 2
        assert session.scalar(select(func.count()).select_from(TaskUnitRecord)) == 2
        assert session.scalar(select(func.count()).select_from(HistoryScanMaterialization)) == 3
    engine.dispose()


def test_history_scan_materialize_is_idempotent_under_concurrent_replay(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    movies = data_root / "movies"
    movies.mkdir(parents=True)
    (movies / "Movie.2026.mkv").write_bytes(b"movie")

    engine = create_sqlite_engine(tmp_path / "concurrent.db")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    service = HistoryScanService(factory, data_root=data_root)
    created = service.create(
        root_path="/data/movies",
        media_kind=HistoryMediaKind.MOVIE,
        extensions=(".mkv",),
        exclude_patterns=(),
    )
    started = service.start(created.id, expected_version=created.version)
    scanned = service.scan_batch(started.id, expected_version=started.version, limit=10)

    def materialize_once() -> int:
        result = service.materialize(
            scanned.scan.id,
            expected_version=scanned.scan.version,
            limit=10,
        )
        return result.task_created_count

    with ThreadPoolExecutor(max_workers=10) as pool:
        created_counts = list(pool.map(lambda _index: materialize_once(), range(10)))

    assert sum(created_counts) == 1
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(UnpackTask)) == 1
        assert session.scalar(select(func.count()).select_from(TaskUnitRecord)) == 1
        assert session.scalar(select(func.count()).select_from(HistoryScanMaterialization)) == 1
    engine.dispose()


def test_history_scan_materialize_rejects_source_change_after_scan(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    movies = data_root / "movies"
    movies.mkdir(parents=True)
    movie = movies / "Movie.2026.mkv"
    movie.write_bytes(b"before")

    engine = create_sqlite_engine(tmp_path / "changed.db")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    service = HistoryScanService(factory, data_root=data_root)
    created = service.create(
        root_path="/data/movies",
        media_kind=HistoryMediaKind.MOVIE,
        extensions=(".mkv",),
        exclude_patterns=(),
    )
    started = service.start(created.id, expected_version=created.version)
    scanned = service.scan_batch(started.id, expected_version=started.version, limit=10)
    movie.write_bytes(b"after-change")

    with pytest.raises(ApplicationError) as changed:
        service.materialize(scanned.scan.id, expected_version=scanned.scan.version, limit=10)
    assert changed.value.code == "HISTORY_SCAN_SOURCE_CHANGED"

    with factory() as session:
        assert session.scalar(select(func.count()).select_from(UnpackTask)) == 0
        assert session.scalar(select(func.count()).select_from(HistoryScanMaterialization)) == 0
    engine.dispose()


def test_episode_history_materialization_groups_versions_ranges_and_specials(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    show_root = data_root / "shows" / "Example Show 2024"
    season = show_root / "Season 01"
    specials = show_root / "Specials"
    season.mkdir(parents=True)
    specials.mkdir(parents=True)
    (season / "01.1080p.WEB-DL.H265.mkv").write_bytes(b"episode-1080")
    (season / "01.2160p.BluRay.H265.mkv").write_bytes(b"episode-2160-longer")
    (season / "E02-E04.1080p.mkv").write_bytes(b"range")
    (specials / "01.1080p.mkv").write_bytes(b"special")

    engine = create_sqlite_engine(tmp_path / "episode-history.db")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    service = HistoryScanService(factory, data_root=data_root)
    created = service.create(
        root_path="/data/shows",
        media_kind=HistoryMediaKind.EPISODE,
        extensions=(".mkv",),
        exclude_patterns=(),
    )
    started = service.start(created.id, expected_version=created.version)
    scanned = service.scan_batch(started.id, expected_version=started.version, limit=100)
    assert scanned.scan.status is HistoryScanStatus.DONE
    assert scanned.scan.new_count == 4

    materialized = service.materialize(
        scanned.scan.id,
        expected_version=scanned.scan.version,
        limit=100,
    )
    assert materialized.task_created_count == 4
    assert materialized.skipped_count == 0

    results = service.list_task_results(scanned.scan.id)
    by_path = {item.relative_path: item for item in results}
    first = by_path["Example Show 2024/Season 01/01.1080p.WEB-DL.H265.mkv"]
    second = by_path["Example Show 2024/Season 01/01.2160p.BluRay.H265.mkv"]
    ranged = by_path["Example Show 2024/Season 01/E02-E04.1080p.mkv"]
    special = by_path["Example Show 2024/Specials/01.1080p.mkv"]

    assert first.episode_kind == second.episode_kind == "SEASON_EPISODE"
    assert first.episode_label == second.episode_label == "S01E01"
    assert first.episode_group_key == second.episode_group_key
    assert first.episode_variant_key != second.episode_variant_key
    assert first.variant_count == second.variant_count == 2
    assert ranged.episode_kind == "SEASON_RANGE"
    assert (ranged.episode_season, ranged.episode_start, ranged.episode_end) == (1, 2, 4)
    assert ranged.episode_label == "S01E02-E04"
    assert ranged.variant_count == 1
    assert special.episode_kind == "SPECIALS"
    assert (special.episode_season, special.episode_start) == (0, 1)
    assert special.episode_label == "Specials E01"
    assert special.variant_count == 1
    engine.dispose()
