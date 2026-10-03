from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from backend.app.application.errors import ApplicationError
from backend.app.application.movie_dedup import MovieDedupJobCreate, MovieDedupService
from backend.app.domain.movie_dedup import (
    MovieDedupCrossFilesystemPolicy,
    MovieDedupJobPhase,
    MovieDedupJobStatus,
    MovieDedupMode,
    MovieDedupPairStatus,
    MovieDedupResolvedAction,
    resolve_movie_dedup_action,
)
from backend.app.infrastructure.authorized_paths import AuthorizedPathScope
from backend.app.infrastructure.persistence.base import Base
from backend.app.infrastructure.persistence.database import (
    create_session_factory,
    create_sqlite_engine,
)
from backend.app.infrastructure.persistence.models import MovieDedupOperationJournal, MovieDedupPair


@pytest.mark.parametrize(
    ("mode", "policy", "same_filesystem", "expected"),
    [
        (
            MovieDedupMode.AUTO,
            MovieDedupCrossFilesystemPolicy.STOP,
            True,
            MovieDedupResolvedAction.HARDLINK,
        ),
        (
            MovieDedupMode.AUTO,
            MovieDedupCrossFilesystemPolicy.STOP,
            False,
            MovieDedupResolvedAction.BLOCKED,
        ),
        (
            MovieDedupMode.AUTO,
            MovieDedupCrossFilesystemPolicy.SYMLINK,
            False,
            MovieDedupResolvedAction.SYMLINK,
        ),
        (
            MovieDedupMode.HARDLINK,
            MovieDedupCrossFilesystemPolicy.SYMLINK,
            False,
            MovieDedupResolvedAction.BLOCKED,
        ),
        (
            MovieDedupMode.SYMLINK,
            MovieDedupCrossFilesystemPolicy.STOP,
            False,
            MovieDedupResolvedAction.SYMLINK,
        ),
        (
            MovieDedupMode.SCAN_ONLY,
            MovieDedupCrossFilesystemPolicy.STOP,
            True,
            MovieDedupResolvedAction.SCAN_ONLY,
        ),
    ],
)
def test_resolve_movie_dedup_action_never_silently_falls_back(
    mode: MovieDedupMode,
    policy: MovieDedupCrossFilesystemPolicy,
    same_filesystem: bool,
    expected: MovieDedupResolvedAction,
) -> None:
    assert resolve_movie_dedup_action(mode, policy, same_filesystem=same_filesystem) is expected


def test_movie_dedup_job_create_and_list(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    source = data_root / "movies-a"
    target = data_root / "movies-b"
    source.mkdir(parents=True)
    target.mkdir()
    engine = create_sqlite_engine(tmp_path / "movie-dedup.db")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    service = MovieDedupService(factory, data_root=data_root)
    try:
        precheck = service.precheck(
            source_root="movies-a",
            target_root="movies-b",
            mode=MovieDedupMode.AUTO,
            cross_filesystem_policy=MovieDedupCrossFilesystemPolicy.STOP,
        )
        assert precheck.same_filesystem
        assert precheck.resolved_action is MovieDedupResolvedAction.HARDLINK
        assert precheck.blocked_reasons == ()

        created = service.create(
            MovieDedupJobCreate(
                name="电影库去重",
                source_root="movies-a",
                target_root="movies-b",
                mode=MovieDedupMode.AUTO,
                cross_filesystem_policy=MovieDedupCrossFilesystemPolicy.STOP,
                video_extensions=("MKV", ".MP4", ".mkv"),
            ),
            trace_id="trace-movie-dedup",
        )
        assert created.name == "电影库去重"
        assert created.source_root == "movies-a"
        assert created.target_root == "movies-b"
        assert created.video_extensions == (".mkv", ".mp4")
        assert created.status is MovieDedupJobStatus.PENDING
        assert created.phase is MovieDedupJobPhase.PENDING
        assert service.get(created.id).id == created.id
        assert [item.id for item in service.list()] == [created.id]
    finally:
        engine.dispose()


def test_movie_dedup_absolute_authorized_mounts_execute_without_data_alias(tmp_path: Path) -> None:
    source_root = tmp_path / "downloads"
    target_root = tmp_path / "downloads2"
    source_root.mkdir()
    target_root.mkdir()
    name = "Absolute.Movie.2026.1080p-GROUP.mkv"
    content = b"absolute-authorized-path" * 4096
    (source_root / name).write_bytes(content)
    (target_root / name).write_bytes(content)

    engine = create_sqlite_engine(tmp_path / "movie-dedup-absolute.db")
    Base.metadata.create_all(engine)
    scope = AuthorizedPathScope(
        legacy_data_root=tmp_path / "data",
        config_dir=tmp_path / "config",
        authorized_roots=(source_root, target_root),
    )
    service = MovieDedupService(
        create_session_factory(engine),
        data_root=tmp_path / "data",
        path_scope=scope,
    )
    try:
        job = service.create(
            MovieDedupJobCreate(
                name="绝对挂载去重",
                source_root=source_root.as_posix(),
                target_root=target_root.as_posix(),
                mode=MovieDedupMode.AUTO,
                cross_filesystem_policy=MovieDedupCrossFilesystemPolicy.STOP,
                video_extensions=(".mkv",),
            ),
            trace_id="trace-absolute",
        )
        assert job.source_root == source_root.as_posix()
        assert job.target_root == target_root.as_posix()
        service.start(job.id)
        current = service.get(job.id)
        for _ in range(20):
            if current.status is MovieDedupJobStatus.REVIEW_REQUIRED:
                break
            current = service.advance(
                job.id,
                scan_batch_size=1,
                match_batch_size=1,
                verify_batch_size=1,
            )
        assert current.status is MovieDedupJobStatus.REVIEW_REQUIRED
        pair = next(
            item
            for item in service.list_pairs(job.id)
            if item.status is MovieDedupPairStatus.VERIFIED_DUPLICATE
        )
        service.execute_pairs(
            job.id,
            pair_ids=(pair.id,),
            idempotency_key="absolute-authorized-hardlink",
        )
        assert (source_root / name).stat().st_ino == (target_root / name).stat().st_ino
    finally:
        engine.dispose()


def test_movie_dedup_rejects_overlapping_roots(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    source = data_root / "movies"
    nested = source / "collection"
    nested.mkdir(parents=True)
    engine = create_sqlite_engine(tmp_path / "movie-dedup-overlap.db")
    Base.metadata.create_all(engine)
    service = MovieDedupService(create_session_factory(engine), data_root=data_root)
    try:
        with pytest.raises(ApplicationError) as exc_info:
            service.precheck(
                source_root="movies",
                target_root="movies/collection",
                mode=MovieDedupMode.AUTO,
                cross_filesystem_policy=MovieDedupCrossFilesystemPolicy.STOP,
            )
        assert exc_info.value.code == "MOVIE_DEDUP_INPUT_INVALID"
    finally:
        engine.dispose()


def test_movie_dedup_scan_match_and_sha256_verification(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    source_root = data_root / "movies-a"
    target_root = data_root / "movies-b"
    source_root.mkdir(parents=True)
    target_root.mkdir()
    name = "Example.Movie.2025.2160p.BluRay-GROUP.mkv"
    content = (b"movie-content-" * 1024) + b"tail"
    (source_root / name).write_bytes(content)
    (target_root / name).write_bytes(content)
    (target_root / "different.2025.2160p.BluRay-GROUP.mkv").write_bytes(b"x" * len(content))

    engine = create_sqlite_engine(tmp_path / "movie-dedup-pipeline.db")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    service = MovieDedupService(factory, data_root=data_root)
    try:
        job = service.create(
            MovieDedupJobCreate(
                name="影片去重验证",
                source_root="movies-a",
                target_root="movies-b",
                mode=MovieDedupMode.AUTO,
                cross_filesystem_policy=MovieDedupCrossFilesystemPolicy.STOP,
                video_extensions=(".mkv",),
            ),
            trace_id="trace-pipeline",
        )
        service.start(job.id)
        current = service.get(job.id)
        for _ in range(20):
            if current.status is MovieDedupJobStatus.REVIEW_REQUIRED:
                break
            current = service.advance(
                job.id,
                scan_batch_size=1,
                match_batch_size=1,
                verify_batch_size=1,
            )

        assert current.status is MovieDedupJobStatus.REVIEW_REQUIRED
        assert current.phase is MovieDedupJobPhase.REVIEW
        assert current.source_file_count == 1
        assert current.target_file_count == 2
        assert current.verified_count == 1
        assert current.logical_duplicate_bytes == len(content)
        with factory() as session:
            pairs = tuple(session.scalars(select(MovieDedupPair)))
        assert any(
            pair.status == MovieDedupPairStatus.VERIFIED_DUPLICATE.value
            and pair.full_hash_match
            and pair.quick_hash_match
            and pair.resolved_action == MovieDedupResolvedAction.HARDLINK.value
            for pair in pairs
        )
        assert any(
            pair.status == MovieDedupPairStatus.BLOCKED.value
            and pair.error_code == "MOVIE_CONTENT_DIFFERENT"
            for pair in pairs
        )
        verified = next(
            pair for pair in pairs if pair.status == MovieDedupPairStatus.VERIFIED_DUPLICATE.value
        )
        completed = service.execute_pairs(
            job.id,
            pair_ids=(verified.id,),
            idempotency_key="movie-dedup-execute-1",
        )
        assert completed.status is MovieDedupJobStatus.COMPLETED
        assert completed.deduplicated_count == 1
        assert (source_root / name).stat(follow_symlinks=False).st_ino == (target_root / name).stat(
            follow_symlinks=False
        ).st_ino
        with factory() as session:
            journal = session.scalar(select(MovieDedupOperationJournal))
            assert journal is not None
            assert journal.status == "COMMITTED"
            assert journal.temporary_path is not None
        assert not (data_root / journal.temporary_path).exists()
    finally:
        engine.dispose()


def test_movie_dedup_reconciles_crash_after_atomic_exchange(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_root = tmp_path / "data"
    source_root = data_root / "movies-a"
    target_root = data_root / "movies-b"
    source_root.mkdir(parents=True)
    target_root.mkdir()
    name = "Crash.Movie.2026.2160p-GROUP.mkv"
    content = b"recoverable-content" * 4096
    (source_root / name).write_bytes(content)
    (target_root / name).write_bytes(content)

    engine = create_sqlite_engine(tmp_path / "movie-dedup-recovery.db")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    service = MovieDedupService(factory, data_root=data_root)
    try:
        job = service.create(
            MovieDedupJobCreate(
                name="崩溃恢复",
                source_root="movies-a",
                target_root="movies-b",
                mode=MovieDedupMode.AUTO,
                cross_filesystem_policy=MovieDedupCrossFilesystemPolicy.STOP,
                video_extensions=(".mkv",),
            ),
            trace_id="trace-recovery",
        )
        service.start(job.id)
        current = service.get(job.id)
        for _ in range(20):
            if current.status is MovieDedupJobStatus.REVIEW_REQUIRED:
                break
            current = service.advance(
                job.id, scan_batch_size=1, match_batch_size=1, verify_batch_size=1
            )
        pair = next(
            item
            for item in service.list_pairs(job.id)
            if item.status is MovieDedupPairStatus.VERIFIED_DUPLICATE
        )

        original_exchange = service._filesystem.exchange_duplicate_with_hardlink

        def crashing_exchange(**kwargs: Any) -> Any:
            def crash(checkpoint: str) -> None:
                if checkpoint == "after_dedup_exchange":
                    raise RuntimeError("simulated-dedup-crash")

            return original_exchange(**kwargs, fault_hook=crash)

        monkeypatch.setattr(
            service._filesystem,
            "exchange_duplicate_with_hardlink",
            crashing_exchange,
        )
        with pytest.raises(RuntimeError, match="simulated-dedup-crash"):
            service.execute_pairs(
                job.id,
                pair_ids=(pair.id,),
                idempotency_key="crash-after-exchange",
            )

        assert (source_root / name).stat(follow_symlinks=False).st_ino == (target_root / name).stat(
            follow_symlinks=False
        ).st_ino
        with factory() as session:
            journal = session.scalar(select(MovieDedupOperationJournal))
            assert journal is not None
            assert journal.status == "INTENT_RECORDED"

        monkeypatch.setattr(
            service._filesystem,
            "exchange_duplicate_with_hardlink",
            original_exchange,
        )
        assert service.reconcile_job_operations(job.id)
        recovered = service.get(job.id)
        assert recovered.status is MovieDedupJobStatus.COMPLETED
        assert recovered.deduplicated_count == 1
        with factory() as session:
            journal = session.scalar(select(MovieDedupOperationJournal))
            assert journal is not None
            assert journal.status == "COMMITTED"
            assert journal.temporary_path is not None
        assert not (data_root / journal.temporary_path).exists()
    finally:
        engine.dispose()


def test_movie_dedup_scan_only_completes_without_replacing_target(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    source_root = data_root / "movies-a"
    target_root = data_root / "movies-b"
    source_root.mkdir(parents=True)
    target_root.mkdir()
    name = "Scan.Only.2026.1080p-GROUP.mkv"
    content = b"scan-only-content" * 2048
    (source_root / name).write_bytes(content)
    (target_root / name).write_bytes(content)
    source_inode = (source_root / name).stat(follow_symlinks=False).st_ino
    target_inode = (target_root / name).stat(follow_symlinks=False).st_ino
    assert source_inode != target_inode

    engine = create_sqlite_engine(tmp_path / "movie-dedup-scan-only.db")
    Base.metadata.create_all(engine)
    service = MovieDedupService(create_session_factory(engine), data_root=data_root)
    try:
        job = service.create(
            MovieDedupJobCreate(
                name="仅扫描",
                source_root="movies-a",
                target_root="movies-b",
                mode=MovieDedupMode.SCAN_ONLY,
                cross_filesystem_policy=MovieDedupCrossFilesystemPolicy.STOP,
                video_extensions=(".mkv",),
            ),
            trace_id="trace-scan-only",
        )
        service.start(job.id)
        current = service.get(job.id)
        for _ in range(20):
            if current.status in {
                MovieDedupJobStatus.COMPLETED,
                MovieDedupJobStatus.PARTIAL_FAILED,
            }:
                break
            current = service.advance(
                job.id,
                scan_batch_size=1,
                match_batch_size=1,
                verify_batch_size=1,
            )
        assert current.status is MovieDedupJobStatus.COMPLETED
        assert current.phase is MovieDedupJobPhase.COMPLETED
        assert current.verified_count == 1
        assert (source_root / name).stat(follow_symlinks=False).st_ino == source_inode
        assert (target_root / name).stat(follow_symlinks=False).st_ino == target_inode
    finally:
        engine.dispose()
