from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.errors import ApplicationError
from backend.app.domain.errors import DomainViolation
from backend.app.domain.history_scan import HistoryMediaKind, HistoryScanStatus
from backend.app.infrastructure.history_scanner import HistoryFileSnapshot, HistoryFilesystemScanner
from backend.app.infrastructure.persistence.models import (
    HistoryScan,
    HistoryScanFile,
    new_uuid,
    utc_now,
)
from backend.app.infrastructure.safe_filesystem import SafeFilesystemGateway


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


class HistoryScanService:
    def __init__(self, session_factory: sessionmaker[Session], *, data_root: Path) -> None:
        self._session_factory = session_factory
        self._filesystem = SafeFilesystemGateway(data_root)
        self._scanner = HistoryFilesystemScanner(data_root)

    def list_scans(self) -> tuple[HistoryScanView, ...]:
        with self._session_factory() as session:
            records = tuple(
                session.scalars(select(HistoryScan).order_by(HistoryScan.created_at.desc()))
            )
            return tuple(self._view(record) for record in records)

    def get(self, scan_id: str) -> HistoryScanView:
        with self._session_factory() as session:
            return self._view(self._require_scan(session, scan_id))

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
            }:
                raise self._state_conflict("只有 READY 或 DONE 扫描可以开始新一轮增量扫描")
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
            snapshots = self._scanner.scan(
                root_relative_path=root,
                extensions=extensions,
                exclude_patterns=excludes,
            )
        except OSError as exc:
            raise ApplicationError(
                code="HISTORY_SCAN_FILESYSTEM_CHANGED",
                status=409,
                title="历史扫描目录发生变化",
                detail="扫描期间目录不可安全读取，请刷新后重试当前批次",
            ) from exc
        remaining = tuple(
            item for item in snapshots if cursor is None or item.relative_path > cursor
        )
        batch = remaining[:limit]
        has_more = len(remaining) > len(batch)

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
        payload = (
            f"{snapshot.device}\0{snapshot.inode}\0{snapshot.size}\0{snapshot.mtime_ns}"
        ).encode()
        return sha256(payload).hexdigest()

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
