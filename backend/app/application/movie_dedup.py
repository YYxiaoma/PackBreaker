from __future__ import annotations

import hashlib
import re
import stat
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.errors import ApplicationError
from backend.app.domain.movie_dedup import (
    MovieDedupCrossFilesystemPolicy,
    MovieDedupInventorySide,
    MovieDedupInventoryStatus,
    MovieDedupJobPhase,
    MovieDedupJobStatus,
    MovieDedupMode,
    MovieDedupPairStatus,
    MovieDedupResolvedAction,
    resolve_movie_dedup_action,
)
from backend.app.domain.task_definition import DEFAULT_VIDEO_EXTENSIONS
from backend.app.infrastructure.persistence.models import (
    MovieDedupFileInventory,
    MovieDedupJob,
    MovieDedupOperationJournal,
    MovieDedupPair,
    new_uuid,
    utc_now,
)
from backend.app.infrastructure.safe_filesystem import (
    MovieFileSnapshot,
    SafeFilesystemGateway,
)
from backend.app.infrastructure.source_inventory import scan_source_inventory_page


@dataclass(frozen=True, slots=True)
class MovieDedupJobCreate:
    name: str
    source_root: str
    target_root: str
    mode: MovieDedupMode = MovieDedupMode.AUTO
    cross_filesystem_policy: MovieDedupCrossFilesystemPolicy = MovieDedupCrossFilesystemPolicy.STOP
    include_subdirectories: bool = True
    min_size_bytes: int = 0
    video_extensions: tuple[str, ...] = DEFAULT_VIDEO_EXTENSIONS


@dataclass(frozen=True, slots=True)
class MovieDedupPrecheckView:
    source_root: str
    target_root: str
    source_device: int
    target_device: int
    same_filesystem: bool
    resolved_action: MovieDedupResolvedAction
    blocked_reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MovieDedupJobView:
    id: str
    name: str
    source_root: str
    target_root: str
    mode: MovieDedupMode
    cross_filesystem_policy: MovieDedupCrossFilesystemPolicy
    include_subdirectories: bool
    min_size_bytes: int
    video_extensions: tuple[str, ...]
    status: MovieDedupJobStatus
    phase: MovieDedupJobPhase
    source_scan_cursor: str | None
    target_scan_cursor: str | None
    source_file_count: int
    target_file_count: int
    candidate_count: int
    verified_count: int
    deduplicated_count: int
    failed_count: int
    logical_duplicate_bytes: int
    estimated_reclaimable_bytes: int
    trace_id: str
    version: int
    error_code: str | None
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None


@dataclass(frozen=True, slots=True)
class MovieDedupPairView:
    id: str
    source_relative_path: str
    target_relative_path: str
    source_media_metadata: dict[str, object]
    target_media_metadata: dict[str, object]
    source_size_bytes: int
    target_size_bytes: int
    source_device: int
    target_device: int
    source_inode: int
    target_inode: int
    source_link_count: int
    target_link_count: int
    source_mtime_ns: str
    target_mtime_ns: str
    source_sha256: str | None
    target_sha256: str | None
    metadata_match: bool
    size_match: bool
    quick_hash_match: bool
    full_hash_match: bool
    status: MovieDedupPairStatus
    resolved_action: MovieDedupResolvedAction
    estimated_reclaimable_bytes: int
    error_code: str | None
    error_message: str | None


class MovieDedupService:
    def __init__(self, session_factory: sessionmaker[Session], *, data_root: Path) -> None:
        self._session_factory = session_factory
        self._data_root = data_root
        self._filesystem = SafeFilesystemGateway(data_root)

    def precheck(
        self,
        *,
        source_root: str,
        target_root: str,
        mode: MovieDedupMode,
        cross_filesystem_policy: MovieDedupCrossFilesystemPolicy,
    ) -> MovieDedupPrecheckView:
        source_normalized, source = self._resolve_existing_directory(source_root, "保留目录 A")
        target_normalized, target = self._resolve_existing_directory(target_root, "去重目录 B")
        self._require_disjoint_roots(source, target)
        try:
            source_device = source.stat(follow_symlinks=False).st_dev
            target_device = target.stat(follow_symlinks=False).st_dev
        except OSError as exc:
            raise self._invalid("无法读取影片去重目录的文件系统信息") from exc
        same_filesystem = source_device == target_device
        action = resolve_movie_dedup_action(
            mode,
            cross_filesystem_policy,
            same_filesystem=same_filesystem,
        )
        blocked: list[str] = []
        if action is MovieDedupResolvedAction.BLOCKED:
            blocked.append("CROSS_DEVICE_LINK")
        return MovieDedupPrecheckView(
            source_root=source_normalized,
            target_root=target_normalized,
            source_device=source_device,
            target_device=target_device,
            same_filesystem=same_filesystem,
            resolved_action=action,
            blocked_reasons=tuple(blocked),
        )

    def create(self, request: MovieDedupJobCreate, *, trace_id: str) -> MovieDedupJobView:
        name = request.name.strip()
        if not name or len(name) > 120:
            raise self._invalid("任务名称不能为空且最长 120 个字符")
        if request.min_size_bytes < 0:
            raise self._invalid("最小文件大小不能小于 0")
        source_normalized, source = self._resolve_existing_directory(
            request.source_root, "保留目录 A"
        )
        target_normalized, target = self._resolve_existing_directory(
            request.target_root, "去重目录 B"
        )
        self._require_disjoint_roots(source, target)
        extensions = self._normalize_extensions(request.video_extensions)
        now = utc_now()
        record = MovieDedupJob(
            id=new_uuid(),
            name=name,
            source_root=source_normalized,
            target_root=target_normalized,
            mode=request.mode.value,
            cross_filesystem_policy=request.cross_filesystem_policy.value,
            include_subdirectories=request.include_subdirectories,
            min_size_bytes=request.min_size_bytes,
            video_extensions=list(extensions),
            status=MovieDedupJobStatus.PENDING.value,
            phase=MovieDedupJobPhase.PENDING.value,
            source_file_count=0,
            target_file_count=0,
            candidate_count=0,
            verified_count=0,
            deduplicated_count=0,
            failed_count=0,
            logical_duplicate_bytes=0,
            estimated_reclaimable_bytes=0,
            trace_id=trace_id,
            version=1,
            created_at=now,
            updated_at=now,
        )
        with self._session_factory() as session:
            session.add(record)
            session.commit()
            session.refresh(record)
            return self._view(record)

    def list(self, *, limit: int = 100) -> tuple[MovieDedupJobView, ...]:
        with self._session_factory() as session:
            records = session.scalars(
                select(MovieDedupJob)
                .order_by(MovieDedupJob.created_at.desc(), MovieDedupJob.id.desc())
                .limit(limit)
            ).all()
            return tuple(self._view(record) for record in records)

    def get(self, job_id: str) -> MovieDedupJobView:
        with self._session_factory() as session:
            record = session.get(MovieDedupJob, job_id)
            if record is None:
                raise ApplicationError(
                    code="MOVIE_DEDUP_JOB_NOT_FOUND",
                    status=404,
                    title="影片去重任务不存在",
                    detail="未找到指定影片去重任务",
                )
            return self._view(record)

    def start(self, job_id: str) -> MovieDedupJobView:
        with self._session_factory() as session:
            record = self._require_job(session, job_id)
            if record.status == MovieDedupJobStatus.PENDING.value:
                record.status = MovieDedupJobStatus.RUNNING.value
                record.phase = MovieDedupJobPhase.SCANNING_SOURCE.value
                record.updated_at = utc_now()
                record.version += 1
                session.commit()
            elif record.status not in {
                MovieDedupJobStatus.RUNNING.value,
                MovieDedupJobStatus.REVIEW_REQUIRED.value,
            }:
                raise ApplicationError(
                    code="MOVIE_DEDUP_STATE_INVALID",
                    status=409,
                    title="影片去重任务状态不可启动",
                    detail=f"当前状态 {record.status} 不允许开始扫描",
                )
            session.refresh(record)
            return self._view(record)

    def advance(
        self,
        job_id: str,
        *,
        scan_batch_size: int = 250,
        match_batch_size: int = 100,
        verify_batch_size: int = 2,
    ) -> MovieDedupJobView:
        if scan_batch_size <= 0 or match_batch_size <= 0 or verify_batch_size <= 0:
            raise ValueError("影片去重批次大小必须大于 0")
        with self._session_factory() as session:
            record = self._require_job(session, job_id)
            if record.status == MovieDedupJobStatus.PENDING.value:
                record.status = MovieDedupJobStatus.RUNNING.value
                record.phase = MovieDedupJobPhase.SCANNING_SOURCE.value
            if record.status != MovieDedupJobStatus.RUNNING.value:
                return self._view(record)

            phase = MovieDedupJobPhase(record.phase)
            if phase is MovieDedupJobPhase.SCANNING_SOURCE:
                self._scan_step(
                    session,
                    record,
                    side=MovieDedupInventorySide.SOURCE,
                    batch_size=scan_batch_size,
                )
            elif phase is MovieDedupJobPhase.SCANNING_TARGET:
                self._scan_step(
                    session,
                    record,
                    side=MovieDedupInventorySide.TARGET,
                    batch_size=scan_batch_size,
                )
            elif phase is MovieDedupJobPhase.MATCHING:
                self._match_step(session, record, batch_size=match_batch_size)
            elif phase is MovieDedupJobPhase.VERIFYING:
                self._verify_step(session, record, batch_size=verify_batch_size)
            record.updated_at = utc_now()
            session.commit()
            session.refresh(record)
            return self._view(record)

    def list_runnable_job_ids(self, *, limit: int = 8) -> tuple[str, ...]:
        with self._session_factory() as session:
            return tuple(
                session.scalars(
                    select(MovieDedupJob.id)
                    .where(MovieDedupJob.status == MovieDedupJobStatus.RUNNING.value)
                    .order_by(MovieDedupJob.updated_at, MovieDedupJob.id)
                    .limit(limit)
                )
            )

    def reconcile_job_operations(self, job_id: str, *, limit: int = 1) -> bool:
        with self._session_factory() as session:
            job = self._require_job(session, job_id)
            journals = tuple(
                session.scalars(
                    select(MovieDedupOperationJournal)
                    .where(
                        MovieDedupOperationJournal.job_id == job_id,
                        MovieDedupOperationJournal.status.in_(
                            ("INTENT_RECORDED", "TEMP_LINK_CREATED", "EXCHANGED", "VERIFIED")
                        ),
                    )
                    .order_by(MovieDedupOperationJournal.created_at, MovieDedupOperationJournal.id)
                    .limit(limit)
                )
            )
            if not journals:
                if job.phase == MovieDedupJobPhase.EXECUTING.value:
                    self._refresh_execution_summary(session, job)
                    session.commit()
                    return True
                return False
            resume = tuple((journal.id, journal.pair_id) for journal in journals)

        for journal_id, pair_id in resume:
            try:
                self._execute_pair(
                    job_id,
                    pair_id,
                    request_idempotency_key=f"recovery-{journal_id}",
                )
            except Exception as exc:
                with self._session_factory() as session:
                    journal = session.get(MovieDedupOperationJournal, journal_id)
                    pair = session.get(MovieDedupPair, pair_id)
                    job = self._require_job(session, job_id)
                    if journal is not None:
                        journal.status = "RECOVERY_REQUIRED"
                        journal.updated_at = utc_now()
                    if pair is not None:
                        pair.status = MovieDedupPairStatus.FAILED.value
                        pair.error_code = (
                            getattr(getattr(exc, "code", None), "value", None)
                            or "MOVIE_DEDUP_RECOVERY_REQUIRED"
                        )
                        pair.error_message = str(exc)
                        pair.updated_at = utc_now()
                    job.status = MovieDedupJobStatus.RECOVERY_REQUIRED.value
                    job.error_code = "MOVIE_DEDUP_RECOVERY_REQUIRED"
                    job.updated_at = utc_now()
                    job.version += 1
                    session.commit()
                return True

        with self._session_factory() as session:
            job = self._require_job(session, job_id)
            self._refresh_execution_summary(session, job)
            session.commit()
        return True

    def list_pairs(
        self,
        job_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[MovieDedupPairView, ...]:
        with self._session_factory() as session:
            self._require_job(session, job_id)
            pairs = tuple(
                session.scalars(
                    select(MovieDedupPair)
                    .where(MovieDedupPair.job_id == job_id)
                    .order_by(MovieDedupPair.created_at, MovieDedupPair.id)
                    .offset(offset)
                    .limit(limit)
                )
            )
            result: list[MovieDedupPairView] = []
            for pair in pairs:
                source = session.get(MovieDedupFileInventory, pair.source_file_id)
                target = session.get(MovieDedupFileInventory, pair.target_file_id)
                if source is None or target is None:
                    continue
                result.append(self._pair_view(pair, source, target))
            return tuple(result)

    def execute_pairs(
        self,
        job_id: str,
        *,
        pair_ids: tuple[str, ...],
        idempotency_key: str,
    ) -> MovieDedupJobView:
        normalized_ids = tuple(dict.fromkeys(item.strip() for item in pair_ids if item.strip()))
        if not normalized_ids:
            raise self._invalid("至少需要选择一个影片去重候选")
        if not idempotency_key or len(idempotency_key) > 200:
            raise self._invalid("影片去重执行需要有效的 Idempotency-Key")
        with self._session_factory() as session:
            selected_pairs = tuple(
                session.scalars(
                    select(MovieDedupPair).where(
                        MovieDedupPair.job_id == job_id,
                        MovieDedupPair.id.in_(normalized_ids),
                    )
                )
            )
            if len(selected_pairs) != len(normalized_ids):
                raise ApplicationError(
                    code="MOVIE_DEDUP_PAIR_NOT_FOUND",
                    status=404,
                    title="影片去重候选不存在",
                    detail="至少一个选中的影片去重候选不存在",
                )
            target_ids = [pair.target_file_id for pair in selected_pairs]
            if len(target_ids) != len(set(target_ids)):
                raise ApplicationError(
                    code="MOVIE_DEDUP_TARGET_SELECTION_CONFLICT",
                    status=409,
                    title="同一 B 文件被重复选择",
                    detail="同一次执行中每个 B 文件只能选择一个 A 候选",
                )
        for pair_id in normalized_ids:
            self._execute_pair(job_id, pair_id, request_idempotency_key=idempotency_key)
        with self._session_factory() as session:
            job = self._require_job(session, job_id)
            self._refresh_execution_summary(session, job)
            session.commit()
            session.refresh(job)
            return self._view(job)

    def _execute_pair(
        self,
        job_id: str,
        pair_id: str,
        *,
        request_idempotency_key: str,
    ) -> None:
        journal_key = hashlib.sha256(
            f"movie-dedup:{job_id}:{pair_id}:{request_idempotency_key}".encode()
        ).hexdigest()
        with self._session_factory() as session:
            job = self._require_job(session, job_id)
            pair = session.get(MovieDedupPair, pair_id)
            if pair is None or pair.job_id != job_id:
                raise ApplicationError(
                    code="MOVIE_DEDUP_PAIR_NOT_FOUND",
                    status=404,
                    title="影片去重候选不存在",
                    detail="未找到指定影片去重候选",
                )
            if pair.status == MovieDedupPairStatus.COMPLETED.value:
                return
            if pair.status not in {
                MovieDedupPairStatus.VERIFIED_DUPLICATE.value,
                MovieDedupPairStatus.REVIEW_REQUIRED.value,
            }:
                raise ApplicationError(
                    code="MOVIE_DEDUP_PAIR_NOT_EXECUTABLE",
                    status=409,
                    title="影片去重候选不可执行",
                    detail=f"当前候选状态 {pair.status} 不允许替换文件",
                )
            if not pair.full_hash_match:
                raise ApplicationError(
                    code="MOVIE_DEDUP_FULL_HASH_REQUIRED",
                    status=409,
                    title="影片内容尚未完整验证",
                    detail="只有完整 SHA-256 一致的候选才能执行影片去重",
                )
            action = MovieDedupResolvedAction(pair.resolved_action)
            if action not in {MovieDedupResolvedAction.HARDLINK, MovieDedupResolvedAction.SYMLINK}:
                raise ApplicationError(
                    code="MOVIE_DEDUP_ACTION_BLOCKED",
                    status=409,
                    title="影片去重方式不可执行",
                    detail=f"当前候选执行方式为 {action.value}",
                )
            source = session.get(MovieDedupFileInventory, pair.source_file_id)
            target = session.get(MovieDedupFileInventory, pair.target_file_id)
            if source is None or target is None:
                raise ApplicationError(
                    code="MOVIE_DEDUP_INVENTORY_MISSING",
                    status=409,
                    title="影片去重文件清单缺失",
                    detail="执行前无法恢复 A/B 文件清单",
                )
            source_path = _join_relative(job.source_root, source.relative_path)
            target_path = _join_relative(job.target_root, target.relative_path)
            journal = session.scalar(
                select(MovieDedupOperationJournal)
                .where(
                    MovieDedupOperationJournal.pair_id == pair.id,
                    MovieDedupOperationJournal.status.in_(
                        ("INTENT_RECORDED", "TEMP_LINK_CREATED", "EXCHANGED", "VERIFIED")
                    ),
                )
                .order_by(MovieDedupOperationJournal.created_at.desc())
            )
            if journal is None:
                journal = session.scalar(
                    select(MovieDedupOperationJournal).where(
                        MovieDedupOperationJournal.idempotency_key == journal_key
                    )
                )
            if journal is not None and journal.pair_id != pair.id:
                raise ApplicationError(
                    code="MOVIE_DEDUP_IDEMPOTENCY_CONFLICT",
                    status=409,
                    title="影片去重幂等键冲突",
                    detail="相同执行幂等键已经用于其他候选",
                )
            if journal is None:
                source_current = self._filesystem.inspect_movie_file(relative_path=source_path)
                target_current = self._filesystem.inspect_movie_file(relative_path=target_path)
                _assert_inventory_current(source, source_current, "SOURCE_CHANGED")
                _assert_inventory_current(target, target_current, "TARGET_CHANGED")
                operation_token = hashlib.sha256(
                    f"{job.id}:{pair.id}:{action.value}".encode()
                ).hexdigest()
                journal = MovieDedupOperationJournal(
                    id=new_uuid(),
                    job_id=job.id,
                    pair_id=pair.id,
                    idempotency_key=journal_key,
                    operation_token=operation_token,
                    operation_type=f"MOVIE_DEDUP_{action.value}",
                    status="INTENT_RECORDED",
                    source_snapshot=_movie_snapshot_payload(source_current),
                    target_snapshot=_movie_snapshot_payload(target_current),
                    created_at=utc_now(),
                    updated_at=utc_now(),
                )
                pair.selected = True
                pair.updated_at = utc_now()
                job.status = MovieDedupJobStatus.RUNNING.value
                job.phase = MovieDedupJobPhase.EXECUTING.value
                session.add(journal)
                session.commit()
            if journal.status == "COMMITTED":
                pair = session.get(MovieDedupPair, pair_id)
                if pair is not None:
                    pair.status = MovieDedupPairStatus.COMPLETED.value
                    pair.executed_at = pair.executed_at or utc_now()
                    session.commit()
                return
            journal_id = journal.id
            operation_token = journal.operation_token
            source_expected = _movie_snapshot_from_payload(journal.source_snapshot)
            target_expected = _movie_snapshot_from_payload(journal.target_snapshot)

        if journal.status != "VERIFIED":
            if action is MovieDedupResolvedAction.HARDLINK:
                exchanged = self._filesystem.exchange_duplicate_with_hardlink(
                    source_relative_path=source_path,
                    target_relative_path=target_path,
                    expected_source_snapshot=source_expected.filesystem_snapshot(),
                    expected_target_snapshot=target_expected.filesystem_snapshot(),
                    operation_token=operation_token,
                )
            else:
                exchanged = self._filesystem.exchange_duplicate_with_symlink(
                    source_relative_path=source_path,
                    target_relative_path=target_path,
                    expected_source_snapshot=source_expected.filesystem_snapshot(),
                    expected_target_snapshot=target_expected.filesystem_snapshot(),
                    operation_token=operation_token,
                )
            with self._session_factory() as session:
                journal = session.get(MovieDedupOperationJournal, journal_id)
                if journal is None:
                    raise RuntimeError("影片去重 journal 在原子交换后丢失")
                journal.status = "EXCHANGED"
                journal.temporary_path = exchanged.temporary_relative_path
                journal.temporary_snapshot = exchanged.original_target_snapshot.to_payload()
                journal.after_snapshot = exchanged.target_snapshot.to_payload()
                journal.updated_at = utc_now()
                session.commit()

            self._verify_executed_target(
                action=action,
                source_path=source_path,
                target_path=target_path,
                source_expected=source_expected,
            )
            with self._session_factory() as session:
                journal = session.get(MovieDedupOperationJournal, journal_id)
                if journal is None:
                    raise RuntimeError("影片去重 journal 在最终校验前丢失")
                journal.status = "VERIFIED"
                journal.updated_at = utc_now()
                session.commit()

        with self._session_factory() as session:
            journal = session.get(MovieDedupOperationJournal, journal_id)
            if journal is None:
                raise RuntimeError("影片去重 journal 在清理前丢失")
            temporary_path = journal.temporary_path
            target_payload = journal.target_snapshot
        if temporary_path:
            self._filesystem.remove_exchanged_original_if_matches(
                temporary_relative_path=temporary_path,
                expected_original_snapshot=_movie_snapshot_from_payload(
                    target_payload
                ).filesystem_snapshot(),
            )
        self._verify_executed_target(
            action=action,
            source_path=source_path,
            target_path=target_path,
            source_expected=source_expected,
        )
        with self._session_factory() as session:
            journal = session.get(MovieDedupOperationJournal, journal_id)
            pair = session.get(MovieDedupPair, pair_id)
            if journal is None or pair is None:
                raise RuntimeError("影片去重提交阶段持久化记录缺失")
            journal.status = "COMMITTED"
            journal.updated_at = utc_now()
            pair.status = MovieDedupPairStatus.COMPLETED.value
            pair.executed_at = utc_now()
            pair.updated_at = utc_now()
            session.commit()

    def _verify_executed_target(
        self,
        *,
        action: MovieDedupResolvedAction,
        source_path: str,
        target_path: str,
        source_expected: MovieFileSnapshot,
    ) -> None:
        current_source = self._filesystem.inspect_movie_file(relative_path=source_path)
        source_changed = (
            current_source.device != source_expected.device
            or current_source.inode != source_expected.inode
            or current_source.size != source_expected.size
            or current_source.mtime_ns != source_expected.mtime_ns
            or (
                action is MovieDedupResolvedAction.SYMLINK
                and current_source.ctime_ns != source_expected.ctime_ns
            )
        )
        if source_changed:
            raise ApplicationError(
                code="SOURCE_CHANGED",
                status=409,
                title="影片去重源文件发生变化",
                detail="执行过程中保留目录 A 的文件发生变化",
            )
        if action is MovieDedupResolvedAction.HARDLINK:
            current_target = self._filesystem.inspect_movie_file(relative_path=target_path)
            if (
                current_target.device != current_source.device
                or current_target.inode != current_source.inode
            ):
                raise ApplicationError(
                    code="MOVIE_DEDUP_FINAL_VERIFY_FAILED",
                    status=409,
                    title="影片去重最终校验失败",
                    detail="B 路径没有与 A 共享 inode",
                )
        else:
            expected_link = str(self._data_root / source_path)
            actual_link = self._filesystem.inspect_symlink_target(relative_path=target_path)
            if actual_link != expected_link:
                raise ApplicationError(
                    code="MOVIE_DEDUP_FINAL_VERIFY_FAILED",
                    status=409,
                    title="影片去重最终校验失败",
                    detail="B 软链接没有指向预期 A 路径",
                )

    def _refresh_execution_summary(self, session: Session, job: MovieDedupJob) -> None:
        pairs = tuple(
            session.scalars(select(MovieDedupPair).where(MovieDedupPair.job_id == job.id))
        )
        completed_targets = {
            pair.target_file_id
            for pair in pairs
            if pair.status == MovieDedupPairStatus.COMPLETED.value
        }
        job.deduplicated_count = len(completed_targets)
        failed_targets = {
            pair.target_file_id
            for pair in pairs
            if pair.status == MovieDedupPairStatus.FAILED.value
        }
        job.failed_count = len(failed_targets)
        remaining = any(_pair_requires_execution(pair) for pair in pairs)
        blocked_duplicate = any(_pair_is_blocked_duplicate(pair) for pair in pairs)
        if remaining:
            job.status = MovieDedupJobStatus.REVIEW_REQUIRED.value
            job.phase = MovieDedupJobPhase.REVIEW.value
        else:
            job.status = (
                MovieDedupJobStatus.PARTIAL_FAILED.value
                if job.failed_count or blocked_duplicate
                else MovieDedupJobStatus.COMPLETED.value
            )
            job.phase = MovieDedupJobPhase.COMPLETED.value
            job.finished_at = utc_now()
        job.version += 1
        job.updated_at = utc_now()

    def _scan_step(
        self,
        session: Session,
        job: MovieDedupJob,
        *,
        side: MovieDedupInventorySide,
        batch_size: int,
    ) -> None:
        root_relative = (
            job.source_root if side is MovieDedupInventorySide.SOURCE else job.target_root
        )
        _, root = self._resolve_existing_directory(
            root_relative,
            "保留目录 A" if side is MovieDedupInventorySide.SOURCE else "去重目录 B",
        )
        cursor = (
            job.source_scan_cursor
            if side is MovieDedupInventorySide.SOURCE
            else job.target_scan_cursor
        )
        page = scan_source_inventory_page(root, after=cursor, limit=batch_size)
        allowed_extensions = frozenset(str(item).lower() for item in job.video_extensions)
        for candidate in page.candidates:
            if not job.include_subdirectories and "/" in candidate.relative_path:
                continue
            extension = Path(candidate.relative_path).suffix.lower()
            if extension not in allowed_extensions or candidate.length < job.min_size_bytes:
                continue
            full_relative = _join_relative(root_relative, candidate.relative_path)
            try:
                observed = self._filesystem.inspect_movie_file(relative_path=full_relative)
            except Exception:
                continue
            existing = session.scalar(
                select(MovieDedupFileInventory).where(
                    MovieDedupFileInventory.job_id == job.id,
                    MovieDedupFileInventory.side == side.value,
                    MovieDedupFileInventory.relative_path == candidate.relative_path,
                )
            )
            if existing is not None:
                continue
            session.add(
                MovieDedupFileInventory(
                    id=new_uuid(),
                    job_id=job.id,
                    side=side.value,
                    relative_path=candidate.relative_path,
                    basename=Path(candidate.relative_path).name,
                    extension=extension,
                    device=observed.device,
                    inode=observed.inode,
                    size_bytes=observed.size,
                    allocated_bytes=observed.allocated_bytes,
                    link_count=observed.link_count,
                    mtime_ns=observed.mtime_ns,
                    ctime_ns=observed.ctime_ns,
                    mode=observed.mode,
                    uid=observed.uid,
                    gid=observed.gid,
                    media_metadata=_parse_movie_metadata(Path(candidate.relative_path).name),
                    scan_status=MovieDedupInventoryStatus.DISCOVERED.value,
                    created_at=utc_now(),
                    updated_at=utc_now(),
                )
            )
        session.flush()
        count = int(
            session.scalar(
                select(func.count())
                .select_from(MovieDedupFileInventory)
                .where(
                    MovieDedupFileInventory.job_id == job.id,
                    MovieDedupFileInventory.side == side.value,
                )
            )
            or 0
        )
        if side is MovieDedupInventorySide.SOURCE:
            job.source_file_count = count
            job.source_scan_cursor = page.next_cursor if page.has_more else None
            if not page.has_more:
                job.phase = MovieDedupJobPhase.SCANNING_TARGET.value
        else:
            job.target_file_count = count
            job.target_scan_cursor = page.next_cursor if page.has_more else None
            if not page.has_more:
                job.phase = MovieDedupJobPhase.MATCHING.value

    def _match_step(self, session: Session, job: MovieDedupJob, *, batch_size: int) -> None:
        targets = tuple(
            session.scalars(
                select(MovieDedupFileInventory)
                .where(
                    MovieDedupFileInventory.job_id == job.id,
                    MovieDedupFileInventory.side == MovieDedupInventorySide.TARGET.value,
                    MovieDedupFileInventory.scan_status
                    == MovieDedupInventoryStatus.DISCOVERED.value,
                )
                .order_by(
                    MovieDedupFileInventory.relative_path,
                    MovieDedupFileInventory.id,
                )
                .limit(batch_size)
            )
        )
        if not targets:
            job.phase = MovieDedupJobPhase.VERIFYING.value
            return
        now = utc_now()
        for target in targets:
            sources = list(
                session.scalars(
                    select(MovieDedupFileInventory).where(
                        MovieDedupFileInventory.job_id == job.id,
                        MovieDedupFileInventory.side == MovieDedupInventorySide.SOURCE.value,
                        MovieDedupFileInventory.size_bytes == target.size_bytes,
                    )
                )
            )
            sources.sort(
                key=lambda item: (
                    item.relative_path != target.relative_path,
                    item.basename.casefold() != target.basename.casefold(),
                    item.relative_path.casefold(),
                )
            )
            if not sources:
                target.scan_status = MovieDedupInventoryStatus.SKIPPED.value
                target.updated_at = now
                continue
            for source in sources[:32]:
                existing = session.scalar(
                    select(MovieDedupPair).where(
                        MovieDedupPair.job_id == job.id,
                        MovieDedupPair.source_file_id == source.id,
                        MovieDedupPair.target_file_id == target.id,
                    )
                )
                if existing is not None:
                    continue
                metadata_match = _metadata_matches(source.media_metadata, target.media_metadata)
                session.add(
                    MovieDedupPair(
                        id=new_uuid(),
                        job_id=job.id,
                        source_file_id=source.id,
                        target_file_id=target.id,
                        match_level="SIZE_AND_METADATA" if metadata_match else "SIZE",
                        status=MovieDedupPairStatus.CANDIDATE.value,
                        metadata_match=metadata_match,
                        size_match=True,
                        quick_hash_match=False,
                        full_hash_match=False,
                        selected=False,
                        resolved_action=MovieDedupResolvedAction.SCAN_ONLY.value,
                        estimated_reclaimable_bytes=0,
                        created_at=now,
                        updated_at=now,
                    )
                )
            target.scan_status = MovieDedupInventoryStatus.MATCHED.value
            target.updated_at = now
        session.flush()
        job.candidate_count = int(
            session.scalar(
                select(func.count())
                .select_from(MovieDedupPair)
                .where(MovieDedupPair.job_id == job.id)
            )
            or 0
        )

    def _verify_step(self, session: Session, job: MovieDedupJob, *, batch_size: int) -> None:
        pairs = tuple(
            session.scalars(
                select(MovieDedupPair)
                .where(
                    MovieDedupPair.job_id == job.id,
                    MovieDedupPair.status == MovieDedupPairStatus.CANDIDATE.value,
                )
                .order_by(MovieDedupPair.created_at, MovieDedupPair.id)
                .limit(batch_size)
            )
        )
        if not pairs:
            self._finalize_verification(session, job)
            return
        for pair in pairs:
            source = session.get(MovieDedupFileInventory, pair.source_file_id)
            target = session.get(MovieDedupFileInventory, pair.target_file_id)
            if source is None or target is None:
                pair.status = MovieDedupPairStatus.FAILED.value
                pair.error_code = "MOVIE_DEDUP_INVENTORY_MISSING"
                pair.error_message = "候选文件清单已缺失"
                pair.updated_at = utc_now()
                continue
            self._verify_pair(session, job, pair, source, target)

    def _verify_pair(
        self,
        session: Session,
        job: MovieDedupJob,
        pair: MovieDedupPair,
        source: MovieDedupFileInventory,
        target: MovieDedupFileInventory,
    ) -> None:
        source_path = _join_relative(job.source_root, source.relative_path)
        target_path = _join_relative(job.target_root, target.relative_path)
        source_expected = _movie_snapshot_from_inventory(source)
        target_expected = _movie_snapshot_from_inventory(target)
        now = utc_now()
        try:
            source_quick = (
                source.quick_fingerprint
                or self._cached_quick_fingerprint(session, source)
                or self._filesystem.movie_quick_fingerprint(
                    relative_path=source_path,
                    expected_snapshot=source_expected,
                )
            )
            source.quick_fingerprint = source_quick
            session.flush()
            target_quick = (
                target.quick_fingerprint
                or self._cached_quick_fingerprint(session, target)
                or self._filesystem.movie_quick_fingerprint(
                    relative_path=target_path,
                    expected_snapshot=target_expected,
                )
            )
            target.quick_fingerprint = target_quick
            source.scan_status = MovieDedupInventoryStatus.QUICK_HASHED.value
            target.scan_status = MovieDedupInventoryStatus.QUICK_HASHED.value
            pair.quick_hash_match = source_quick == target_quick
            if not pair.quick_hash_match:
                pair.status = MovieDedupPairStatus.BLOCKED.value
                pair.error_code = "MOVIE_CONTENT_DIFFERENT"
                pair.error_message = "快速内容指纹不同"
                pair.updated_at = now
                return

            source_hash = (
                source.full_sha256
                or self._cached_full_sha256(session, source)
                or self._filesystem.movie_full_sha256(
                    relative_path=source_path,
                    expected_snapshot=source_expected,
                )
            )
            source.full_sha256 = source_hash
            session.flush()
            target_hash = (
                target.full_sha256
                or self._cached_full_sha256(session, target)
                or self._filesystem.movie_full_sha256(
                    relative_path=target_path,
                    expected_snapshot=target_expected,
                )
            )
            target.full_sha256 = target_hash
            source.scan_status = MovieDedupInventoryStatus.FULL_HASHED.value
            target.scan_status = MovieDedupInventoryStatus.FULL_HASHED.value
            pair.full_hash_match = source_hash == target_hash
            if not pair.full_hash_match:
                pair.status = MovieDedupPairStatus.BLOCKED.value
                pair.error_code = "MOVIE_CONTENT_DIFFERENT"
                pair.error_message = "完整 SHA-256 不一致"
                pair.updated_at = now
                return

            if source.device == target.device and source.inode == target.inode:
                pair.status = MovieDedupPairStatus.ALREADY_DEDUPLICATED.value
                pair.resolved_action = MovieDedupResolvedAction.SCAN_ONLY.value
                pair.estimated_reclaimable_bytes = 0
                pair.verified_at = now
                pair.updated_at = now
                return

            action = resolve_movie_dedup_action(
                MovieDedupMode(job.mode),
                MovieDedupCrossFilesystemPolicy(job.cross_filesystem_policy),
                same_filesystem=source.device == target.device,
            )
            pair.resolved_action = action.value
            pair.estimated_reclaimable_bytes = (
                target.allocated_bytes if target.link_count == 1 else 0
            )
            pair.verified_at = now
            pair.metadata_match = _metadata_matches(source.media_metadata, target.media_metadata)
            if action is MovieDedupResolvedAction.BLOCKED:
                pair.status = MovieDedupPairStatus.BLOCKED.value
                pair.error_code = "CROSS_DEVICE_LINK"
                pair.error_message = "A/B 不在同一文件系统，且未授权软链接 fallback"
            elif (
                _metadata_complete(source.media_metadata)
                and _metadata_complete(target.media_metadata)
                and pair.metadata_match
            ):
                pair.status = MovieDedupPairStatus.VERIFIED_DUPLICATE.value
                pair.error_code = None
                pair.error_message = None
            else:
                pair.status = MovieDedupPairStatus.REVIEW_REQUIRED.value
                pair.error_code = "MOVIE_METADATA_REVIEW_REQUIRED"
                pair.error_message = "完整内容一致，但影片元数据不足或不一致，需要人工确认"
            pair.updated_at = now
        except Exception as exc:
            pair.status = MovieDedupPairStatus.FAILED.value
            pair.error_code = (
                getattr(getattr(exc, "code", None), "value", None) or "MOVIE_VERIFY_FAILED"
            )
            pair.error_message = str(exc)
            pair.updated_at = now

    @staticmethod
    def _cached_quick_fingerprint(session: Session, record: MovieDedupFileInventory) -> str | None:
        return session.scalar(
            select(MovieDedupFileInventory.quick_fingerprint)
            .where(
                MovieDedupFileInventory.job_id == record.job_id,
                MovieDedupFileInventory.device == record.device,
                MovieDedupFileInventory.inode == record.inode,
                MovieDedupFileInventory.size_bytes == record.size_bytes,
                MovieDedupFileInventory.mtime_ns == record.mtime_ns,
                MovieDedupFileInventory.ctime_ns == record.ctime_ns,
                MovieDedupFileInventory.quick_fingerprint.is_not(None),
            )
            .limit(1)
        )

    @staticmethod
    def _cached_full_sha256(session: Session, record: MovieDedupFileInventory) -> str | None:
        return session.scalar(
            select(MovieDedupFileInventory.full_sha256)
            .where(
                MovieDedupFileInventory.job_id == record.job_id,
                MovieDedupFileInventory.device == record.device,
                MovieDedupFileInventory.inode == record.inode,
                MovieDedupFileInventory.size_bytes == record.size_bytes,
                MovieDedupFileInventory.mtime_ns == record.mtime_ns,
                MovieDedupFileInventory.ctime_ns == record.ctime_ns,
                MovieDedupFileInventory.full_sha256.is_not(None),
            )
            .limit(1)
        )

    def _finalize_verification(self, session: Session, job: MovieDedupJob) -> None:
        pairs = tuple(
            session.scalars(select(MovieDedupPair).where(MovieDedupPair.job_id == job.id))
        )
        unique_targets: dict[str, MovieDedupFileInventory] = {}
        failed_targets: set[str] = set()
        for pair in pairs:
            if pair.full_hash_match:
                target = session.get(MovieDedupFileInventory, pair.target_file_id)
                if target is not None:
                    unique_targets.setdefault(target.id, target)
            if pair.status == MovieDedupPairStatus.FAILED.value:
                failed_targets.add(pair.target_file_id)
        job.verified_count = len(unique_targets)
        job.failed_count = len(failed_targets)
        job.logical_duplicate_bytes = sum(item.size_bytes for item in unique_targets.values())
        job.estimated_reclaimable_bytes = sum(
            item.allocated_bytes for item in unique_targets.values() if item.link_count == 1
        )
        requires_execution = any(_pair_requires_execution(pair) for pair in pairs)
        blocked_duplicate = any(_pair_is_blocked_duplicate(pair) for pair in pairs)
        if requires_execution:
            job.phase = MovieDedupJobPhase.REVIEW.value
            job.status = MovieDedupJobStatus.REVIEW_REQUIRED.value
        else:
            job.phase = MovieDedupJobPhase.COMPLETED.value
            job.status = (
                MovieDedupJobStatus.PARTIAL_FAILED.value
                if failed_targets or blocked_duplicate
                else MovieDedupJobStatus.COMPLETED.value
            )
            job.finished_at = utc_now()
        job.version += 1
        job.updated_at = utc_now()

    @staticmethod
    def _require_job(session: Session, job_id: str) -> MovieDedupJob:
        record = session.get(MovieDedupJob, job_id)
        if record is None:
            raise ApplicationError(
                code="MOVIE_DEDUP_JOB_NOT_FOUND",
                status=404,
                title="影片去重任务不存在",
                detail="未找到指定影片去重任务",
            )
        return record

    def _resolve_existing_directory(self, value: str, field_name: str) -> tuple[str, Path]:
        normalized = self._normalize_relative_path(value, field_name)
        try:
            base_stat = self._data_root.stat(follow_symlinks=False)
            base = self._data_root.resolve(strict=True)
        except OSError as exc:
            raise self._invalid("授权数据目录不可用") from exc
        if stat.S_ISLNK(base_stat.st_mode) or not stat.S_ISDIR(base_stat.st_mode):
            raise self._invalid("授权数据目录必须是真实目录且不能是符号链接")
        current = self._data_root
        parts = () if normalized == "." else tuple(normalized.split("/"))
        for part in parts:
            current = current / part
            try:
                item_stat = current.stat(follow_symlinks=False)
            except OSError as exc:
                raise self._invalid(f"{field_name}不存在或不可读取") from exc
            if stat.S_ISLNK(item_stat.st_mode) or not stat.S_ISDIR(item_stat.st_mode):
                raise self._invalid(f"{field_name}不能经过符号链接或非目录路径")
        resolved = current.resolve(strict=True)
        if not resolved.is_relative_to(base):
            raise self._invalid(f"{field_name}越过 PackBreaker 授权数据目录")
        return normalized, resolved

    @staticmethod
    def _require_disjoint_roots(source: Path, target: Path) -> None:
        if source == target or source in target.parents or target in source.parents:
            raise MovieDedupService._invalid(
                "保留目录 A 与去重目录 B 不能相同或互相包含，避免扫描与替换范围重叠"
            )

    @staticmethod
    def _normalize_relative_path(value: str, field_name: str) -> str:
        normalized = value.strip().replace("\\", "/")
        if not normalized:
            raise MovieDedupService._invalid(f"{field_name}不能为空")
        path = PurePosixPath(normalized)
        if path.is_absolute() or ".." in path.parts or "\x00" in normalized:
            raise MovieDedupService._invalid(f"{field_name}必须使用授权数据目录内的相对路径")
        result = path.as_posix()
        return "." if result in {"", "."} else result

    @staticmethod
    def _normalize_extensions(values: tuple[str, ...]) -> tuple[str, ...]:
        normalized: list[str] = []
        for raw in values:
            value = raw.strip().lower()
            if not value:
                continue
            if not value.startswith("."):
                value = f".{value}"
            if len(value) > 32 or "/" in value or "\\" in value or "\x00" in value:
                raise MovieDedupService._invalid("视频扩展名格式无效")
            normalized.append(value)
        result = tuple(dict.fromkeys(normalized))
        if not result:
            raise MovieDedupService._invalid("至少需要一个视频扩展名")
        return result

    @staticmethod
    def _view(record: MovieDedupJob) -> MovieDedupJobView:
        return MovieDedupJobView(
            id=record.id,
            name=record.name,
            source_root=record.source_root,
            target_root=record.target_root,
            mode=MovieDedupMode(record.mode),
            cross_filesystem_policy=MovieDedupCrossFilesystemPolicy(record.cross_filesystem_policy),
            include_subdirectories=record.include_subdirectories,
            min_size_bytes=record.min_size_bytes,
            video_extensions=tuple(record.video_extensions),
            status=MovieDedupJobStatus(record.status),
            phase=MovieDedupJobPhase(record.phase),
            source_scan_cursor=record.source_scan_cursor,
            target_scan_cursor=record.target_scan_cursor,
            source_file_count=record.source_file_count,
            target_file_count=record.target_file_count,
            candidate_count=record.candidate_count,
            verified_count=record.verified_count,
            deduplicated_count=record.deduplicated_count,
            failed_count=record.failed_count,
            logical_duplicate_bytes=record.logical_duplicate_bytes,
            estimated_reclaimable_bytes=record.estimated_reclaimable_bytes,
            trace_id=record.trace_id,
            version=record.version,
            error_code=record.error_code,
            created_at=record.created_at,
            updated_at=record.updated_at,
            finished_at=record.finished_at,
        )

    @staticmethod
    def _pair_view(
        pair: MovieDedupPair,
        source: MovieDedupFileInventory,
        target: MovieDedupFileInventory,
    ) -> MovieDedupPairView:
        return MovieDedupPairView(
            id=pair.id,
            source_relative_path=source.relative_path,
            target_relative_path=target.relative_path,
            source_media_metadata=dict(source.media_metadata),
            target_media_metadata=dict(target.media_metadata),
            source_size_bytes=source.size_bytes,
            target_size_bytes=target.size_bytes,
            source_device=source.device,
            target_device=target.device,
            source_inode=source.inode,
            target_inode=target.inode,
            source_link_count=source.link_count,
            target_link_count=target.link_count,
            source_mtime_ns=str(source.mtime_ns),
            target_mtime_ns=str(target.mtime_ns),
            source_sha256=source.full_sha256,
            target_sha256=target.full_sha256,
            metadata_match=pair.metadata_match,
            size_match=pair.size_match,
            quick_hash_match=pair.quick_hash_match,
            full_hash_match=pair.full_hash_match,
            status=MovieDedupPairStatus(pair.status),
            resolved_action=MovieDedupResolvedAction(pair.resolved_action),
            estimated_reclaimable_bytes=pair.estimated_reclaimable_bytes,
            error_code=pair.error_code,
            error_message=pair.error_message,
        )

    @staticmethod
    def _invalid(detail: str) -> ApplicationError:
        return ApplicationError(
            code="MOVIE_DEDUP_INPUT_INVALID",
            status=422,
            title="影片去重配置无效",
            detail=detail,
        )


def _join_relative(root: str, child: str) -> str:
    return child if root == "." else f"{root}/{child}"


def _movie_snapshot_from_inventory(record: MovieDedupFileInventory) -> MovieFileSnapshot:
    return MovieFileSnapshot(
        device=record.device,
        inode=record.inode,
        size=record.size_bytes,
        allocated_bytes=record.allocated_bytes,
        mtime_ns=record.mtime_ns,
        ctime_ns=record.ctime_ns,
        link_count=record.link_count,
        mode=record.mode,
        uid=record.uid,
        gid=record.gid,
    )


_RESOLUTION_RE = re.compile(r"(?i)(?<!\d)(2160p|1080p|1080i|720p|576p|480p)(?!\d)")
_YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
_GROUP_RE = re.compile(r"-([A-Za-z0-9][A-Za-z0-9._]{1,40})$")


def _parse_movie_metadata(filename: str) -> dict[str, str]:
    stem = Path(filename).stem
    resolution = _RESOLUTION_RE.search(stem)
    year = _YEAR_RE.search(stem)
    group = _GROUP_RE.search(stem)
    return {
        **({"resolution": resolution.group(1).lower()} if resolution else {}),
        **({"year": year.group(1)} if year else {}),
        **({"release_group": group.group(1).lower()} if group else {}),
    }


def _metadata_complete(metadata: dict[str, object]) -> bool:
    return bool(metadata.get("resolution") and metadata.get("release_group"))


def _metadata_matches(left: dict[str, object], right: dict[str, object]) -> bool:
    if not _metadata_complete(left) or not _metadata_complete(right):
        return False
    if left.get("resolution") != right.get("resolution"):
        return False
    if left.get("release_group") != right.get("release_group"):
        return False
    left_year = left.get("year")
    right_year = right.get("year")
    return not (left_year and right_year) or left_year == right_year


def _pair_requires_execution(pair: MovieDedupPair) -> bool:
    return pair.status in {
        MovieDedupPairStatus.VERIFIED_DUPLICATE.value,
        MovieDedupPairStatus.REVIEW_REQUIRED.value,
    } and pair.resolved_action in {
        MovieDedupResolvedAction.HARDLINK.value,
        MovieDedupResolvedAction.SYMLINK.value,
    }


def _pair_is_blocked_duplicate(pair: MovieDedupPair) -> bool:
    return pair.full_hash_match and pair.status == MovieDedupPairStatus.BLOCKED.value


def _movie_snapshot_payload(snapshot: MovieFileSnapshot) -> dict[str, int]:
    return {
        "device": snapshot.device,
        "inode": snapshot.inode,
        "size": snapshot.size,
        "allocated_bytes": snapshot.allocated_bytes,
        "mtime_ns": snapshot.mtime_ns,
        "ctime_ns": snapshot.ctime_ns,
        "link_count": snapshot.link_count,
        "mode": snapshot.mode,
        "uid": snapshot.uid,
        "gid": snapshot.gid,
    }


def _movie_snapshot_from_payload(payload: dict[str, object]) -> MovieFileSnapshot:
    return MovieFileSnapshot(
        device=_payload_int(payload, "device"),
        inode=_payload_int(payload, "inode"),
        size=_payload_int(payload, "size"),
        allocated_bytes=_payload_int(payload, "allocated_bytes"),
        mtime_ns=_payload_int(payload, "mtime_ns"),
        ctime_ns=_payload_int(payload, "ctime_ns"),
        link_count=_payload_int(payload, "link_count"),
        mode=_payload_int(payload, "mode"),
        uid=_payload_int(payload, "uid"),
        gid=_payload_int(payload, "gid"),
    )


def _payload_int(payload: dict[str, object], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeError("影片去重 journal 文件快照损坏")
    return value


def _assert_inventory_current(
    record: MovieDedupFileInventory,
    current: MovieFileSnapshot,
    error_code: str,
) -> None:
    expected = _movie_snapshot_from_inventory(record)
    if current.stable_identity() != expected.stable_identity():
        raise ApplicationError(
            code=error_code,
            status=409,
            title="影片文件发生变化",
            detail="扫描/验证后的文件身份已变化，禁止执行旧的去重计划",
        )
