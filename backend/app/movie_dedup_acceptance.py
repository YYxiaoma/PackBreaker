"""Container-runnable isolated real-filesystem acceptance for PackBreaker v1.0.7 movie dedup.

The script never scans existing media. It creates small synthetic files only inside
two uniquely named temporary directories below explicitly supplied parent directories,
runs the real MovieDedupService, verifies the resulting inode/symlink semantics, and
removes the fixtures on exit.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

from sqlalchemy import select

from backend.app.application.movie_dedup import MovieDedupJobCreate, MovieDedupService
from backend.app.domain.movie_dedup import (
    MovieDedupCrossFilesystemPolicy,
    MovieDedupJobStatus,
    MovieDedupMode,
    MovieDedupPairStatus,
    MovieDedupResolvedAction,
)
from backend.app.infrastructure.persistence.base import Base
from backend.app.infrastructure.persistence.database import (
    create_session_factory,
    create_sqlite_engine,
)
from backend.app.infrastructure.persistence.models import MovieDedupOperationJournal

_PREFIX = "packbreaker-movie-dedup-acceptance-"
_MOVIE_NAME = "PackBreaker.Acceptance.2026.2160p-GROUP.mkv"


def _require_safe_directory(path: Path, *, data_root: Path, label: str) -> Path:
    if not path.is_absolute():
        raise ValueError(f"{label} 必须使用绝对路径")
    if path.is_symlink() or not path.is_dir():
        raise ValueError(f"{label} 必须是已存在的真实目录，不能是符号链接")
    resolved = path.resolve(strict=True)
    try:
        resolved.relative_to(data_root)
    except ValueError as exc:
        raise ValueError(f"{label} 必须位于 data-root 内") from exc
    return resolved


def _relative(root: Path, path: Path) -> str:
    value = path.relative_to(root).as_posix()
    return value or "."


def _drive_until_settled(service: MovieDedupService, job_id: str) -> None:
    for _ in range(80):
        current = service.get(job_id)
        if current.status in {
            MovieDedupJobStatus.REVIEW_REQUIRED,
            MovieDedupJobStatus.COMPLETED,
            MovieDedupJobStatus.PARTIAL_FAILED,
            MovieDedupJobStatus.FAILED,
            MovieDedupJobStatus.RECOVERY_REQUIRED,
        }:
            return
        service.advance(
            job_id,
            scan_batch_size=16,
            match_batch_size=16,
            verify_batch_size=2,
        )
    raise RuntimeError("影片去重隔离验收未在有界步数内收敛")


def _create_job(
    service: MovieDedupService,
    *,
    name: str,
    source_root: str,
    target_root: str,
    mode: MovieDedupMode,
    cross_policy: MovieDedupCrossFilesystemPolicy,
) -> str:
    created = service.create(
        MovieDedupJobCreate(
            name=name,
            source_root=source_root,
            target_root=target_root,
            mode=mode,
            cross_filesystem_policy=cross_policy,
            include_subdirectories=True,
            min_size_bytes=0,
            video_extensions=(".mkv",),
        ),
        trace_id="movie-dedup-isolated-acceptance",
    )
    service.start(created.id)
    _drive_until_settled(service, created.id)
    return created.id


def run_acceptance(
    *,
    data_root: Path,
    source_parent: Path,
    target_parent: Path,
) -> dict[str, object]:
    root = data_root.resolve(strict=True)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("data-root 必须是已存在的真实目录，不能是符号链接")
    source_parent = _require_safe_directory(source_parent, data_root=root, label="source-parent")
    target_parent = _require_safe_directory(target_parent, data_root=root, label="target-parent")

    same_filesystem = os.stat(source_parent).st_dev == os.stat(target_parent).st_dev
    outcomes: dict[str, object] = {
        "same_filesystem": same_filesystem,
        "source_device": os.stat(source_parent).st_dev,
        "target_device": os.stat(target_parent).st_dev,
    }

    with (
        tempfile.TemporaryDirectory(prefix=_PREFIX, dir=source_parent) as source_tmp,
        tempfile.TemporaryDirectory(prefix=_PREFIX, dir=target_parent) as target_tmp,
        tempfile.TemporaryDirectory(prefix="packbreaker-movie-dedup-db-") as db_tmp,
    ):
        source_root = Path(source_tmp)
        target_root = Path(target_tmp)
        source_relative = _relative(root, source_root)
        target_relative = _relative(root, target_root)
        engine = create_sqlite_engine(Path(db_tmp) / "acceptance.db")
        Base.metadata.create_all(engine)
        factory = create_session_factory(engine)
        service = MovieDedupService(factory, data_root=root)
        try:
            content = (b"PACKBREAKER-MOVIE-DEDUP-E2E\n" * 8192) + b"END"
            source = source_root / _MOVIE_NAME
            target = target_root / _MOVIE_NAME
            source.write_bytes(content)
            target.write_bytes(content)
            source_before = source.stat(follow_symlinks=False)
            target_before = target.stat(follow_symlinks=False)
            if source_before.st_ino == target_before.st_ino and same_filesystem:
                raise RuntimeError("验收 fixture 初始状态意外已经共享 inode")

            stop_job_id = _create_job(
                service,
                name="影片去重隔离验收-AUTO-STOP",
                source_root=source_relative,
                target_root=target_relative,
                mode=MovieDedupMode.AUTO,
                cross_policy=MovieDedupCrossFilesystemPolicy.STOP,
            )
            stop_pairs = service.list_pairs(stop_job_id)
            matching = tuple(pair for pair in stop_pairs if pair.full_hash_match)
            if len(matching) != 1:
                raise RuntimeError("AUTO/STOP 验收未得到唯一完整 SHA-256 重复候选")
            stop_pair = matching[0]

            if same_filesystem:
                if stop_pair.resolved_action is not MovieDedupResolvedAction.HARDLINK:
                    raise RuntimeError("同文件系统 AUTO/STOP 未解析为 HARDLINK")
                service.execute_pairs(
                    stop_job_id,
                    pair_ids=(stop_pair.id,),
                    idempotency_key="acceptance-auto-stop-hardlink",
                )
                source_after = source.stat(follow_symlinks=False)
                target_after = target.stat(follow_symlinks=False)
                if source_after.st_ino != target_after.st_ino:
                    raise RuntimeError("同文件系统影片去重后 A/B 未共享 inode")
                with factory() as session:
                    journals = tuple(
                        session.scalars(
                            select(MovieDedupOperationJournal).where(
                                MovieDedupOperationJournal.job_id == stop_job_id
                            )
                        )
                    )
                if len(journals) != 1 or journals[0].status != "COMMITTED":
                    raise RuntimeError("同文件系统影片去重 journal 未提交")
                outcomes["auto_stop"] = "HARDLINK_COMMITTED"
                outcomes["hardlink_inode_equal"] = True
            else:
                if stop_pair.status is not MovieDedupPairStatus.BLOCKED:
                    raise RuntimeError("跨文件系统 AUTO/STOP 未阻断")
                if stop_pair.resolved_action is not MovieDedupResolvedAction.BLOCKED:
                    raise RuntimeError("跨文件系统 AUTO/STOP action 不是 BLOCKED")
                unchanged = target.stat(follow_symlinks=False)
                if unchanged.st_ino != target_before.st_ino or target.is_symlink():
                    raise RuntimeError("跨文件系统阻断场景不应修改 B")
                outcomes["auto_stop"] = "CROSS_DEVICE_BLOCKED"

                symlink_target = target_root / f"symlink-{_MOVIE_NAME}"
                symlink_source = source_root / f"symlink-{_MOVIE_NAME}"
                symlink_source.write_bytes(content)
                symlink_target.write_bytes(content)
                symlink_job_id = _create_job(
                    service,
                    name="影片去重隔离验收-AUTO-SYMLINK",
                    source_root=source_relative,
                    target_root=target_relative,
                    mode=MovieDedupMode.AUTO,
                    cross_policy=MovieDedupCrossFilesystemPolicy.SYMLINK,
                )
                symlink_pairs = service.list_pairs(symlink_job_id)
                pair = next(
                    (
                        item
                        for item in symlink_pairs
                        if item.target_relative_path == symlink_target.name
                        and item.full_hash_match
                        and item.resolved_action is MovieDedupResolvedAction.SYMLINK
                    ),
                    None,
                )
                if pair is None:
                    raise RuntimeError("跨文件系统未得到可执行 SYMLINK 候选")
                service.execute_pairs(
                    symlink_job_id,
                    pair_ids=(pair.id,),
                    idempotency_key="acceptance-auto-symlink",
                )
                if not symlink_target.is_symlink():
                    raise RuntimeError("跨文件系统软链接 fallback 后 B 不是 symlink")
                if symlink_target.read_bytes() != content:
                    raise RuntimeError("跨文件系统软链接 fallback 内容读取不一致")
                outcomes["auto_symlink"] = "SYMLINK_COMMITTED"

            negative_source = source_root / f"negative-{_MOVIE_NAME}"
            negative_target = target_root / f"negative-{_MOVIE_NAME}"
            negative_source.write_bytes(b"A" * 65536)
            negative_target.write_bytes(b"B" * 65536)
            negative_target_inode = negative_target.stat(follow_symlinks=False).st_ino
            negative_job_id = _create_job(
                service,
                name="影片去重隔离验收-不同内容",
                source_root=source_relative,
                target_root=target_relative,
                mode=MovieDedupMode.SCAN_ONLY,
                cross_policy=MovieDedupCrossFilesystemPolicy.STOP,
            )
            negative_pairs = service.list_pairs(negative_job_id)
            negative_pair = next(
                (
                    item
                    for item in negative_pairs
                    if item.target_relative_path == negative_target.name
                ),
                None,
            )
            if negative_pair is None or negative_pair.full_hash_match:
                raise RuntimeError("同大小不同内容负例没有被完整 SHA-256 正确排除")
            if negative_target.stat(follow_symlinks=False).st_ino != negative_target_inode:
                raise RuntimeError("负例验收不应修改 B")
            outcomes["same_size_different_content"] = "BLOCKED_NO_MUTATION"
            outcomes["fixture_cleanup"] = "PENDING_CONTEXT_EXIT"
        finally:
            engine.dispose()

    outcomes["fixture_cleanup"] = "COMPLETED"
    return outcomes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PackBreaker v1.0.7 影片去重隔离真实文件系统验收")
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--source-parent", type=Path, required=True)
    parser.add_argument("--target-parent", type=Path, required=True)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--acknowledge-synthetic-file-writes", action="store_true")
    args = parser.parse_args(argv)
    if not (args.live and args.acknowledge_synthetic_file_writes):
        print("未创建验收文件：须同时指定 --live 与 --acknowledge-synthetic-file-writes")
        return 2
    try:
        result = run_acceptance(
            data_root=args.data_root,
            source_parent=args.source_parent,
            target_parent=args.target_parent,
        )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "schema_version": "packbreaker-movie-dedup-acceptance-v1",
                    "status": "ERROR",
                    "error_type": type(exc).__name__,
                    "detail": str(exc),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 1
    print(
        json.dumps(
            {
                "schema_version": "packbreaker-movie-dedup-acceptance-v1",
                "status": "PASSED",
                **result,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
