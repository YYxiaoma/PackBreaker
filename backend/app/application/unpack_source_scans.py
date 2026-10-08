from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.errors import ApplicationError
from backend.app.domain.errors import DomainViolation
from backend.app.domain.task_definition import DEFAULT_VIDEO_EXTENSIONS
from backend.app.infrastructure.authorized_paths import AuthorizedPathScope
from backend.app.infrastructure.persistence.models import (
    UnpackSourceScan,
    UnpackSourceScanItem,
    new_uuid,
    utc_now,
)
from backend.app.infrastructure.source_inventory import scan_source_inventory_page

_SCAN_TTL = timedelta(minutes=30)
_SCAN_PAGE_SIZE = 250
_MAX_SCAN_MEDIA = 100_000
_RESOLUTION_PATTERN = re.compile(
    r"(?:^|[._\-\s])(4320p|2160p|1440p|1080p|720p|576p|480p)(?:$|[._\-\s])",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class UnpackTreeEntryView:
    name: str
    display_path: str
    selection_token: str


@dataclass(frozen=True, slots=True)
class UnpackTreeView:
    display_path: str
    selection_token: str
    entries: tuple[UnpackTreeEntryView, ...]


@dataclass(frozen=True, slots=True)
class UnpackSourceScanView:
    id: str
    directory_path: str
    discovered_count: int
    selected_count: int
    expires_at: datetime
    version: int


@dataclass(frozen=True, slots=True)
class UnpackSourceScanItemView:
    source_object_key: str
    relative_path: str
    filename: str
    extension: str
    resolution: str | None
    size_bytes: int
    selected: bool


@dataclass(frozen=True, slots=True)
class UnpackSourceScanPageView:
    items: tuple[UnpackSourceScanItemView, ...]
    next_cursor: str | None
    has_more: bool


@dataclass(frozen=True, slots=True)
class UnpackSourceScanSelectionSummaryView:
    discovered_count: int
    selected_count: int


class UnpackSourceScanService:
    """保存前只读目录扫描；selection token 只是定位符，不授予任何目录权限。"""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        path_scope: AuthorizedPathScope,
    ) -> None:
        self._session_factory = session_factory
        self._path_scope = path_scope

    def list_tree_roots(self) -> tuple[UnpackTreeEntryView, ...]:
        _current, entries = self._path_scope.browse_directories("/")
        return tuple(
            UnpackTreeEntryView(
                name=Path(entry.path).name or entry.path,
                display_path=entry.path,
                selection_token=self._encode_selection_token(entry.path),
            )
            for entry in entries
        )

    def browse_tree(self, selection_token: str) -> UnpackTreeView:
        raw_path = self._decode_selection_token(selection_token)
        try:
            current, entries = self._path_scope.browse_directories(raw_path)
        except DomainViolation as exc:
            raise self._invalid_path(str(exc)) from exc
        return UnpackTreeView(
            display_path=current,
            selection_token=self._encode_selection_token(current),
            entries=tuple(
                UnpackTreeEntryView(
                    name=entry.name,
                    display_path=entry.path,
                    selection_token=self._encode_selection_token(entry.path),
                )
                for entry in entries
            ),
        )

    def create_scan(
        self,
        *,
        selection_token: str,
        file_filter: dict[str, Any],
    ) -> UnpackSourceScanView:
        raw_path = self._decode_selection_token(selection_token)
        try:
            directory_path, directory = self._path_scope.resolve_existing_directory(raw_path)
        except DomainViolation as exc:
            raise self._invalid_path(str(exc)) from exc
        normalized_filter = normalize_unpack_file_filter(file_filter)
        now = utc_now()
        scan = UnpackSourceScan(
            id=new_uuid(),
            directory_path=directory_path,
            file_filter=normalized_filter,
            discovered_count=0,
            version=1,
            expires_at=now + _SCAN_TTL,
            created_at=now,
            updated_at=now,
        )

        with self._session_factory() as session:
            self._purge_expired(session, now=now)
            session.add(scan)
            session.flush()

            cursor: str | None = None
            discovered = 0
            while True:
                page = scan_source_inventory_page(
                    directory,
                    after=cursor,
                    limit=_SCAN_PAGE_SIZE,
                )
                for source in page.candidates:
                    if not matches_unpack_file_filter(
                        source.relative_path, source.length, normalized_filter
                    ):
                        continue
                    if discovered >= _MAX_SCAN_MEDIA:
                        raise ApplicationError(
                            code="UNPACK_SOURCE_SCAN_LIMIT_EXCEEDED",
                            status=422,
                            title="目录影视文件过多",
                            detail="目录内符合条件的影视文件超过扫描上限，请缩小目录或过滤范围",
                        )
                    relative = source.relative_path
                    filename = PurePosixPath(relative).name
                    extension = PurePosixPath(filename).suffix.lower()
                    canonical_path = directory.joinpath(*relative.split("/")).as_posix()
                    session.add(
                        UnpackSourceScanItem(
                            id=new_uuid(),
                            scan_id=scan.id,
                            source_object_key=build_unpack_source_object_key(
                                relative_path=relative,
                                device=source.snapshot.device,
                                inode=source.snapshot.inode,
                                size_bytes=source.length,
                                mtime_ns=source.snapshot.mtime_ns,
                            ),
                            relative_path=relative,
                            canonical_path_hint=canonical_path,
                            filename=filename,
                            extension=extension,
                            resolution=_detect_resolution(filename),
                            size_bytes=source.length,
                            device=source.snapshot.device,
                            inode=source.snapshot.inode,
                            mtime_ns=str(source.snapshot.mtime_ns),
                            selected=False,
                            created_at=now,
                            updated_at=now,
                        )
                    )
                    discovered += 1
                if not page.has_more:
                    break
                cursor = page.next_cursor

            scan.discovered_count = discovered
            scan.updated_at = utc_now()
            session.commit()
            return self._scan_view(session, scan)

    def get_scan(self, scan_id: str) -> UnpackSourceScanView:
        with self._session_factory() as session:
            scan = self._require_scan(session, scan_id)
            return self._scan_view(session, scan)

    def list_items(
        self,
        scan_id: str,
        *,
        cursor: str | None,
        limit: int,
        query: str | None = None,
        extension: str | None = None,
        resolution: str | None = None,
        selected: bool | None = None,
    ) -> UnpackSourceScanPageView:
        if limit < 1 or limit > 200:
            raise self._invalid("分页大小必须位于 1 到 200 之间")
        with self._session_factory() as session:
            self._require_scan(session, scan_id)
            statement = select(UnpackSourceScanItem).where(UnpackSourceScanItem.scan_id == scan_id)
            if cursor:
                statement = statement.where(UnpackSourceScanItem.relative_path > cursor)
            if query and query.strip():
                pattern = f"%{query.strip().lower()}%"
                statement = statement.where(
                    func.lower(UnpackSourceScanItem.relative_path).like(pattern)
                )
            if extension and extension.strip():
                normalized_extension = _normalize_extension(extension)
                statement = statement.where(UnpackSourceScanItem.extension == normalized_extension)
            if resolution and resolution.strip():
                statement = statement.where(
                    UnpackSourceScanItem.resolution == resolution.strip().lower()
                )
            if selected is not None:
                statement = statement.where(UnpackSourceScanItem.selected.is_(selected))
            records = session.scalars(
                statement.order_by(UnpackSourceScanItem.relative_path).limit(limit + 1)
            ).all()
            has_more = len(records) > limit
            visible = records[:limit]
            next_cursor = visible[-1].relative_path if visible and has_more else None
            return UnpackSourceScanPageView(
                items=tuple(self._item_view(item) for item in visible),
                next_cursor=next_cursor,
                has_more=has_more,
            )

    def update_selection(
        self,
        scan_id: str,
        *,
        source_object_keys: tuple[str, ...],
        selected: bool,
    ) -> UnpackSourceScanSelectionSummaryView:
        keys = tuple(dict.fromkeys(value.strip() for value in source_object_keys if value.strip()))
        if not keys:
            raise self._invalid("至少选择一个影视文件")
        if len(keys) > 1000:
            raise self._invalid("单次最多更新 1000 个影视文件")
        with self._session_factory() as session:
            scan = self._require_scan(session, scan_id)
            records = session.scalars(
                select(UnpackSourceScanItem)
                .where(UnpackSourceScanItem.scan_id == scan.id)
                .where(UnpackSourceScanItem.source_object_key.in_(keys))
            ).all()
            if len(records) != len(keys):
                raise ApplicationError(
                    code="UNPACK_SOURCE_SCAN_ITEM_NOT_FOUND",
                    status=404,
                    title="影视文件不存在",
                    detail="部分影视文件已不属于当前扫描结果，请刷新扫描结果后重试",
                )
            now = utc_now()
            for item in records:
                item.selected = selected
                item.updated_at = now
            scan.version += 1
            scan.updated_at = now
            session.commit()
            return self._selection_summary(session, scan)

    def selection_summary(self, scan_id: str) -> UnpackSourceScanSelectionSummaryView:
        with self._session_factory() as session:
            scan = self._require_scan(session, scan_id)
            return self._selection_summary(session, scan)

    def selected_sources(
        self,
        session: Session,
        *,
        scan_id: str,
        directory_path: str,
        file_filter: dict[str, Any],
    ) -> tuple[UnpackSourceScanItem, ...]:
        scan = self._require_scan(session, scan_id)
        if scan.directory_path != directory_path:
            raise self._invalid("影视文件扫描目录与任务来源目录不一致")
        if dict(scan.file_filter) != normalize_unpack_file_filter(file_filter):
            raise self._invalid("影视文件扫描使用的过滤规则与任务配置不一致")
        selected = tuple(
            session.scalars(
                select(UnpackSourceScanItem)
                .where(UnpackSourceScanItem.scan_id == scan.id)
                .where(UnpackSourceScanItem.selected.is_(True))
                .order_by(UnpackSourceScanItem.relative_path)
            ).all()
        )
        if not selected:
            raise self._invalid("选择影片模式至少需要勾选一个影视文件")
        return selected

    @staticmethod
    def consume_scan(session: Session, scan: UnpackSourceScan) -> None:
        session.delete(scan)

    def _scan_view(self, session: Session, scan: UnpackSourceScan) -> UnpackSourceScanView:
        selected_count = session.scalar(
            select(func.count(UnpackSourceScanItem.id))
            .where(UnpackSourceScanItem.scan_id == scan.id)
            .where(UnpackSourceScanItem.selected.is_(True))
        )
        return UnpackSourceScanView(
            id=scan.id,
            directory_path=scan.directory_path,
            discovered_count=scan.discovered_count,
            selected_count=int(selected_count or 0),
            expires_at=scan.expires_at,
            version=scan.version,
        )

    def _selection_summary(
        self,
        session: Session,
        scan: UnpackSourceScan,
    ) -> UnpackSourceScanSelectionSummaryView:
        selected_count = session.scalar(
            select(func.count(UnpackSourceScanItem.id))
            .where(UnpackSourceScanItem.scan_id == scan.id)
            .where(UnpackSourceScanItem.selected.is_(True))
        )
        return UnpackSourceScanSelectionSummaryView(
            discovered_count=scan.discovered_count,
            selected_count=int(selected_count or 0),
        )

    def _require_scan(self, session: Session, scan_id: str) -> UnpackSourceScan:
        scan = session.get(UnpackSourceScan, scan_id)
        if scan is None:
            raise ApplicationError(
                code="UNPACK_SOURCE_SCAN_NOT_FOUND",
                status=404,
                title="目录扫描不存在",
                detail="目录扫描不存在或已经失效，请重新扫描",
            )
        if scan.expires_at <= utc_now():
            session.delete(scan)
            session.commit()
            raise ApplicationError(
                code="UNPACK_SOURCE_SCAN_EXPIRED",
                status=410,
                title="目录扫描已过期",
                detail="目录扫描结果已过期，请重新扫描后选择影片",
            )
        return scan

    @staticmethod
    def _purge_expired(session: Session, *, now: datetime) -> None:
        session.execute(delete(UnpackSourceScan).where(UnpackSourceScan.expires_at <= now))

    @staticmethod
    def _item_view(item: UnpackSourceScanItem) -> UnpackSourceScanItemView:
        return UnpackSourceScanItemView(
            source_object_key=item.source_object_key,
            relative_path=item.relative_path,
            filename=item.filename,
            extension=item.extension,
            resolution=item.resolution,
            size_bytes=item.size_bytes,
            selected=item.selected,
        )

    @staticmethod
    def _encode_selection_token(path: str) -> str:
        payload = base64.urlsafe_b64encode(path.encode("utf-8")).decode("ascii").rstrip("=")
        return f"v1.{payload}"

    @staticmethod
    def _decode_selection_token(token: str) -> str:
        if not token.startswith("v1.") or len(token) > 8192:
            raise UnpackSourceScanService._invalid_path("目录选择令牌无效")
        raw = token[3:]
        try:
            padding = "=" * (-len(raw) % 4)
            path = base64.urlsafe_b64decode(raw + padding).decode("utf-8")
        except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
            raise UnpackSourceScanService._invalid_path("目录选择令牌无效") from exc
        if not path:
            raise UnpackSourceScanService._invalid_path("目录选择令牌无效")
        return path

    @staticmethod
    def _invalid(detail: str) -> ApplicationError:
        return ApplicationError(
            code="UNPACK_SOURCE_SCAN_INVALID",
            status=422,
            title="目录扫描参数无效",
            detail=detail,
        )

    @staticmethod
    def _invalid_path(detail: str) -> ApplicationError:
        return ApplicationError(
            code="UNPACK_DIRECTORY_SELECTION_INVALID",
            status=422,
            title="目录选择无效",
            detail=detail,
        )


def normalize_unpack_file_filter(raw: dict[str, Any]) -> dict[str, Any]:
    result = dict(raw)
    extensions = raw.get("extensions", DEFAULT_VIDEO_EXTENSIONS)
    if not isinstance(extensions, (list, tuple)):
        raise UnpackSourceScanService._invalid("后缀名过滤必须是数组")
    normalized_extensions: list[str] = []
    for value in extensions:
        if not isinstance(value, str):
            raise UnpackSourceScanService._invalid("后缀名过滤包含无效值")
        item = _normalize_extension(value)
        if item not in normalized_extensions:
            normalized_extensions.append(item)
    if not normalized_extensions:
        raise UnpackSourceScanService._invalid("至少保留一个影视文件后缀")
    result["extensions"] = normalized_extensions

    min_size = raw.get("min_size_bytes")
    max_size = raw.get("max_size_bytes")
    if min_size is not None and (
        not isinstance(min_size, int) or isinstance(min_size, bool) or min_size < 0
    ):
        raise UnpackSourceScanService._invalid("最小文件大小无效")
    if max_size is not None and (
        not isinstance(max_size, int) or isinstance(max_size, bool) or max_size < 0
    ):
        raise UnpackSourceScanService._invalid("最大文件大小无效")
    if min_size is not None and max_size is not None and min_size > max_size:
        raise UnpackSourceScanService._invalid("最小文件大小不能大于最大文件大小")
    result["min_size_bytes"] = min_size
    result["max_size_bytes"] = max_size

    include_name = raw.get("include_name")
    if include_name is not None and not isinstance(include_name, str):
        raise UnpackSourceScanService._invalid("名称包含过滤无效")
    result["include_name"] = (
        include_name.strip() if isinstance(include_name, str) and include_name.strip() else None
    )

    exclude_names = raw.get("exclude_names", [])
    if not isinstance(exclude_names, (list, tuple)) or any(
        not isinstance(value, str) for value in exclude_names
    ):
        raise UnpackSourceScanService._invalid("名称排除过滤无效")
    result["exclude_names"] = [value.strip().casefold() for value in exclude_names if value.strip()]
    include_subdirectories = raw.get("include_subdirectories", True)
    if not isinstance(include_subdirectories, bool):
        raise UnpackSourceScanService._invalid("子目录过滤必须是布尔值")
    result["include_subdirectories"] = include_subdirectories
    return result


def matches_unpack_file_filter(
    relative_path: str,
    size_bytes: int,
    file_filter: dict[str, Any],
) -> bool:
    path = PurePosixPath(relative_path)
    if path.suffix.lower() not in file_filter["extensions"]:
        return False
    if not file_filter["include_subdirectories"] and len(path.parts) > 1:
        return False
    min_size = file_filter["min_size_bytes"]
    max_size = file_filter["max_size_bytes"]
    if min_size is not None and size_bytes < min_size:
        return False
    if max_size is not None and size_bytes > max_size:
        return False
    folded = relative_path.casefold()
    include_name = file_filter["include_name"]
    if include_name and include_name.casefold() not in folded:
        return False
    return not any(value in folded for value in file_filter["exclude_names"])


def _normalize_extension(value: str) -> str:
    item = value.strip().lower()
    if not item or "/" in item or "\\" in item or "\x00" in item or len(item) > 32:
        raise UnpackSourceScanService._invalid("后缀名过滤包含无效值")
    if not item.startswith("."):
        item = f".{item}"
    if item == ".":
        raise UnpackSourceScanService._invalid("后缀名过滤包含无效值")
    return item


def _detect_resolution(filename: str) -> str | None:
    match = _RESOLUTION_PATTERN.search(filename)
    return match.group(1).lower() if match else None


def build_unpack_source_object_key(
    *,
    relative_path: str,
    device: int,
    inode: int,
    size_bytes: int,
    mtime_ns: int,
) -> str:
    raw = json.dumps(
        {
            "relative_path": relative_path,
            "device": device,
            "inode": inode,
            "size_bytes": size_bytes,
            "mtime_ns": mtime_ns,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(raw).hexdigest()
