from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.errors import ApplicationError
from backend.app.application.unpack_source_scans import (
    build_unpack_source_object_key,
    matches_unpack_file_filter,
    normalize_unpack_file_filter,
)
from backend.app.domain.errors import DomainViolation
from backend.app.domain.file_mapping import SourceFileCandidate
from backend.app.domain.unpack import (
    UnpackExecutionScopeKind,
    UnpackExecutionStatus,
    UnpackItemStatus,
    UnpackSourceKind,
)
from backend.app.infrastructure.authorized_paths import AuthorizedPathScope
from backend.app.infrastructure.persistence.database import begin_immediate_write
from backend.app.infrastructure.persistence.models import (
    UnpackExecution,
    UnpackExecutionItem,
    new_uuid,
    utc_now,
)
from backend.app.infrastructure.source_inventory import (
    current_file_snapshot,
    scan_source_inventory_page,
)


@dataclass(frozen=True, slots=True)
class UnpackDiscoveryReport:
    execution_id: str
    created_count: int
    error_count: int
    total_count: int
    discovery_complete: bool
    next_cursor: str | None
    execution_status: UnpackExecutionStatus


class UnpackDiscoveryService:
    """将冻结来源配置分页物化为 execution item，不产生外部副作用。"""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        path_scope: AuthorizedPathScope,
    ) -> None:
        self._session_factory = session_factory
        self._path_scope = path_scope

    def list_due_execution_ids(self, *, limit: int = 20) -> tuple[str, ...]:
        if limit < 1 or limit > 500:
            raise self._invalid("待发现 execution 查询数量必须位于 1 到 500 之间")
        with self._session_factory() as session:
            return tuple(
                session.scalars(
                    select(UnpackExecution.id)
                    .where(UnpackExecution.status == UnpackExecutionStatus.DISCOVERING.value)
                    .where(UnpackExecution.discovery_complete.is_(False))
                    .order_by(UnpackExecution.created_at, UnpackExecution.id)
                    .limit(limit)
                ).all()
            )

    def discover_next_page(
        self,
        execution_id: str,
        *,
        limit: int = 100,
    ) -> UnpackDiscoveryReport:
        if limit < 1 or limit > 500:
            raise self._invalid("媒体发现分页大小必须位于 1 到 500 之间")

        with self._session_factory() as session:
            begin_immediate_write(session)
            execution = self._require_execution(session, execution_id)
            if execution.discovery_complete:
                return self._report(execution, created_count=0, error_count=0)
            if execution.status != UnpackExecutionStatus.DISCOVERING.value:
                raise self._conflict("当前 execution 不处于媒体发现阶段")

            snapshot = dict(execution.config_snapshot)
            try:
                source_kind = UnpackSourceKind(str(snapshot["source_kind"]))
                scope_kind = UnpackExecutionScopeKind(str(snapshot["execution_scope_kind"]))
            except (KeyError, ValueError) as exc:
                raise self._invalid_snapshot("execution 来源快照无效") from exc

            if source_kind is UnpackSourceKind.DOWNLOADER:
                created, errors = self._discover_downloader(
                    session,
                    execution,
                    snapshot,
                    limit=limit,
                )
            elif scope_kind is UnpackExecutionScopeKind.ALL_MATCHING_MEDIA:
                created, errors = self._discover_all_matching(
                    session,
                    execution,
                    snapshot,
                    limit=limit,
                )
            else:
                created, errors = self._discover_selected(
                    session,
                    execution,
                    snapshot,
                    limit=limit,
                )

            session.flush()
            self._finalize_execution_state(session, execution)
            session.commit()
            return self._report(execution, created_count=created, error_count=errors)

    def _discover_downloader(
        self,
        session: Session,
        execution: UnpackExecution,
        snapshot: dict[str, Any],
        *,
        limit: int,
    ) -> tuple[int, int]:
        raw_sources = snapshot.get("downloader_sources")
        if not isinstance(raw_sources, list):
            raise self._invalid_snapshot("execution 缺少冻结的下载器来源")
        source_config = _require_mapping(snapshot.get("source_config"), "来源配置")
        downloader_id = source_config.get("downloader_id")
        if not isinstance(downloader_id, str) or not downloader_id:
            raise self._invalid_snapshot("execution 缺少 downloader_id")
        file_filter = normalize_unpack_file_filter(
            _require_mapping(snapshot.get("file_filter"), "文件过滤")
        )
        source_index, inner_cursor = _decode_downloader_cursor(execution.discovery_cursor)
        budget = limit
        created = 0
        errors = 0
        now = utc_now()

        while source_index < len(raw_sources) and budget > 0:
            raw = raw_sources[source_index]
            if not isinstance(raw, dict):
                raise self._invalid_snapshot("下载器来源快照格式无效")
            torrent_hash = raw.get("torrent_hash")
            torrent_name = raw.get("name")
            if not isinstance(torrent_hash, str) or not torrent_hash:
                raise self._invalid_snapshot("下载器来源缺少 torrent hash")
            if not isinstance(torrent_name, str) or not torrent_name:
                raise self._invalid_snapshot("下载器来源缺少 torrent 名称")

            mapping_error = raw.get("mapping_error_code")
            mapped_path = raw.get("mapped_content_path")
            if isinstance(mapping_error, str) and mapping_error:
                key = _downloader_source_key(torrent_hash, "mapping-error")
                if not self._item_exists(session, execution.id, key):
                    session.add(
                        UnpackExecutionItem(
                            id=new_uuid(),
                            execution_id=execution.id,
                            source_object_key=key,
                            source_snapshot={
                                "downloader_id": downloader_id,
                                "torrent_hash": torrent_hash,
                                "torrent_name": torrent_name,
                                "remote_content": raw.get("remote_content"),
                            },
                            media_identity={},
                            status=UnpackItemStatus.MATCH_ERROR.value,
                            candidate_generation=0,
                            retry_count=0,
                            last_error_code=mapping_error,
                            last_error_message=str(
                                raw.get("mapping_error_message") or "下载器路径映射失败"
                            ),
                            version=1,
                            created_at=now,
                            updated_at=now,
                        )
                    )
                    created += 1
                    errors += 1
                source_index += 1
                inner_cursor = None
                budget -= 1
                continue
            if not isinstance(mapped_path, str) or not mapped_path:
                raise self._invalid_snapshot("下载器来源缺少映射内容路径")

            try:
                normalized_path = self._path_scope.normalize_reference(mapped_path)
                path = Path(normalized_path)
                try:
                    directory_path, directory = self._path_scope.resolve_existing_directory(
                        normalized_path
                    )
                except DomainViolation:
                    directory_path = None
                    directory = None

                if directory is not None and directory_path is not None:
                    page = scan_source_inventory_page(
                        directory,
                        after=inner_cursor,
                        limit=budget,
                    )
                    budget -= len(page.candidates)
                    for source in page.candidates:
                        if not matches_unpack_file_filter(
                            source.relative_path, source.length, file_filter
                        ):
                            continue
                        base_key = build_unpack_source_object_key(
                            relative_path=source.relative_path,
                            device=source.snapshot.device,
                            inode=source.snapshot.inode,
                            size_bytes=source.length,
                            mtime_ns=source.snapshot.mtime_ns,
                        )
                        key = _downloader_source_key(torrent_hash, base_key)
                        if self._item_exists(session, execution.id, key):
                            continue
                        source_snapshot = _source_snapshot(
                            directory_path=directory_path,
                            source=source,
                        )
                        source_snapshot.update(
                            {
                                "downloader_id": downloader_id,
                                "torrent_hash": torrent_hash,
                                "torrent_name": torrent_name,
                                "remote_content": raw.get("remote_content"),
                            }
                        )
                        session.add(
                            UnpackExecutionItem(
                                id=new_uuid(),
                                execution_id=execution.id,
                                source_object_key=key,
                                source_snapshot=source_snapshot,
                                media_identity={},
                                status=UnpackItemStatus.MATCH_PENDING.value,
                                candidate_generation=0,
                                retry_count=0,
                                version=1,
                                created_at=now,
                                updated_at=now,
                            )
                        )
                        created += 1
                    if page.has_more:
                        inner_cursor = page.next_cursor
                        break
                    source_index += 1
                    inner_cursor = None
                    continue

                self._path_scope.resolve_existing_directory(path.parent.as_posix())
                observed = current_file_snapshot(path)
                budget -= 1
                if matches_unpack_file_filter(path.name, observed.size, file_filter):
                    base_key = build_unpack_source_object_key(
                        relative_path=path.name,
                        device=observed.device,
                        inode=observed.inode,
                        size_bytes=observed.size,
                        mtime_ns=observed.mtime_ns,
                    )
                    key = _downloader_source_key(torrent_hash, base_key)
                    if not self._item_exists(session, execution.id, key):
                        session.add(
                            UnpackExecutionItem(
                                id=new_uuid(),
                                execution_id=execution.id,
                                source_object_key=key,
                                source_snapshot={
                                    "path": path.as_posix(),
                                    "relative_path": path.name,
                                    "device": observed.device,
                                    "inode": observed.inode,
                                    "size": observed.size,
                                    "mtime_ns": str(observed.mtime_ns),
                                    "file_type": observed.file_type,
                                    "downloader_id": downloader_id,
                                    "torrent_hash": torrent_hash,
                                    "torrent_name": torrent_name,
                                    "remote_content": raw.get("remote_content"),
                                },
                                media_identity={},
                                status=UnpackItemStatus.MATCH_PENDING.value,
                                candidate_generation=0,
                                retry_count=0,
                                version=1,
                                created_at=now,
                                updated_at=now,
                            )
                        )
                        created += 1
                source_index += 1
                inner_cursor = None
            except (DomainViolation, OSError) as exc:
                key = _downloader_source_key(torrent_hash, "source-unavailable")
                if not self._item_exists(session, execution.id, key):
                    session.add(
                        UnpackExecutionItem(
                            id=new_uuid(),
                            execution_id=execution.id,
                            source_object_key=key,
                            source_snapshot={
                                "path": mapped_path,
                                "downloader_id": downloader_id,
                                "torrent_hash": torrent_hash,
                                "torrent_name": torrent_name,
                                "remote_content": raw.get("remote_content"),
                            },
                            media_identity={},
                            status=UnpackItemStatus.MATCH_ERROR.value,
                            candidate_generation=0,
                            retry_count=0,
                            last_error_code="UNPACK_DOWNLOADER_SOURCE_UNAVAILABLE",
                            last_error_message=str(exc),
                            version=1,
                            created_at=now,
                            updated_at=now,
                        )
                    )
                    created += 1
                    errors += 1
                source_index += 1
                inner_cursor = None
                budget -= 1

        execution.discovery_complete = source_index >= len(raw_sources)
        execution.discovery_cursor = _encode_downloader_cursor(source_index, inner_cursor)
        execution.total_count += created
        execution.error_count += errors
        execution.updated_at = now
        execution.version += 1
        return created, errors

    def _discover_all_matching(
        self,
        session: Session,
        execution: UnpackExecution,
        snapshot: dict[str, Any],
        *,
        limit: int,
    ) -> tuple[int, int]:
        source_config = _require_mapping(snapshot.get("source_config"), "来源配置")
        directory_reference = source_config.get("directory_path")
        if not isinstance(directory_reference, str) or not directory_reference:
            raise self._invalid_snapshot("execution 缺少来源目录")
        try:
            directory_path, directory = self._path_scope.resolve_existing_directory(
                directory_reference
            )
        except DomainViolation as exc:
            raise self._source_unavailable(str(exc)) from exc

        file_filter = normalize_unpack_file_filter(
            _require_mapping(snapshot.get("file_filter"), "文件过滤")
        )
        page = scan_source_inventory_page(
            directory,
            after=execution.discovery_cursor,
            limit=limit,
        )
        created = 0
        now = utc_now()
        for source in page.candidates:
            if not matches_unpack_file_filter(
                source.relative_path,
                source.length,
                file_filter,
            ):
                continue
            key = build_unpack_source_object_key(
                relative_path=source.relative_path,
                device=source.snapshot.device,
                inode=source.snapshot.inode,
                size_bytes=source.length,
                mtime_ns=source.snapshot.mtime_ns,
            )
            if self._item_exists(session, execution.id, key):
                continue
            session.add(
                UnpackExecutionItem(
                    id=new_uuid(),
                    execution_id=execution.id,
                    source_object_key=key,
                    source_snapshot=_source_snapshot(
                        directory_path=directory_path,
                        source=source,
                    ),
                    media_identity={},
                    status=UnpackItemStatus.MATCH_PENDING.value,
                    candidate_generation=0,
                    retry_count=0,
                    version=1,
                    created_at=now,
                    updated_at=now,
                )
            )
            created += 1

        execution.discovery_cursor = page.next_cursor
        execution.discovery_complete = not page.has_more
        execution.total_count += created
        execution.updated_at = now
        execution.version += 1
        return created, 0

    def _discover_selected(
        self,
        session: Session,
        execution: UnpackExecution,
        snapshot: dict[str, Any],
        *,
        limit: int,
    ) -> tuple[int, int]:
        raw_selected = snapshot.get("selected_sources")
        if not isinstance(raw_selected, list):
            raise self._invalid_snapshot("execution 缺少选定影片快照")

        offset = _selected_cursor_offset(execution.discovery_cursor)
        selected = raw_selected[offset : offset + limit]
        now = utc_now()
        created = 0
        errors = 0

        for raw in selected:
            if not isinstance(raw, dict):
                raise self._invalid_snapshot("选定影片快照格式无效")
            key = raw.get("source_object_key")
            path_text = raw.get("canonical_path_hint")
            expected = raw.get("source_snapshot")
            if not isinstance(key, str) or not key or not isinstance(path_text, str):
                raise self._invalid_snapshot("选定影片缺少稳定身份或路径")
            if self._item_exists(session, execution.id, key):
                continue

            status = UnpackItemStatus.MATCH_PENDING
            error_code: str | None = None
            error_message: str | None = None
            observed_snapshot: dict[str, Any]

            try:
                canonical = self._revalidate_selected_path(path_text)
                observed = current_file_snapshot(canonical)
                observed_snapshot = {
                    "path": canonical.as_posix(),
                    "device": observed.device,
                    "inode": observed.inode,
                    "size": observed.size,
                    "mtime_ns": str(observed.mtime_ns),
                    "file_type": observed.file_type,
                }
                if not _snapshot_matches(expected, observed_snapshot):
                    status = UnpackItemStatus.MATCH_ERROR
                    error_code = "UNPACK_SOURCE_CHANGED"
                    error_message = "来源影片自保存任务后已发生变化，请重新选择影片"
                    errors += 1
            except (DomainViolation, OSError):
                canonical = Path(path_text)
                observed_snapshot = {"path": canonical.as_posix()}
                status = UnpackItemStatus.MATCH_ERROR
                error_code = "UNPACK_SOURCE_CHANGED"
                error_message = "来源影片已不存在、不可访问或路径不再安全，请重新选择影片"
                errors += 1

            session.add(
                UnpackExecutionItem(
                    id=new_uuid(),
                    execution_id=execution.id,
                    source_object_key=key,
                    source_snapshot=observed_snapshot,
                    media_identity={},
                    status=status.value,
                    candidate_generation=0,
                    retry_count=0,
                    last_error_code=error_code,
                    last_error_message=error_message,
                    version=1,
                    created_at=now,
                    updated_at=now,
                )
            )
            created += 1

        next_offset = offset + len(selected)
        execution.discovery_cursor = f"selected:{next_offset}"
        execution.discovery_complete = next_offset >= len(raw_selected)
        execution.total_count += created
        execution.error_count += errors
        execution.updated_at = now
        execution.version += 1
        return created, errors

    def _revalidate_selected_path(self, value: str) -> Path:
        try:
            normalized = self._path_scope.normalize_reference(value)
            path = Path(normalized)
            self._path_scope.resolve_existing_directory(path.parent.as_posix())
        except DomainViolation as exc:
            raise self._source_unavailable(str(exc)) from exc
        return path

    @staticmethod
    def _item_exists(session: Session, execution_id: str, source_object_key: str) -> bool:
        return (
            session.scalar(
                select(UnpackExecutionItem.id)
                .where(UnpackExecutionItem.execution_id == execution_id)
                .where(UnpackExecutionItem.source_object_key == source_object_key)
                .limit(1)
            )
            is not None
        )

    @staticmethod
    def _finalize_execution_state(session: Session, execution: UnpackExecution) -> None:
        if not execution.discovery_complete:
            return

        matchable = session.scalar(
            select(UnpackExecutionItem.id)
            .where(UnpackExecutionItem.execution_id == execution.id)
            .where(UnpackExecutionItem.status == UnpackItemStatus.MATCH_PENDING.value)
            .limit(1)
        )
        now = utc_now()
        if matchable is not None:
            execution.status = UnpackExecutionStatus.MATCHING.value
        elif execution.total_count == 0:
            execution.status = UnpackExecutionStatus.COMPLETED.value
            execution.finished_at = now
        else:
            execution.status = UnpackExecutionStatus.COMPLETED_WITH_ERRORS.value
            execution.finished_at = now
        execution.updated_at = now
        execution.version += 1

    @staticmethod
    def _require_execution(session: Session, execution_id: str) -> UnpackExecution:
        execution = session.get(UnpackExecution, execution_id)
        if execution is None:
            raise ApplicationError(
                code="UNPACK_EXECUTION_NOT_FOUND",
                status=404,
                title="数据拆包执行不存在",
                detail="未找到指定数据拆包执行",
            )
        return execution

    @staticmethod
    def _report(
        execution: UnpackExecution,
        *,
        created_count: int,
        error_count: int,
    ) -> UnpackDiscoveryReport:
        return UnpackDiscoveryReport(
            execution_id=execution.id,
            created_count=created_count,
            error_count=error_count,
            total_count=execution.total_count,
            discovery_complete=execution.discovery_complete,
            next_cursor=execution.discovery_cursor,
            execution_status=UnpackExecutionStatus(execution.status),
        )

    @staticmethod
    def _invalid(detail: str) -> ApplicationError:
        return ApplicationError(
            code="UNPACK_DISCOVERY_INVALID",
            status=422,
            title="媒体发现参数无效",
            detail=detail,
        )

    @staticmethod
    def _invalid_snapshot(detail: str) -> ApplicationError:
        return ApplicationError(
            code="UNPACK_EXECUTION_SNAPSHOT_INVALID",
            status=409,
            title="数据拆包执行快照无效",
            detail=detail,
        )

    @staticmethod
    def _source_unavailable(detail: str) -> ApplicationError:
        return ApplicationError(
            code="UNPACK_SOURCE_UNAVAILABLE",
            status=409,
            title="拆包来源不可用",
            detail=detail,
        )

    @staticmethod
    def _conflict(detail: str) -> ApplicationError:
        return ApplicationError(
            code="UNPACK_DISCOVERY_CONFLICT",
            status=409,
            title="媒体发现状态冲突",
            detail=detail,
        )


def _require_mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ApplicationError(
            code="UNPACK_EXECUTION_SNAPSHOT_INVALID",
            status=409,
            title="数据拆包执行快照无效",
            detail=f"{label}格式无效",
        )
    return dict(value)


def _source_snapshot(
    *,
    directory_path: str,
    source: SourceFileCandidate,
) -> dict[str, Any]:
    return {
        "path": Path(directory_path).joinpath(*source.relative_path.split("/")).as_posix(),
        "relative_path": source.relative_path,
        "device": source.snapshot.device,
        "inode": source.snapshot.inode,
        "size": source.snapshot.size,
        "mtime_ns": str(source.snapshot.mtime_ns),
        "file_type": source.snapshot.file_type,
    }


def _selected_cursor_offset(cursor: str | None) -> int:
    if cursor is None:
        return 0
    prefix = "selected:"
    if not cursor.startswith(prefix):
        raise ApplicationError(
            code="UNPACK_EXECUTION_SNAPSHOT_INVALID",
            status=409,
            title="数据拆包执行快照无效",
            detail="选定影片发现游标无效",
        )
    try:
        offset = int(cursor[len(prefix) :])
    except ValueError as exc:
        raise ApplicationError(
            code="UNPACK_EXECUTION_SNAPSHOT_INVALID",
            status=409,
            title="数据拆包执行快照无效",
            detail="选定影片发现游标无效",
        ) from exc
    if offset < 0:
        raise ApplicationError(
            code="UNPACK_EXECUTION_SNAPSHOT_INVALID",
            status=409,
            title="数据拆包执行快照无效",
            detail="选定影片发现游标无效",
        )
    return offset


def _snapshot_matches(expected: object, observed: dict[str, Any]) -> bool:
    if not isinstance(expected, dict):
        return False
    fields = ("device", "inode", "size", "file_type")
    if any(expected.get(field) != observed.get(field) for field in fields):
        return False
    return str(expected.get("mtime_ns")) == str(observed.get("mtime_ns"))


def _downloader_source_key(torrent_hash: str, source_key: str) -> str:
    return sha256(f"{torrent_hash}:{source_key}".encode()).hexdigest()


def _encode_downloader_cursor(source_index: int, inner_cursor: str | None) -> str:
    payload = json.dumps(
        {"source_index": source_index, "inner_cursor": inner_cursor},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    return f"downloader:{encoded}"


def _decode_downloader_cursor(cursor: str | None) -> tuple[int, str | None]:
    if cursor is None:
        return 0, None
    prefix = "downloader:"
    if not cursor.startswith(prefix):
        raise ApplicationError(
            code="UNPACK_EXECUTION_SNAPSHOT_INVALID",
            status=409,
            title="数据拆包执行快照无效",
            detail="下载器媒体发现游标无效",
        )
    raw = cursor[len(prefix) :]
    try:
        padding = "=" * (-len(raw) % 4)
        payload = json.loads(base64.urlsafe_b64decode(raw + padding).decode("utf-8"))
        source_index = payload["source_index"]
        inner_cursor = payload["inner_cursor"]
    except (binascii.Error, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ApplicationError(
            code="UNPACK_EXECUTION_SNAPSHOT_INVALID",
            status=409,
            title="数据拆包执行快照无效",
            detail="下载器媒体发现游标无效",
        ) from exc
    if not isinstance(source_index, int) or source_index < 0:
        raise ApplicationError(
            code="UNPACK_EXECUTION_SNAPSHOT_INVALID",
            status=409,
            title="数据拆包执行快照无效",
            detail="下载器媒体发现游标无效",
        )
    if inner_cursor is not None and not isinstance(inner_cursor, str):
        raise ApplicationError(
            code="UNPACK_EXECUTION_SNAPSHOT_INVALID",
            status=409,
            title="数据拆包执行快照无效",
            detail="下载器媒体发现游标无效",
        )
    return source_index, inner_cursor
