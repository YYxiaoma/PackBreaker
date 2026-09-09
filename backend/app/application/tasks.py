from __future__ import annotations

import stat
import unicodedata
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.analysis import AnalysisService, AnalysisSiteProvider
from backend.app.application.errors import ApplicationError
from backend.app.domain.errors import DomainViolation
from backend.app.domain.preflight import PreflightSnapshot
from backend.app.domain.task_state import TaskStatus
from backend.app.domain.task_units import SourceTaskFile, identify_task_units
from backend.app.infrastructure.persistence.preflight_repositories import (
    PreflightSnapshotRepository,
)
from backend.app.infrastructure.persistence.repositories import TaskCreate, TaskRepository
from backend.app.infrastructure.persistence.task_analysis_repositories import (
    TaskCandidateRepository,
    TaskUnitRepository,
)
from backend.app.infrastructure.source_inventory import (
    scan_source_inventory,
    source_inventory_digest,
)


@dataclass(frozen=True, slots=True)
class TaskUnitView:
    id: str
    normalized_unit_key: str
    kind: str
    source_root: str
    source_relative_path: str
    length: int
    source_inventory_digest: str
    descriptor: dict[str, Any]
    discovered_at: datetime


@dataclass(frozen=True, slots=True)
class TaskCandidateView:
    id: str
    snapshot_id: str
    normalized_unit_key: str
    site_id: str
    torrent_id: str
    display_name: str
    score: float
    rejected: bool
    selected_for_verification: bool
    verification_level: str | None
    metainfo_digest: str | None
    error_code: str | None
    evidence: dict[str, Any]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class PreflightView:
    id: str
    snapshot_digest: str
    payload: dict[str, Any]
    current: bool
    stale_reasons: tuple[str, ...]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class TaskView:
    id: str
    type: str
    source_downloader_id: str
    source_hash: str
    normalized_unit_key: str
    status: str
    error_code: str | None
    version: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class TaskCreateView:
    task: TaskView
    created: bool


class TaskAnalysisService:
    """任务级 M2 入口：安全定位 /data、持久化 unit/candidate，并判断 preflight 当前性。"""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        site_service: AnalysisSiteProvider,
        *,
        data_root: Path,
    ) -> None:
        self._session_factory = session_factory
        self._site_service = site_service
        self._data_root = data_root
        self._analysis = AnalysisService(session_factory, site_service)

    def list_tasks(self, *, status: TaskStatus | None = None, limit: int = 100) -> list[TaskView]:
        with self._session_factory() as session:
            records = TaskRepository(session).list_recent(status=status, limit=limit)
            return [self._task_view(item) for item in records]

    def get_task(self, task_id: str) -> TaskView:
        with self._session_factory() as session:
            task = TaskRepository(session).get(task_id)
            if task is None:
                raise _task_not_found()
            return self._task_view(task)

    def create_task(
        self,
        *,
        task_type: str,
        source_downloader_id: str,
        source_hash: str,
        normalized_unit_key: str,
    ) -> TaskCreateView:
        normalized_values = tuple(
            value.strip()
            for value in (task_type, source_downloader_id, source_hash, normalized_unit_key)
        )
        if any(not value for value in normalized_values):
            raise ApplicationError(
                code="TASK_INPUT_INVALID",
                status=422,
                title="任务输入无效",
                detail="任务类型、来源下载器、source hash 与处理单元 key 均不能为空",
            )
        normalized_type, normalized_downloader, normalized_hash, normalized_unit = normalized_values
        request = TaskCreate(
            task_type=normalized_type,
            source_downloader_id=normalized_downloader,
            source_hash=normalized_hash,
            normalized_unit_key=normalized_unit,
            trace_id=str(uuid4()),
        )
        with self._session_factory() as session:
            task, created = TaskRepository(session).create_or_get(request)
            session.commit()
            return TaskCreateView(task=self._task_view(task), created=created)

    async def analyze(self, task_id: str, *, source_root: str) -> PreflightSnapshot:
        normalized_root, resolved_root = self._resolve_source_root(source_root)
        task_key = self._task_unit_key(task_id)
        try:
            inventory = scan_source_inventory(resolved_root)
        except DomainViolation as exc:
            raise ApplicationError(
                code=exc.code.value,
                status=422,
                title="源目录扫描失败",
                detail=str(exc),
            ) from exc
        inventory_digest = source_inventory_digest(inventory)
        units = identify_task_units(
            tuple(SourceTaskFile(item.relative_path, item.length) for item in inventory)
        )
        selected = next((item for item in units if item.normalized_unit_key == task_key), None)
        if selected is None:
            raise ApplicationError(
                code="ANALYSIS_UNIT_NOT_FOUND",
                status=409,
                title="处理单元未找到",
                detail="当前源目录中没有与任务 normalized_unit_key 匹配的媒体单元",
            )
        with self._session_factory() as session:
            TaskUnitRepository(session).record_batch(
                task_id=task_id,
                source_root=normalized_root,
                source_inventory_digest=inventory_digest,
                units=units,
            )
            session.commit()
        try:
            return await self._analysis.analyze(
                task_id=task_id,
                unit=selected,
                source_root=resolved_root,
                source_inventory=inventory,
            )
        except DomainViolation as exc:
            raise ApplicationError(
                code=exc.code.value,
                status=409,
                title="分析安全检查失败",
                detail=str(exc),
            ) from exc

    def list_units(self, task_id: str) -> list[TaskUnitView]:
        self._require_task(task_id)
        with self._session_factory() as session:
            records = TaskUnitRepository(session).list_latest(task_id)
            return [self._unit_view(item) for item in records]

    def list_candidates(self, task_id: str) -> list[TaskCandidateView]:
        self._require_task(task_id)
        with self._session_factory() as session:
            records = TaskCandidateRepository(session).list_for_latest_snapshot(task_id)
            return [self._candidate_view(item) for item in records]

    def latest_preflight(self, task_id: str) -> PreflightView:
        task_version = self._task_version(task_id)
        with self._session_factory() as session:
            record = PreflightSnapshotRepository(session).latest_for_task(task_id)
            if record is None:
                raise ApplicationError(
                    code="PREFLIGHT_NOT_FOUND",
                    status=404,
                    title="预演不存在",
                    detail="该任务尚未生成 preflight snapshot",
                )
            unit = TaskUnitRepository(session).get_for_snapshot(
                task_id=task_id,
                normalized_unit_key=record.normalized_unit_key,
                source_inventory_digest=record.source_inventory_digest,
            )
            payload = deepcopy(record.payload)
            record_id = record.id
            digest = record.snapshot_digest
            created_at = record.created_at
            record_task_version = record.task_version

        reasons: list[str] = []
        if task_version != record_task_version:
            reasons.append("TASK_VERSION_CHANGED")
        if unit is None:
            reasons.append("UNIT_RECORD_MISSING")
        else:
            try:
                _, source_path = self._resolve_source_root(unit.source_root)
                current_inventory = scan_source_inventory(source_path)
                if source_inventory_digest(current_inventory) != record.source_inventory_digest:
                    reasons.append("SOURCE_CHANGED")
            except (ApplicationError, DomainViolation):
                reasons.append("SOURCE_UNAVAILABLE")

        expected_site_versions = _site_versions_from_payload(payload)
        if expected_site_versions is None:
            reasons.append("PREFLIGHT_EVIDENCE_INVALID")
        elif tuple(sorted(self._site_service.enabled_site_versions())) != expected_site_versions:
            reasons.append("SITE_CONFIG_CHANGED")

        return PreflightView(
            id=record_id,
            snapshot_digest=digest,
            payload=payload,
            current=not reasons,
            stale_reasons=tuple(reasons),
            created_at=created_at,
        )

    def _task_unit_key(self, task_id: str) -> str:
        with self._session_factory() as session:
            task = TaskRepository(session).get(task_id)
            if task is None:
                raise _task_not_found()
            return task.normalized_unit_key

    def _task_version(self, task_id: str) -> int:
        with self._session_factory() as session:
            task = TaskRepository(session).get(task_id)
            if task is None:
                raise _task_not_found()
            return task.version

    def _require_task(self, task_id: str) -> None:
        self._task_version(task_id)

    def _resolve_source_root(self, value: str) -> tuple[str, Path]:
        normalized = unicodedata.normalize("NFC", value.strip())
        has_windows_drive = (
            len(normalized) >= 2 and normalized[0].isalpha() and normalized[1] == ":"
        )
        if (
            not normalized
            or "\x00" in normalized
            or "\\" in normalized
            or normalized.startswith("/")
            or has_windows_drive
        ):
            raise _source_root_invalid("source_root 必须是 /data 下的 POSIX 相对目录")
        if normalized == ".":
            parts: tuple[str, ...] = ()
        else:
            parts = tuple(normalized.split("/"))
            if any(not part or part in {".", ".."} for part in parts):
                raise _source_root_invalid("source_root 包含不安全路径段")

        try:
            base_stat = self._data_root.stat(follow_symlinks=False)
        except OSError as exc:
            raise _source_root_invalid("数据根目录不可用") from exc
        if stat.S_ISLNK(base_stat.st_mode) or not stat.S_ISDIR(base_stat.st_mode):
            raise _source_root_invalid("数据根目录必须是真实目录且不能是符号链接")
        try:
            base = self._data_root.resolve(strict=True)
        except OSError as exc:
            raise _source_root_invalid("数据根目录不可用") from exc
        current = self._data_root
        for index, part in enumerate(parts):
            current = current / part
            try:
                item_stat = current.stat(follow_symlinks=False)
            except OSError as exc:
                raise ApplicationError(
                    code="ANALYSIS_SOURCE_ROOT_NOT_FOUND",
                    status=404,
                    title="源目录不存在",
                    detail="source_root 指向的目录不可见",
                ) from exc
            if stat.S_ISLNK(item_stat.st_mode):
                raise _source_root_invalid("source_root 不能经过符号链接")
            if index < len(parts) - 1 and not stat.S_ISDIR(item_stat.st_mode):
                raise _source_root_invalid("source_root 的中间路径不是目录")
        resolved = current.resolve(strict=True)
        if not resolved.is_relative_to(base) or not resolved.is_dir():
            raise _source_root_invalid("source_root 必须解析到 /data 内的真实目录")
        return ("." if not parts else "/".join(parts), resolved)

    @staticmethod
    def _unit_view(record: Any) -> TaskUnitView:
        return TaskUnitView(
            id=record.id,
            normalized_unit_key=record.normalized_unit_key,
            kind=record.kind,
            source_root=record.source_root,
            source_relative_path=record.source_relative_path,
            length=record.length,
            source_inventory_digest=record.source_inventory_digest,
            descriptor=deepcopy(record.descriptor),
            discovered_at=record.discovered_at,
        )

    @staticmethod
    def _candidate_view(record: Any) -> TaskCandidateView:
        return TaskCandidateView(
            id=record.id,
            snapshot_id=record.preflight_snapshot_id,
            normalized_unit_key=record.normalized_unit_key,
            site_id=record.site_id,
            torrent_id=record.torrent_id,
            display_name=record.display_name,
            score=record.score,
            rejected=record.rejected,
            selected_for_verification=record.selected_for_verification,
            verification_level=record.verification_level,
            metainfo_digest=record.metainfo_digest,
            error_code=record.error_code,
            evidence=deepcopy(record.evidence),
            created_at=record.created_at,
        )

    @staticmethod
    def _task_view(record: Any) -> TaskView:
        return TaskView(
            id=record.id,
            type=record.type,
            source_downloader_id=record.source_downloader_id,
            source_hash=record.source_hash,
            normalized_unit_key=record.normalized_unit_key,
            status=record.status,
            error_code=record.error_code,
            version=record.version,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )


def _site_versions_from_payload(payload: dict[str, Any]) -> tuple[tuple[str, int], ...] | None:
    raw = payload.get("site_versions")
    if not isinstance(raw, list):
        return None
    values: list[tuple[str, int]] = []
    for item in raw:
        if (
            not isinstance(item, list)
            or len(item) != 2
            or not isinstance(item[0], str)
            or not isinstance(item[1], int)
            or item[1] < 1
        ):
            return None
        values.append((item[0], item[1]))
    return tuple(sorted(values))


def _task_not_found() -> ApplicationError:
    return ApplicationError(
        code="TASK_NOT_FOUND",
        status=404,
        title="任务不存在",
        detail="未找到指定任务",
    )


def _source_root_invalid(detail: str) -> ApplicationError:
    return ApplicationError(
        code="ANALYSIS_SOURCE_ROOT_INVALID",
        status=422,
        title="源目录无效",
        detail=detail,
    )
