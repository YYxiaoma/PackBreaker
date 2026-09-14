from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import func, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.errors import ApplicationError
from backend.app.domain.errors import DomainViolation
from backend.app.domain.history_scan import (
    HistoryMaterializationStatus,
    HistoryMediaKind,
    HistoryScanStatus,
    history_file_snapshot_digest,
)
from backend.app.domain.task_state import TaskStatus
from backend.app.domain.task_units import (
    EpisodeUnitMetadata,
    SourceTaskFile,
    TaskUnit,
    TaskUnitKind,
    episode_unit_metadata,
    identify_task_units,
)
from backend.app.domain.verification import FileSnapshot
from backend.app.infrastructure.history_scanner import HistoryFileSnapshot, HistoryFilesystemScanner
from backend.app.infrastructure.persistence.database import begin_immediate_write
from backend.app.infrastructure.persistence.models import (
    HistoryScan,
    HistoryScanFile,
    HistoryScanMaterialization,
    PreflightSnapshotRecord,
    UnpackTask,
    new_uuid,
    utc_now,
)
from backend.app.infrastructure.persistence.repositories import TaskCreate, TaskRepository
from backend.app.infrastructure.persistence.task_analysis_repositories import TaskUnitRepository
from backend.app.infrastructure.safe_filesystem import SafeFilesystemGateway
from backend.app.infrastructure.source_inventory import (
    current_file_snapshot,
    scan_source_inventory,
    source_inventory_digest,
)


@dataclass(frozen=True, slots=True)
class HistoryScanView:
    id: str
    root_relative_path: str
    media_kind: HistoryMediaKind
    extensions: tuple[str, ...]
    exclude_patterns: tuple[str, ...]
    status: HistoryScanStatus
    generation: int
    cursor: str | None
    discovered_count: int
    new_count: int
    changed_count: int
    unchanged_count: int
    version: int
    last_started_at: datetime | None
    last_completed_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class HistoryScanBatchResult:
    scan: HistoryScanView
    processed_count: int
    has_more: bool


@dataclass(frozen=True, slots=True)
class HistoryMaterializationItemView:
    materialization_id: str
    scan_file_id: str
    status: HistoryMaterializationStatus
    reason_code: str | None
    task_id: str | None
    task_created: bool
    source_root: str | None
    normalized_unit_key: str | None
    unit_kind: str | None


@dataclass(frozen=True, slots=True)
class HistoryMaterializeResult:
    scan: HistoryScanView
    processed_count: int
    task_created_count: int
    task_reused_count: int
    skipped_count: int
    remaining_count: int
    items: tuple[HistoryMaterializationItemView, ...]


@dataclass(frozen=True, slots=True)
class HistoryTaskResultView:
    materialization_id: str
    scan_file_id: str
    relative_path: str
    snapshot_digest: str
    materialization_status: HistoryMaterializationStatus
    reason_code: str | None
    task_id: str | None
    source_root: str | None
    normalized_unit_key: str | None
    unit_kind: str | None
    episode_kind: str | None
    episode_season: int | None
    episode_start: int | None
    episode_end: int | None
    episode_label: str | None
    episode_group_key: str | None
    episode_variant_key: str | None
    variant_count: int
    task_status: TaskStatus | None
    task_version: int | None
    task_error_code: str | None
    analysis_eligible: bool
    has_preflight: bool
    created_at: datetime


@dataclass(frozen=True, slots=True)
class HistoryTaskAnalysisTarget:
    task_id: str
    source_root: str


@dataclass(frozen=True, slots=True)
class _HistoryScanFileState:
    id: str
    relative_path: str
    device: int
    inode: int
    size: int
    mtime_ns: int
    snapshot_digest: str
    generation: int


@dataclass(frozen=True, slots=True)
class _HistoryMaterializationPlan:
    file: _HistoryScanFileState
    source_root: str
    source_relative_path: str
    source_inventory_digest: str
    unit: TaskUnit | None
    episode_metadata: EpisodeUnitMetadata | None
    reason_code: str | None


class HistoryScanService:
    def __init__(self, session_factory: sessionmaker[Session], *, data_root: Path) -> None:
        self._session_factory = session_factory
        self._data_root = data_root
        self._filesystem = SafeFilesystemGateway(data_root)
        self._scanner = HistoryFilesystemScanner(data_root)

    def list_scans(self) -> tuple[HistoryScanView, ...]:
        with self._session_factory() as session:
            records = tuple(
                session.scalars(select(HistoryScan).order_by(HistoryScan.created_at.desc()))
            )
            return tuple(self._view(record) for record in records)

    def list_scanning(self, *, limit: int) -> tuple[HistoryScanView, ...]:
        if limit < 1 or limit > 1000:
            raise ValueError("history scanning limit 必须位于 1..1000")
        with self._session_factory() as session:
            records = tuple(
                session.scalars(
                    select(HistoryScan)
                    .where(HistoryScan.status == HistoryScanStatus.SCANNING.value)
                    .order_by(HistoryScan.updated_at, HistoryScan.id)
                    .limit(limit)
                )
            )
            return tuple(self._view(record) for record in records)

    def get(self, scan_id: str) -> HistoryScanView:
        with self._session_factory() as session:
            return self._view(self._require_scan(session, scan_id))

    def list_task_results(
        self,
        scan_id: str,
        *,
        limit: int = 500,
        task_status: TaskStatus | None = None,
        materialization_status: HistoryMaterializationStatus | None = None,
        query: str | None = None,
    ) -> tuple[HistoryTaskResultView, ...]:
        if limit < 1 or limit > 1000:
            raise ValueError("history task result limit 必须位于 1..1000")
        normalized_query = query.strip().lower() if query is not None else ""
        if len(normalized_query) > 200:
            raise ValueError("history task result query 最长 200 字符")
        with self._session_factory() as session:
            self._require_scan(session, scan_id)
            statement = (
                select(HistoryScanMaterialization, HistoryScanFile, UnpackTask)
                .join(
                    HistoryScanFile,
                    HistoryScanFile.id == HistoryScanMaterialization.scan_file_id,
                )
                .outerjoin(UnpackTask, UnpackTask.id == HistoryScanMaterialization.task_id)
                .where(HistoryScanMaterialization.scan_id == scan_id)
            )
            if task_status is not None:
                statement = statement.where(UnpackTask.status == task_status.value)
            if materialization_status is not None:
                statement = statement.where(
                    HistoryScanMaterialization.status == materialization_status.value
                )
            if normalized_query:
                searchable = (
                    func.lower(HistoryScanFile.relative_path),
                    func.lower(func.coalesce(HistoryScanMaterialization.episode_label, "")),
                    func.lower(func.coalesce(HistoryScanMaterialization.episode_kind, "")),
                    func.lower(func.coalesce(HistoryScanMaterialization.task_id, "")),
                    func.lower(func.coalesce(UnpackTask.status, "")),
                    func.lower(func.coalesce(UnpackTask.error_code, "")),
                )
                statement = statement.where(
                    or_(
                        *(
                            expression.contains(normalized_query, autoescape=True)
                            for expression in searchable
                        )
                    )
                )
            rows = tuple(
                session.execute(
                    statement.order_by(
                        HistoryScanMaterialization.created_at.desc(),
                        HistoryScanMaterialization.id.desc(),
                    ).limit(limit)
                )
            )
            task_ids = tuple(
                materialization.task_id
                for materialization, _scan_file, _task in rows
                if materialization.task_id is not None
            )
            preflight_task_ids = (
                set(
                    session.scalars(
                        select(PreflightSnapshotRecord.task_id)
                        .where(PreflightSnapshotRecord.task_id.in_(task_ids))
                        .distinct()
                    )
                )
                if task_ids
                else set()
            )
            group_counts = {
                group_key: int(count)
                for group_key, count in session.execute(
                    select(
                        HistoryScanMaterialization.episode_group_key,
                        func.count(HistoryScanMaterialization.scan_file_id.distinct()),
                    )
                    .where(
                        HistoryScanMaterialization.scan_id == scan_id,
                        HistoryScanMaterialization.status
                        == HistoryMaterializationStatus.MATERIALIZED.value,
                        HistoryScanMaterialization.episode_group_key.is_not(None),
                    )
                    .group_by(HistoryScanMaterialization.episode_group_key)
                )
                if group_key is not None
            }
            results: list[HistoryTaskResultView] = []
            for materialization, scan_file, task in rows:
                task_status = TaskStatus(task.status) if task is not None else None
                results.append(
                    HistoryTaskResultView(
                        materialization_id=materialization.id,
                        scan_file_id=scan_file.id,
                        relative_path=scan_file.relative_path,
                        snapshot_digest=materialization.snapshot_digest,
                        materialization_status=HistoryMaterializationStatus(materialization.status),
                        reason_code=materialization.reason_code,
                        task_id=materialization.task_id,
                        source_root=materialization.source_root,
                        normalized_unit_key=materialization.normalized_unit_key,
                        unit_kind=materialization.unit_kind,
                        episode_kind=materialization.episode_kind,
                        episode_season=materialization.episode_season,
                        episode_start=materialization.episode_start,
                        episode_end=materialization.episode_end,
                        episode_label=materialization.episode_label,
                        episode_group_key=materialization.episode_group_key,
                        episode_variant_key=materialization.episode_variant_key,
                        variant_count=group_counts.get(materialization.episode_group_key, 0),
                        task_status=task_status,
                        task_version=task.version if task is not None else None,
                        task_error_code=task.error_code if task is not None else None,
                        analysis_eligible=task_status
                        in {TaskStatus.PENDING, TaskStatus.RETRY, TaskStatus.PAUSED},
                        has_preflight=task is not None and task.id in preflight_task_ids,
                        created_at=materialization.created_at,
                    )
                )
            return tuple(results)

    def analysis_targets(
        self,
        scan_id: str,
        *,
        task_ids: tuple[str, ...],
    ) -> tuple[HistoryTaskAnalysisTarget, ...]:
        normalized = tuple(dict.fromkeys(item.strip() for item in task_ids if item.strip()))
        if not normalized or len(normalized) > 10:
            raise ApplicationError(
                code="HISTORY_TASK_SELECTION_INVALID",
                status=422,
                title="历史任务选择无效",
                detail="批量分析必须选择 1 到 10 个历史任务",
            )
        with self._session_factory() as session:
            self._require_scan(session, scan_id)
            records = tuple(
                session.scalars(
                    select(HistoryScanMaterialization).where(
                        HistoryScanMaterialization.scan_id == scan_id,
                        HistoryScanMaterialization.status
                        == HistoryMaterializationStatus.MATERIALIZED.value,
                        HistoryScanMaterialization.task_id.in_(normalized),
                    )
                )
            )
            by_task_id = {
                record.task_id: record for record in records if record.task_id is not None
            }
            if set(by_task_id) != set(normalized):
                raise ApplicationError(
                    code="HISTORY_TASK_SELECTION_INVALID",
                    status=409,
                    title="历史任务选择无效",
                    detail="所选任务必须全部来自当前历史扫描的成功转换记录",
                )
            targets: list[HistoryTaskAnalysisTarget] = []
            for task_id in normalized:
                record = by_task_id[task_id]
                if record.source_root is None:
                    raise ApplicationError(
                        code="HISTORY_TASK_PROVENANCE_INVALID",
                        status=409,
                        title="历史任务来源证据无效",
                        detail="历史任务缺少服务端冻结的 source_root",
                    )
                targets.append(
                    HistoryTaskAnalysisTarget(task_id=task_id, source_root=record.source_root)
                )
            return tuple(targets)

    def create(
        self,
        *,
        root_path: str,
        media_kind: HistoryMediaKind,
        extensions: tuple[str, ...],
        exclude_patterns: tuple[str, ...],
    ) -> HistoryScanView:
        root_relative_path = self._normalize_root(root_path)
        normalized_extensions = self._normalize_extensions(extensions)
        normalized_excludes = self._normalize_excludes(exclude_patterns)
        try:
            self._filesystem.assert_directory(relative_path=root_relative_path)
        except DomainViolation as exc:
            raise ApplicationError(
                code="HISTORY_SCAN_ROOT_INVALID",
                status=422,
                title="历史扫描根目录无效",
                detail="扫描根目录必须是 /data 内已存在且不经过符号链接的目录",
            ) from exc
        now = utc_now()
        record = HistoryScan(
            id=new_uuid(),
            root_relative_path=root_relative_path,
            media_kind=media_kind.value,
            extensions=list(normalized_extensions),
            exclude_patterns=list(normalized_excludes),
            status=HistoryScanStatus.READY.value,
            generation=0,
            cursor=None,
            discovered_count=0,
            new_count=0,
            changed_count=0,
            unchanged_count=0,
            version=1,
            last_started_at=None,
            last_completed_at=None,
            created_at=now,
            updated_at=now,
        )
        with self._session_factory() as session:
            session.add(record)
            try:
                session.commit()
                session.refresh(record)
            except IntegrityError as exc:
                session.rollback()
                raise ApplicationError(
                    code="HISTORY_SCAN_EXISTS",
                    status=409,
                    title="历史扫描已存在",
                    detail="同一根目录和媒体类型只能保留一条扫描配置",
                ) from exc
        return self._view(record)

    def start(self, scan_id: str, *, expected_version: int) -> HistoryScanView:
        with self._session_factory() as session:
            record = self._require_scan(session, scan_id)
            if record.version != expected_version:
                raise self._version_conflict()
            if HistoryScanStatus(record.status) not in {
                HistoryScanStatus.READY,
                HistoryScanStatus.DONE,
                HistoryScanStatus.CANCELLED,
            }:
                raise self._state_conflict(
                    "只有 READY、DONE 或 CANCELLED 扫描可以开始新一轮增量扫描"
                )
            now = utc_now()
            result = cast(
                CursorResult[Any],
                session.execute(
                    update(HistoryScan)
                    .where(HistoryScan.id == scan_id, HistoryScan.version == expected_version)
                    .values(
                        status=HistoryScanStatus.SCANNING.value,
                        generation=record.generation + 1,
                        cursor=None,
                        discovered_count=0,
                        new_count=0,
                        changed_count=0,
                        unchanged_count=0,
                        version=expected_version + 1,
                        last_started_at=now,
                        last_completed_at=None,
                        updated_at=now,
                    )
                ),
            )
            if result.rowcount != 1:
                session.rollback()
                raise self._version_conflict()
            session.commit()
            return self._view(self._require_scan(session, scan_id))

    def pause(self, scan_id: str, *, expected_version: int) -> HistoryScanView:
        return self._set_status(
            scan_id,
            expected_version=expected_version,
            required=HistoryScanStatus.SCANNING,
            target=HistoryScanStatus.PAUSED,
        )

    def resume(self, scan_id: str, *, expected_version: int) -> HistoryScanView:
        return self._set_status(
            scan_id,
            expected_version=expected_version,
            required=HistoryScanStatus.PAUSED,
            target=HistoryScanStatus.SCANNING,
        )

    def cancel(self, scan_id: str, *, expected_version: int) -> HistoryScanView:
        with self._session_factory() as session:
            record = self._require_scan(session, scan_id)
            if record.version != expected_version:
                raise self._version_conflict()
            if HistoryScanStatus(record.status) not in {
                HistoryScanStatus.SCANNING,
                HistoryScanStatus.PAUSED,
            }:
                raise self._state_conflict("只有 SCANNING 或 PAUSED 扫描可以取消")
            now = utc_now()
            result = cast(
                CursorResult[Any],
                session.execute(
                    update(HistoryScan)
                    .where(HistoryScan.id == scan_id, HistoryScan.version == expected_version)
                    .values(
                        status=HistoryScanStatus.CANCELLED.value,
                        version=expected_version + 1,
                        updated_at=now,
                    )
                ),
            )
            if result.rowcount != 1:
                session.rollback()
                raise self._version_conflict()
            session.commit()
            return self._view(self._require_scan(session, scan_id))

    def scan_batch(
        self,
        scan_id: str,
        *,
        expected_version: int,
        limit: int,
    ) -> HistoryScanBatchResult:
        if limit < 1 or limit > 1000:
            raise ValueError("history scan batch limit 必须位于 1..1000")
        with self._session_factory() as session:
            record = self._require_scan(session, scan_id)
            if record.version != expected_version:
                raise self._version_conflict()
            if HistoryScanStatus(record.status) is not HistoryScanStatus.SCANNING:
                raise self._state_conflict("只有 SCANNING 扫描可以推进游标")
            generation = record.generation
            cursor = record.cursor
            root = record.root_relative_path
            extensions = tuple(record.extensions)
            excludes = tuple(record.exclude_patterns)

        try:
            page = self._scanner.scan_page(
                root_relative_path=root,
                extensions=extensions,
                exclude_patterns=excludes,
                after=cursor,
                limit=limit,
            )
        except OSError as exc:
            raise ApplicationError(
                code="HISTORY_SCAN_FILESYSTEM_CHANGED",
                status=409,
                title="历史扫描目录发生变化",
                detail="扫描期间目录不可安全读取，请刷新后重试当前批次",
            ) from exc
        batch = page.snapshots
        has_more = page.has_more

        with self._session_factory() as session:
            current = self._require_scan(session, scan_id)
            if (
                current.version != expected_version
                or current.status != HistoryScanStatus.SCANNING.value
                or current.generation != generation
                or current.cursor != cursor
            ):
                raise self._version_conflict()
            new_count = 0
            changed_count = 0
            unchanged_count = 0
            for snapshot in batch:
                existing = session.scalar(
                    select(HistoryScanFile).where(
                        HistoryScanFile.scan_id == scan_id,
                        HistoryScanFile.relative_path == snapshot.relative_path,
                    )
                )
                digest = self._snapshot_digest(snapshot)
                if existing is None:
                    now = utc_now()
                    session.add(
                        HistoryScanFile(
                            id=new_uuid(),
                            scan_id=scan_id,
                            relative_path=snapshot.relative_path,
                            device=snapshot.device,
                            inode=snapshot.inode,
                            size=snapshot.size,
                            mtime_ns=snapshot.mtime_ns,
                            snapshot_digest=digest,
                            last_seen_generation=generation,
                            created_at=now,
                            updated_at=now,
                        )
                    )
                    new_count += 1
                elif existing.snapshot_digest == digest:
                    existing.last_seen_generation = generation
                    existing.updated_at = utc_now()
                    unchanged_count += 1
                else:
                    existing.device = snapshot.device
                    existing.inode = snapshot.inode
                    existing.size = snapshot.size
                    existing.mtime_ns = snapshot.mtime_ns
                    existing.snapshot_digest = digest
                    existing.last_seen_generation = generation
                    existing.updated_at = utc_now()
                    changed_count += 1

            next_cursor = batch[-1].relative_path if batch else cursor
            now = utc_now()
            final_status = HistoryScanStatus.SCANNING if has_more else HistoryScanStatus.DONE
            result = cast(
                CursorResult[Any],
                session.execute(
                    update(HistoryScan)
                    .where(
                        HistoryScan.id == scan_id,
                        HistoryScan.version == expected_version,
                        HistoryScan.status == HistoryScanStatus.SCANNING.value,
                    )
                    .values(
                        status=final_status.value,
                        cursor=next_cursor,
                        discovered_count=current.discovered_count + len(batch),
                        new_count=current.new_count + new_count,
                        changed_count=current.changed_count + changed_count,
                        unchanged_count=current.unchanged_count + unchanged_count,
                        version=expected_version + 1,
                        last_completed_at=now if final_status is HistoryScanStatus.DONE else None,
                        updated_at=now,
                    )
                ),
            )
            if result.rowcount != 1:
                session.rollback()
                raise self._version_conflict()
            session.commit()
            refreshed = self._require_scan(session, scan_id)
            return HistoryScanBatchResult(self._view(refreshed), len(batch), has_more)

    def materialize(
        self,
        scan_id: str,
        *,
        expected_version: int,
        limit: int,
    ) -> HistoryMaterializeResult:
        if limit < 1 or limit > 1000:
            raise ValueError("history materialize limit 必须位于 1..1000")
        with self._session_factory() as session:
            record = self._require_scan(session, scan_id)
            if record.version != expected_version:
                raise self._version_conflict()
            if HistoryScanStatus(record.status) is not HistoryScanStatus.DONE:
                raise self._state_conflict("只有 DONE 扫描可以把当前文件快照转换为任务")
            generation = record.generation
            root = record.root_relative_path
            media_kind = HistoryMediaKind(record.media_kind)
            pending = tuple(
                session.scalars(
                    select(HistoryScanFile)
                    .where(
                        HistoryScanFile.scan_id == scan_id,
                        HistoryScanFile.last_seen_generation == generation,
                        ~(
                            select(HistoryScanMaterialization.id)
                            .where(
                                HistoryScanMaterialization.scan_file_id == HistoryScanFile.id,
                                HistoryScanMaterialization.snapshot_digest
                                == HistoryScanFile.snapshot_digest,
                            )
                            .exists()
                        ),
                    )
                    .order_by(HistoryScanFile.relative_path, HistoryScanFile.id)
                    .limit(limit)
                )
            )
            file_states = tuple(self._file_state(item) for item in pending)

        plans = tuple(
            self._plan_materialization(root=root, media_kind=media_kind, file_state=file_state)
            for file_state in file_states
        )
        for plan in plans:
            self._assert_materialization_source_current(plan)

        items: list[HistoryMaterializationItemView] = []
        created_count = 0
        reused_count = 0
        skipped_count = 0
        with self._session_factory() as session:
            begin_immediate_write(session)
            current_scan = self._require_scan(session, scan_id)
            if (
                current_scan.version != expected_version
                or current_scan.status != HistoryScanStatus.DONE.value
                or current_scan.generation != generation
            ):
                raise self._version_conflict()
            task_repository = TaskRepository(session)
            unit_repository = TaskUnitRepository(session)
            for plan in plans:
                current_file = session.get(HistoryScanFile, plan.file.id)
                if (
                    current_file is None
                    or current_file.scan_id != scan_id
                    or current_file.last_seen_generation != generation
                    or current_file.snapshot_digest != plan.file.snapshot_digest
                ):
                    raise self._version_conflict()
                existing = session.scalar(
                    select(HistoryScanMaterialization).where(
                        HistoryScanMaterialization.scan_file_id == plan.file.id,
                        HistoryScanMaterialization.snapshot_digest == plan.file.snapshot_digest,
                    )
                )
                if existing is not None:
                    items.append(self._materialization_view(existing, task_created=False))
                    continue
                if plan.unit is None:
                    materialization = HistoryScanMaterialization(
                        id=new_uuid(),
                        scan_id=scan_id,
                        scan_file_id=plan.file.id,
                        snapshot_digest=plan.file.snapshot_digest,
                        status=HistoryMaterializationStatus.SKIPPED.value,
                        reason_code=plan.reason_code,
                        task_id=None,
                        source_root=plan.source_root,
                        normalized_unit_key=None,
                        unit_kind=None,
                        episode_kind=None,
                        episode_season=None,
                        episode_start=None,
                        episode_end=None,
                        episode_label=None,
                        episode_group_key=None,
                        episode_variant_key=None,
                        created_at=utc_now(),
                    )
                    session.add(materialization)
                    session.flush()
                    skipped_count += 1
                    items.append(self._materialization_view(materialization, task_created=False))
                    continue

                task, task_created = task_repository.create_or_get(
                    TaskCreate(
                        task_type=f"HISTORY_{media_kind.value}",
                        source_downloader_id=scan_id,
                        source_hash=self._history_source_hash(
                            scan_id=scan_id,
                            scan_file_id=plan.file.id,
                            snapshot_digest=plan.file.snapshot_digest,
                        ),
                        normalized_unit_key=plan.unit.normalized_unit_key,
                        trace_id=str(uuid4()),
                    )
                )
                unit_repository.record_batch(
                    task_id=task.id,
                    source_root=plan.source_root,
                    source_inventory_digest=plan.source_inventory_digest,
                    units=(plan.unit,),
                )
                materialization = HistoryScanMaterialization(
                    id=new_uuid(),
                    scan_id=scan_id,
                    scan_file_id=plan.file.id,
                    snapshot_digest=plan.file.snapshot_digest,
                    status=HistoryMaterializationStatus.MATERIALIZED.value,
                    reason_code=None,
                    task_id=task.id,
                    source_root=plan.source_root,
                    normalized_unit_key=plan.unit.normalized_unit_key,
                    unit_kind=plan.unit.kind.value,
                    episode_kind=(
                        plan.episode_metadata.kind.value
                        if plan.episode_metadata is not None
                        else None
                    ),
                    episode_season=(
                        plan.episode_metadata.season if plan.episode_metadata is not None else None
                    ),
                    episode_start=(
                        plan.episode_metadata.start if plan.episode_metadata is not None else None
                    ),
                    episode_end=(
                        plan.episode_metadata.end if plan.episode_metadata is not None else None
                    ),
                    episode_label=(
                        plan.episode_metadata.label if plan.episode_metadata is not None else None
                    ),
                    episode_group_key=(
                        plan.episode_metadata.group_key
                        if plan.episode_metadata is not None
                        else None
                    ),
                    episode_variant_key=(
                        plan.episode_metadata.variant_key
                        if plan.episode_metadata is not None
                        else None
                    ),
                    created_at=utc_now(),
                )
                session.add(materialization)
                session.flush()
                if task_created:
                    created_count += 1
                else:
                    reused_count += 1
                items.append(self._materialization_view(materialization, task_created=task_created))

            session.commit()
            refreshed = self._require_scan(session, scan_id)
            remaining = session.scalar(
                select(func.count())
                .select_from(HistoryScanFile)
                .where(
                    HistoryScanFile.scan_id == scan_id,
                    HistoryScanFile.last_seen_generation == generation,
                    ~(
                        select(HistoryScanMaterialization.id)
                        .where(
                            HistoryScanMaterialization.scan_file_id == HistoryScanFile.id,
                            HistoryScanMaterialization.snapshot_digest
                            == HistoryScanFile.snapshot_digest,
                        )
                        .exists()
                    ),
                )
            )
            return HistoryMaterializeResult(
                scan=self._view(refreshed),
                processed_count=len(plans),
                task_created_count=created_count,
                task_reused_count=reused_count,
                skipped_count=skipped_count,
                remaining_count=int(remaining or 0),
                items=tuple(items),
            )

    def _plan_materialization(
        self,
        *,
        root: str,
        media_kind: HistoryMediaKind,
        file_state: _HistoryScanFileState,
    ) -> _HistoryMaterializationPlan:
        relative = PurePosixPath(file_state.relative_path)
        parent = relative.parent.as_posix()
        if root == ".":
            source_root = parent
        elif parent == ".":
            source_root = root
        else:
            source_root = f"{root}/{parent}"
        source_root = self._filesystem.normalize_relative_path(source_root, allow_root=True)
        try:
            self._filesystem.assert_directory(relative_path=source_root)
            source_path = self._data_root_path(source_root)
            inventory = scan_source_inventory(source_path)
        except DomainViolation as exc:
            raise self._source_changed("历史扫描文件的父目录已不可安全读取") from exc
        source_relative_path = relative.name
        candidate = next(
            (item for item in inventory if item.relative_path == source_relative_path),
            None,
        )
        if candidate is None or not self._snapshot_matches(file_state, candidate.snapshot):
            raise self._source_changed("历史扫描文件快照在任务转换前已经变化")
        units = identify_task_units(
            tuple(SourceTaskFile(item.relative_path, item.length) for item in inventory),
            episode_context=source_root if media_kind is HistoryMediaKind.EPISODE else None,
        )
        unit = next(
            (item for item in units if item.source_relative_path == source_relative_path),
            None,
        )
        if unit is None:
            return _HistoryMaterializationPlan(
                file=file_state,
                source_root=source_root,
                source_relative_path=source_relative_path,
                source_inventory_digest=source_inventory_digest(inventory),
                unit=None,
                episode_metadata=None,
                reason_code="UNSUPPORTED_MEDIA_UNIT",
            )
        expected_kind = (
            TaskUnitKind.MOVIE if media_kind is HistoryMediaKind.MOVIE else TaskUnitKind.EPISODE
        )
        if unit.kind is not expected_kind:
            return _HistoryMaterializationPlan(
                file=file_state,
                source_root=source_root,
                source_relative_path=source_relative_path,
                source_inventory_digest=source_inventory_digest(inventory),
                unit=None,
                episode_metadata=None,
                reason_code="MEDIA_KIND_MISMATCH",
            )
        metadata = episode_unit_metadata(unit) if media_kind is HistoryMediaKind.EPISODE else None
        return _HistoryMaterializationPlan(
            file=file_state,
            source_root=source_root,
            source_relative_path=source_relative_path,
            source_inventory_digest=source_inventory_digest(inventory),
            unit=unit,
            episode_metadata=metadata,
            reason_code=None,
        )

    def _assert_materialization_source_current(self, plan: _HistoryMaterializationPlan) -> None:
        try:
            observed = current_file_snapshot(
                self._data_root_path(plan.source_root) / plan.source_relative_path
            )
        except DomainViolation as exc:
            raise self._source_changed("历史扫描文件在任务转换前已经不可用") from exc
        if not self._snapshot_matches(plan.file, observed):
            raise self._source_changed("历史扫描文件在任务转换前已经变化")

    def _data_root_path(self, relative_path: str) -> Path:
        if relative_path == ".":
            return self._data_root
        return self._data_root.joinpath(*relative_path.split("/"))

    @staticmethod
    def _snapshot_matches(file_state: _HistoryScanFileState, snapshot: FileSnapshot) -> bool:
        return (
            snapshot.device == file_state.device
            and snapshot.inode == file_state.inode
            and snapshot.size == file_state.size
            and snapshot.mtime_ns == file_state.mtime_ns
            and snapshot.file_type == "regular"
        )

    @staticmethod
    def _history_source_hash(*, scan_id: str, scan_file_id: str, snapshot_digest: str) -> str:
        payload = (
            f"packbreaker-history-source-v1\0{scan_id}\0{scan_file_id}\0{snapshot_digest}".encode()
        )
        return sha256(payload).hexdigest()

    def _set_status(
        self,
        scan_id: str,
        *,
        expected_version: int,
        required: HistoryScanStatus,
        target: HistoryScanStatus,
    ) -> HistoryScanView:
        with self._session_factory() as session:
            record = self._require_scan(session, scan_id)
            if record.version != expected_version:
                raise self._version_conflict()
            if record.status != required.value:
                raise self._state_conflict(f"只有 {required.value} 扫描可以切换到 {target.value}")
            now = utc_now()
            result = cast(
                CursorResult[Any],
                session.execute(
                    update(HistoryScan)
                    .where(HistoryScan.id == scan_id, HistoryScan.version == expected_version)
                    .values(status=target.value, version=expected_version + 1, updated_at=now)
                ),
            )
            if result.rowcount != 1:
                session.rollback()
                raise self._version_conflict()
            session.commit()
            return self._view(self._require_scan(session, scan_id))

    def _normalize_root(self, value: str) -> str:
        stripped = value.strip()
        if stripped == "/data":
            candidate = "."
        elif stripped.startswith("/data/"):
            candidate = stripped[len("/data/") :]
        elif stripped.startswith("/"):
            raise ApplicationError(
                code="HISTORY_SCAN_ROOT_INVALID",
                status=422,
                title="历史扫描根目录无效",
                detail="扫描根目录只能位于 /data 内",
            )
        else:
            candidate = stripped
        try:
            return self._filesystem.normalize_relative_path(candidate, allow_root=True)
        except DomainViolation as exc:
            raise ApplicationError(
                code="HISTORY_SCAN_ROOT_INVALID",
                status=422,
                title="历史扫描根目录无效",
                detail="扫描根目录必须是安全的 /data 相对路径",
            ) from exc

    @staticmethod
    def _normalize_extensions(values: tuple[str, ...]) -> tuple[str, ...]:
        normalized: set[str] = set()
        for value in values:
            item = value.strip().casefold()
            if not item:
                continue
            if not item.startswith("."):
                item = f".{item}"
            if "/" in item or "\\" in item or len(item) > 16:
                raise ApplicationError(
                    code="HISTORY_SCAN_EXTENSION_INVALID",
                    status=422,
                    title="扫描文件类型无效",
                    detail="文件扩展名必须是简短的单段扩展名",
                )
            normalized.add(item)
        if not normalized:
            raise ApplicationError(
                code="HISTORY_SCAN_EXTENSION_REQUIRED",
                status=422,
                title="缺少扫描文件类型",
                detail="至少配置一个媒体文件扩展名",
            )
        return tuple(sorted(normalized))

    @staticmethod
    def _normalize_excludes(values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = {value.strip().casefold() for value in values if value.strip()}
        if any("\x00" in value or len(value) > 128 for value in normalized):
            raise ApplicationError(
                code="HISTORY_SCAN_EXCLUDE_INVALID",
                status=422,
                title="扫描排除规则无效",
                detail="排除规则不能包含 NUL 且单项长度不能超过 128",
            )
        return tuple(sorted(normalized))

    @staticmethod
    def _snapshot_digest(snapshot: HistoryFileSnapshot) -> str:
        return history_file_snapshot_digest(
            device=snapshot.device,
            inode=snapshot.inode,
            size=snapshot.size,
            mtime_ns=snapshot.mtime_ns,
        )

    @staticmethod
    def _file_state(record: HistoryScanFile) -> _HistoryScanFileState:
        return _HistoryScanFileState(
            id=record.id,
            relative_path=record.relative_path,
            device=record.device,
            inode=record.inode,
            size=record.size,
            mtime_ns=record.mtime_ns,
            snapshot_digest=record.snapshot_digest,
            generation=record.last_seen_generation,
        )

    @staticmethod
    def _materialization_view(
        record: HistoryScanMaterialization,
        *,
        task_created: bool,
    ) -> HistoryMaterializationItemView:
        return HistoryMaterializationItemView(
            materialization_id=record.id,
            scan_file_id=record.scan_file_id,
            status=HistoryMaterializationStatus(record.status),
            reason_code=record.reason_code,
            task_id=record.task_id,
            task_created=task_created,
            source_root=record.source_root,
            normalized_unit_key=record.normalized_unit_key,
            unit_kind=record.unit_kind,
        )

    @staticmethod
    def _source_changed(detail: str) -> ApplicationError:
        return ApplicationError(
            code="HISTORY_SCAN_SOURCE_CHANGED",
            status=409,
            title="历史扫描源文件已经变化",
            detail=detail,
        )

    @staticmethod
    def _require_scan(session: Session, scan_id: str) -> HistoryScan:
        record = session.get(HistoryScan, scan_id)
        if record is None:
            raise ApplicationError(
                code="HISTORY_SCAN_NOT_FOUND",
                status=404,
                title="历史扫描不存在",
                detail="指定历史扫描不存在",
            )
        return record

    @staticmethod
    def _version_conflict() -> ApplicationError:
        return ApplicationError(
            code="HISTORY_SCAN_VERSION_CONFLICT",
            status=409,
            title="历史扫描版本冲突",
            detail="扫描状态已变化，请刷新后使用最新版本继续操作",
        )

    @staticmethod
    def _state_conflict(detail: str) -> ApplicationError:
        return ApplicationError(
            code="HISTORY_SCAN_STATE_INVALID",
            status=409,
            title="历史扫描状态不允许该操作",
            detail=detail,
        )

    @staticmethod
    def _view(record: HistoryScan) -> HistoryScanView:
        return HistoryScanView(
            id=record.id,
            root_relative_path=record.root_relative_path,
            media_kind=HistoryMediaKind(record.media_kind),
            extensions=tuple(record.extensions),
            exclude_patterns=tuple(record.exclude_patterns),
            status=HistoryScanStatus(record.status),
            generation=record.generation,
            cursor=record.cursor,
            discovered_count=record.discovered_count,
            new_count=record.new_count,
            changed_count=record.changed_count,
            unchanged_count=record.unchanged_count,
            version=record.version,
            last_started_at=record.last_started_at,
            last_completed_at=record.last_completed_at,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )
