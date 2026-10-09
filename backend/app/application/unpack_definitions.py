from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.errors import ApplicationError
from backend.app.application.unpack_source_scans import (
    UnpackSourceScanService,
    normalize_unpack_file_filter,
)
from backend.app.domain.errors import DomainViolation
from backend.app.domain.task_definition import (
    TaskConflictPolicy,
    TaskStorageMode,
    next_cron_run,
    normalize_cron_expression,
)
from backend.app.domain.unpack import (
    TERMINAL_EXECUTION_STATUSES,
    UnpackDefinitionStatus,
    UnpackExecutionScopeKind,
    UnpackExecutionStatus,
    UnpackExecutionTrigger,
    UnpackSourceKind,
    UnpackTriggerKind,
    validate_auto_match_threshold_bps,
)
from backend.app.infrastructure.authorized_paths import AuthorizedPathScope
from backend.app.infrastructure.persistence.database import begin_immediate_write
from backend.app.infrastructure.persistence.models import (
    Downloader,
    Site,
    UnpackDefinition,
    UnpackDefinitionSelectedSource,
    UnpackExecution,
    UnpackExecutionItem,
    UnpackExternalOperationJournal,
    UnpackSourceScan,
    new_uuid,
    utc_now,
)
from backend.app.infrastructure.source_inventory import current_file_snapshot


@dataclass(frozen=True, slots=True)
class UnpackSelectedSourceCreate:
    source_object_key: str
    canonical_path_hint: str
    filename: str
    size_bytes_at_selection: int | None = None
    source_snapshot: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class UnpackDefinitionCreate:
    name: str
    trigger_kind: UnpackTriggerKind
    source_kind: UnpackSourceKind
    execution_scope_kind: UnpackExecutionScopeKind
    source_config: dict[str, Any]
    file_filter: dict[str, Any]
    site_ids: tuple[str, ...]
    output_config: dict[str, Any]
    retry_enabled: bool = True
    max_retries: int = 3
    auto_match_threshold_bps: int = 10_000
    cron_expression: str | None = None
    timezone: str | None = None
    source_scan_id: str | None = None
    selected_sources: tuple[UnpackSelectedSourceCreate, ...] = ()


@dataclass(frozen=True, slots=True)
class UnpackDefinitionView:
    id: str
    name: str
    trigger_kind: UnpackTriggerKind
    status: UnpackDefinitionStatus
    source_kind: UnpackSourceKind
    execution_scope_kind: UnpackExecutionScopeKind
    source_config: dict[str, Any]
    file_filter: dict[str, Any]
    site_ids: tuple[str, ...]
    output_config: dict[str, Any]
    retry_enabled: bool
    max_retries: int
    auto_match_threshold_bps: int
    cron_expression: str | None
    timezone: str | None
    next_run_at: datetime | None
    last_triggered_at: datetime | None
    selected_source_count: int
    version: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class UnpackRunResult:
    definition: UnpackDefinitionView
    execution_id: str | None


class UnpackDefinitionService:
    """v1.0.15 数据拆包定义：保存与执行严格分离。"""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        data_root: Path,
        timezone: str,
        path_scope: AuthorizedPathScope | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._timezone = timezone
        self._path_scope = path_scope or AuthorizedPathScope.legacy_only(legacy_data_root=data_root)
        self._source_scans = UnpackSourceScanService(
            session_factory,
            path_scope=self._path_scope,
        )

    def create(self, request: UnpackDefinitionCreate) -> UnpackDefinitionView:
        normalized = self._normalize_request(request)
        now = utc_now()
        definition = UnpackDefinition(
            id=new_uuid(),
            name=normalized.name,
            trigger_kind=normalized.trigger_kind.value,
            status=UnpackDefinitionStatus.PENDING_EXECUTION.value,
            source_kind=normalized.source_kind.value,
            execution_scope_kind=normalized.execution_scope_kind.value,
            source_config=normalized.source_config,
            file_filter=normalized.file_filter,
            site_ids=list(normalized.site_ids),
            output_config=normalized.output_config,
            retry_enabled=normalized.retry_enabled,
            max_retries=normalized.max_retries,
            auto_match_threshold_bps=normalized.auto_match_threshold_bps,
            cron_expression=normalized.cron_expression,
            timezone=normalized.timezone,
            next_run_at=None,
            last_triggered_at=None,
            version=1,
            created_at=now,
            updated_at=now,
        )
        with self._session_factory() as session:
            self._validate_foreign_references(session, normalized)
            materialized_sources, consumed_scan = self._materialize_selected_sources(
                session,
                normalized,
            )
            session.add(definition)
            session.flush()
            for item in materialized_sources:
                session.add(
                    UnpackDefinitionSelectedSource(
                        id=new_uuid(),
                        definition_id=definition.id,
                        source_object_key=item.source_object_key,
                        canonical_path_hint=item.canonical_path_hint,
                        filename=item.filename,
                        size_bytes_at_selection=item.size_bytes_at_selection,
                        source_snapshot=item.source_snapshot or {},
                        created_at=now,
                    )
                )
            if consumed_scan is not None:
                self._source_scans.consume_scan(session, consumed_scan)
            session.commit()
            return self._view(session, definition)

    def get(self, definition_id: str) -> UnpackDefinitionView:
        with self._session_factory() as session:
            definition = self._require_definition(session, definition_id)
            return self._view(session, definition)

    def list(self) -> tuple[UnpackDefinitionView, ...]:
        with self._session_factory() as session:
            records = session.scalars(
                select(UnpackDefinition).order_by(
                    UnpackDefinition.updated_at.desc(),
                    UnpackDefinition.id.desc(),
                )
            ).all()
            return tuple(self._view(session, item) for item in records)

    def update(
        self, definition_id: str, request: UnpackDefinitionCreate, *, expected_version: int
    ) -> UnpackDefinitionView:
        """Only future executions see edited settings; existing snapshots are immutable."""
        with self._session_factory() as session:
            begin_immediate_write(session)
            definition = self._require_definition(session, definition_id)
            if definition.version != expected_version:
                raise self._conflict("任务配置已被其他操作修改，请刷新后重试")
            selected = session.scalars(
                select(UnpackDefinitionSelectedSource).where(
                    UnpackDefinitionSelectedSource.definition_id == definition.id
                )
            ).all()
            if selected:
                if request.source_scan_id is not None:
                    raise self._invalid("编辑指定影片任务不能更换扫描清单")
                request = UnpackDefinitionCreate(
                    name=request.name,
                    trigger_kind=request.trigger_kind,
                    source_kind=request.source_kind,
                    execution_scope_kind=request.execution_scope_kind,
                    source_config=request.source_config,
                    file_filter=request.file_filter,
                    site_ids=request.site_ids,
                    output_config=request.output_config,
                    retry_enabled=request.retry_enabled,
                    max_retries=request.max_retries,
                    auto_match_threshold_bps=request.auto_match_threshold_bps,
                    cron_expression=request.cron_expression,
                    timezone=request.timezone,
                    selected_sources=tuple(
                        UnpackSelectedSourceCreate(
                            source_object_key=item.source_object_key,
                            canonical_path_hint=item.canonical_path_hint,
                            filename=item.filename,
                            size_bytes_at_selection=item.size_bytes_at_selection,
                            source_snapshot=item.source_snapshot,
                        )
                        for item in selected
                    ),
                )
            normalized = self._normalize_request(request)
            if selected and (
                normalized.execution_scope_kind.value != definition.execution_scope_kind
                or normalized.source_kind.value != definition.source_kind
                or normalized.trigger_kind.value != definition.trigger_kind
                or normalized.source_config != definition.source_config
                or normalized.file_filter != definition.file_filter
            ):
                raise self._conflict("指定影片任务的来源和过滤条件已冻结，请新建任务重新选片")
            self._validate_foreign_references(session, normalized)
            now = utc_now()
            definition.name = normalized.name
            definition.trigger_kind = normalized.trigger_kind.value
            definition.source_kind = normalized.source_kind.value
            definition.execution_scope_kind = normalized.execution_scope_kind.value
            definition.source_config = normalized.source_config
            definition.file_filter = normalized.file_filter
            definition.site_ids = list(normalized.site_ids)
            definition.output_config = normalized.output_config
            definition.retry_enabled = normalized.retry_enabled
            definition.max_retries = normalized.max_retries
            definition.auto_match_threshold_bps = normalized.auto_match_threshold_bps
            definition.cron_expression = normalized.cron_expression
            definition.timezone = normalized.timezone
            if definition.status == UnpackDefinitionStatus.ENABLED.value:
                if normalized.trigger_kind is UnpackTriggerKind.MONITOR:
                    assert normalized.cron_expression and normalized.timezone
                    definition.next_run_at = next_cron_run(
                        normalized.cron_expression, now, timezone=normalized.timezone
                    )
                else:
                    definition.status = UnpackDefinitionStatus.PENDING_EXECUTION.value
                    definition.next_run_at = None
            definition.updated_at = now
            definition.version += 1
            session.commit()
            return self._view(session, definition)

    def delete(self, definition_id: str, *, expected_version: int) -> None:
        """Never erase active executions or durable external-side-effect journals."""
        with self._session_factory() as session:
            begin_immediate_write(session)
            definition = self._require_definition(session, definition_id)
            if definition.version != expected_version:
                raise self._conflict("任务配置已更新，请刷新后确认删除")
            execution_ids = session.scalars(
                select(UnpackExecution.id).where(UnpackExecution.definition_id == definition_id)
            ).all()
            if session.scalar(
                select(UnpackExecution.id)
                .where(UnpackExecution.definition_id == definition_id)
                .where(
                    UnpackExecution.status.not_in(
                        tuple(value.value for value in TERMINAL_EXECUTION_STATUSES)
                    )
                )
                .limit(1)
            ):
                raise self._conflict("存在运行中或待审核的执行记录，请先处理，禁止删除")
            if execution_ids and session.scalar(
                select(UnpackExternalOperationJournal.id)
                .join(
                    UnpackExecutionItem,
                    UnpackExecutionItem.id == UnpackExternalOperationJournal.item_id,
                )
                .where(UnpackExecutionItem.execution_id.in_(execution_ids))
                .limit(1)
            ):
                raise self._conflict("任务存在外部操作 journal，必须保留追溯与恢复证据，禁止删除")
            session.delete(definition)
            session.commit()

    def run(self, definition_id: str) -> UnpackRunResult:
        with self._session_factory() as session:
            definition = self._require_definition(session, definition_id)
            if definition.status not in {
                UnpackDefinitionStatus.PENDING_EXECUTION.value,
                UnpackDefinitionStatus.PAUSED.value,
            }:
                raise self._conflict("当前任务状态不能执行")

            trigger_kind = UnpackTriggerKind(definition.trigger_kind)
            if trigger_kind is UnpackTriggerKind.MONITOR:
                cron_expression = definition.cron_expression
                timezone = definition.timezone
                if not cron_expression or not timezone:
                    raise self._conflict("监控任务缺少有效 Cron 或时区配置")
                now = utc_now()
                definition.status = UnpackDefinitionStatus.ENABLED.value
                definition.next_run_at = next_cron_run(
                    cron_expression,
                    now,
                    timezone=timezone,
                )
                definition.version += 1
                definition.updated_at = now
                session.commit()
                return UnpackRunResult(self._view(session, definition), None)

            running = session.scalar(
                select(UnpackExecution.id)
                .where(UnpackExecution.definition_id == definition.id)
                .where(
                    UnpackExecution.status.not_in(
                        (
                            UnpackExecutionStatus.COMPLETED.value,
                            UnpackExecutionStatus.COMPLETED_WITH_ERRORS.value,
                            UnpackExecutionStatus.FAILED.value,
                            UnpackExecutionStatus.CANCELLED.value,
                        )
                    )
                )
                .limit(1)
            )
            if running is not None:
                raise self._conflict("该任务已有未结束的执行，不能重复启动")

            now = utc_now()
            execution = UnpackExecution(
                id=new_uuid(),
                definition_id=definition.id,
                trigger=UnpackExecutionTrigger.MANUAL.value,
                status=UnpackExecutionStatus.DISCOVERING.value,
                config_snapshot=self._config_snapshot(session, definition),
                discovery_complete=False,
                total_count=0,
                matched_auto_count=0,
                review_count=0,
                content_verified_count=0,
                content_mismatch_count=0,
                timeout_count=0,
                error_count=0,
                completed_count=0,
                started_at=now,
                version=1,
                created_at=now,
                updated_at=now,
            )
            session.add(execution)
            session.commit()
            return UnpackRunResult(self._view(session, definition), execution.id)

    def _normalize_request(self, request: UnpackDefinitionCreate) -> UnpackDefinitionCreate:
        name = request.name.strip()
        if not name or len(name) > 120:
            raise self._invalid("任务名称不能为空且最长 120 个字符")
        if request.max_retries < 0 or request.max_retries > 10:
            raise self._invalid("自动重试次数必须位于 0 到 10 之间")
        try:
            threshold = validate_auto_match_threshold_bps(request.auto_match_threshold_bps)
        except ValueError as exc:
            raise self._invalid(str(exc)) from exc

        site_ids = tuple(dict.fromkeys(item.strip() for item in request.site_ids if item.strip()))
        if not site_ids:
            raise self._invalid("至少选择一个扫描站点")

        if request.trigger_kind is UnpackTriggerKind.MANUAL:
            if request.source_kind is not UnpackSourceKind.DIRECTORY:
                raise self._invalid("手动拆包当前仅支持目录来源")
            if request.cron_expression is not None and request.cron_expression.strip():
                raise self._invalid("手动拆包任务不能配置 Cron")
            cron_expression = None
            timezone = None
        else:
            if request.cron_expression is None or not request.cron_expression.strip():
                raise self._invalid("监控拆包任务必须配置 Cron")
            try:
                cron_expression = normalize_cron_expression(request.cron_expression)
            except ValueError as exc:
                raise self._invalid(str(exc)) from exc
            timezone = (request.timezone or self._timezone).strip()
            if not timezone:
                raise self._invalid("监控拆包任务必须配置时区")
            try:
                next_cron_run(cron_expression, utc_now(), timezone=timezone)
            except (ValueError, ZoneInfoNotFoundError) as exc:
                raise self._invalid("监控拆包任务时区或 Cron 无效") from exc

        source_config = dict(request.source_config)
        if request.source_kind is UnpackSourceKind.DIRECTORY:
            raw_directory = source_config.get("directory_path")
            if not isinstance(raw_directory, str) or not raw_directory.strip():
                raise self._invalid("目录来源必须选择来源目录")
            try:
                normalized_directory, _ = self._path_scope.resolve_existing_directory(raw_directory)
            except DomainViolation as exc:
                raise self._invalid(str(exc)) from exc
            source_config["directory_path"] = normalized_directory
        else:
            downloader_id = source_config.get("downloader_id")
            if not isinstance(downloader_id, str) or not downloader_id.strip():
                raise self._invalid("下载器来源必须选择下载器")
            source_config["downloader_id"] = downloader_id.strip()
            source_config = self._normalize_downloader_source_config(source_config)

        selected_sources = request.selected_sources
        if request.execution_scope_kind is UnpackExecutionScopeKind.SELECTED_MEDIA:
            if (
                request.trigger_kind is not UnpackTriggerKind.MANUAL
                or request.source_kind is not UnpackSourceKind.DIRECTORY
            ):
                raise self._invalid("指定影片范围仅支持手动目录任务")
            if not request.source_scan_id and not selected_sources:
                raise self._invalid("选择影片模式至少需要勾选一个影视文件")
            if request.source_scan_id and selected_sources:
                raise self._invalid("选择影片模式必须且只能提交一个目录扫描结果或影片清单")
            normalized_selected = tuple(
                self._normalize_selected_source(item) for item in selected_sources
            )
            source_root = Path(str(source_config["directory_path"]))
            if any(
                not Path(item.canonical_path_hint).is_relative_to(source_root)
                for item in normalized_selected
            ):
                raise self._invalid("选定影片必须位于当前来源目录内")
        else:
            if selected_sources or request.source_scan_id is not None:
                raise self._invalid("全部影视文件模式不能同时提交指定影片清单或目录扫描")
            normalized_selected = ()

        file_filter = normalize_unpack_file_filter(request.file_filter)
        output_config = self._normalize_output_config(
            request.output_config,
            default_target_downloader_id=(
                str(source_config["downloader_id"])
                if request.source_kind is UnpackSourceKind.DOWNLOADER
                else None
            ),
        )

        return UnpackDefinitionCreate(
            name=name,
            trigger_kind=request.trigger_kind,
            source_kind=request.source_kind,
            execution_scope_kind=request.execution_scope_kind,
            source_config=source_config,
            file_filter=file_filter,
            site_ids=site_ids,
            output_config=output_config,
            retry_enabled=request.retry_enabled,
            max_retries=request.max_retries,
            auto_match_threshold_bps=threshold,
            cron_expression=cron_expression,
            timezone=timezone,
            source_scan_id=request.source_scan_id.strip() if request.source_scan_id else None,
            selected_sources=normalized_selected,
        )

    def _normalize_downloader_source_config(self, raw: dict[str, Any]) -> dict[str, Any]:
        result = dict(raw)
        raw_name = result.get("name_contains")
        if raw_name is not None and not isinstance(raw_name, str):
            raise self._invalid("下载器任务名称过滤必须是文本")
        name_contains = raw_name.strip() if isinstance(raw_name, str) else ""
        if len(name_contains) > 255:
            raise self._invalid("下载器任务名称过滤不能超过 255 个字符")
        result["name_contains"] = name_contains or None

        for field_name, label in (("categories", "分类"), ("tags", "标签")):
            raw_values = result.get(field_name, [])
            if not isinstance(raw_values, (list, tuple)) or any(
                not isinstance(value, str) for value in raw_values
            ):
                raise self._invalid(f"下载器{label}过滤必须是文本数组")
            normalized_values = tuple(
                dict.fromkeys(value.strip() for value in raw_values if value.strip())
            )
            if len(normalized_values) > 100:
                raise self._invalid(f"下载器{label}过滤最多支持 100 项")
            result[field_name] = list(normalized_values)
        return result

    def _normalize_output_config(
        self,
        raw: dict[str, Any],
        *,
        default_target_downloader_id: str | None,
    ) -> dict[str, Any]:
        result = dict(raw)
        output_directory = result.get("output_directory")
        if not isinstance(output_directory, str) or not output_directory.strip():
            raise self._invalid("必须配置输出目录")
        try:
            normalized, _anchor, _exists = self._path_scope.resolve_output_anchor(output_directory)
        except DomainViolation as exc:
            raise self._invalid(str(exc)) from exc
        result["output_directory"] = normalized

        try:
            storage_mode = TaskStorageMode(
                str(result.get("storage_mode", TaskStorageMode.HARDLINK))
            )
            conflict_policy = TaskConflictPolicy(
                str(result.get("conflict_policy", TaskConflictPolicy.VERIFY_REUSE_OR_STOP))
            )
        except ValueError as exc:
            raise self._invalid("输出存放方式或文件冲突策略无效") from exc
        result["storage_mode"] = storage_mode.value
        result["conflict_policy"] = conflict_policy.value
        raw_target_downloader_id = result.get("target_downloader_id")
        if raw_target_downloader_id is None or (
            isinstance(raw_target_downloader_id, str) and not raw_target_downloader_id.strip()
        ):
            raw_target_downloader_id = default_target_downloader_id
        if (
            not isinstance(raw_target_downloader_id, str)
            or not raw_target_downloader_id.strip()
            or len(raw_target_downloader_id.strip()) > 36
        ):
            raise self._invalid("必须选择目标下载器")
        result["target_downloader_id"] = raw_target_downloader_id.strip()
        return result

    def _normalize_selected_source(
        self, item: UnpackSelectedSourceCreate
    ) -> UnpackSelectedSourceCreate:
        key = item.source_object_key.strip()
        filename = item.filename.strip()
        path = item.canonical_path_hint.strip()
        if not key or len(key) > 512:
            raise self._invalid("选定影片的源对象标识无效")
        if not filename or not path:
            raise self._invalid("选定影片缺少文件信息")
        if item.size_bytes_at_selection is not None and item.size_bytes_at_selection < 0:
            raise self._invalid("选定影片大小无效")
        try:
            normalized_path = self._path_scope.normalize_reference(path)
        except DomainViolation as exc:
            raise self._invalid(str(exc)) from exc
        candidate_path = Path(normalized_path)
        try:
            self._path_scope.resolve_existing_directory(candidate_path.parent.as_posix())
            snapshot = current_file_snapshot(candidate_path)
        except DomainViolation as exc:
            raise self._invalid(str(exc)) from exc
        if (
            item.size_bytes_at_selection is not None
            and item.size_bytes_at_selection != snapshot.size
        ):
            raise self._invalid("选定影片大小与当前文件快照不一致，请重新扫描")
        return UnpackSelectedSourceCreate(
            source_object_key=key,
            canonical_path_hint=normalized_path,
            filename=filename,
            size_bytes_at_selection=snapshot.size,
            source_snapshot={
                "device": snapshot.device,
                "inode": snapshot.inode,
                "size": snapshot.size,
                "mtime_ns": str(snapshot.mtime_ns),
                "file_type": snapshot.file_type,
            },
        )

    def _validate_foreign_references(
        self,
        session: Session,
        request: UnpackDefinitionCreate,
    ) -> None:
        existing_sites = set(
            session.scalars(select(Site.id).where(Site.id.in_(request.site_ids))).all()
        )
        missing_sites = [site_id for site_id in request.site_ids if site_id not in existing_sites]
        if missing_sites:
            raise self._invalid("包含不存在的扫描站点")

        if request.source_kind is UnpackSourceKind.DOWNLOADER:
            downloader_id = str(request.source_config["downloader_id"])
            if session.get(Downloader, downloader_id) is None:
                raise self._invalid("所选下载器不存在")
        target_downloader_id = request.output_config.get("target_downloader_id")
        if not isinstance(target_downloader_id, str):
            raise self._invalid("必须选择目标下载器")
        if session.get(Downloader, target_downloader_id) is None:
            raise self._invalid("所选目标下载器不存在")

    def _materialize_selected_sources(
        self,
        session: Session,
        request: UnpackDefinitionCreate,
    ) -> tuple[tuple[UnpackSelectedSourceCreate, ...], UnpackSourceScan | None]:
        if request.execution_scope_kind is not UnpackExecutionScopeKind.SELECTED_MEDIA:
            return (), None
        if request.selected_sources:
            return request.selected_sources, None
        assert request.source_scan_id is not None
        directory_path = request.source_config.get("directory_path")
        assert isinstance(directory_path, str)
        scan = session.get(UnpackSourceScan, request.source_scan_id)
        if scan is None:
            raise ApplicationError(
                code="UNPACK_SOURCE_SCAN_NOT_FOUND",
                status=404,
                title="目录扫描不存在",
                detail="目录扫描不存在或已经失效，请重新扫描",
            )
        selected = self._source_scans.selected_sources(
            session,
            scan_id=scan.id,
            directory_path=directory_path,
            file_filter=request.file_filter,
        )
        return (
            tuple(
                UnpackSelectedSourceCreate(
                    source_object_key=item.source_object_key,
                    canonical_path_hint=item.canonical_path_hint,
                    filename=item.filename,
                    size_bytes_at_selection=item.size_bytes,
                    source_snapshot={
                        "device": item.device,
                        "inode": item.inode,
                        "size": item.size_bytes,
                        "mtime_ns": item.mtime_ns,
                        "file_type": "regular",
                    },
                )
                for item in selected
            ),
            scan,
        )

    def _config_snapshot(
        self,
        session: Session,
        definition: UnpackDefinition,
    ) -> dict[str, Any]:
        selected = session.scalars(
            select(UnpackDefinitionSelectedSource)
            .where(UnpackDefinitionSelectedSource.definition_id == definition.id)
            .order_by(UnpackDefinitionSelectedSource.id)
        ).all()
        return {
            "definition_id": definition.id,
            "definition_version": definition.version,
            "name": definition.name,
            "trigger_kind": definition.trigger_kind,
            "source_kind": definition.source_kind,
            "execution_scope_kind": definition.execution_scope_kind,
            "source_config": dict(definition.source_config),
            "file_filter": dict(definition.file_filter),
            "site_ids": list(definition.site_ids),
            "output_config": dict(definition.output_config),
            "retry": {
                "enabled": definition.retry_enabled,
                "max_retries": definition.max_retries,
            },
            "matching": {
                "auto_match_threshold_bps": definition.auto_match_threshold_bps,
            },
            "selected_sources": [
                {
                    "source_object_key": item.source_object_key,
                    "canonical_path_hint": item.canonical_path_hint,
                    "filename": item.filename,
                    "size_bytes_at_selection": item.size_bytes_at_selection,
                    "source_snapshot": dict(item.source_snapshot or {}),
                }
                for item in selected
            ],
        }

    def _view(self, session: Session, definition: UnpackDefinition) -> UnpackDefinitionView:
        selected_count = len(
            session.scalars(
                select(UnpackDefinitionSelectedSource.id).where(
                    UnpackDefinitionSelectedSource.definition_id == definition.id
                )
            ).all()
        )
        return UnpackDefinitionView(
            id=definition.id,
            name=definition.name,
            trigger_kind=UnpackTriggerKind(definition.trigger_kind),
            status=UnpackDefinitionStatus(definition.status),
            source_kind=UnpackSourceKind(definition.source_kind),
            execution_scope_kind=UnpackExecutionScopeKind(definition.execution_scope_kind),
            source_config=dict(definition.source_config),
            file_filter=dict(definition.file_filter),
            site_ids=tuple(definition.site_ids),
            output_config=dict(definition.output_config),
            retry_enabled=definition.retry_enabled,
            max_retries=definition.max_retries,
            auto_match_threshold_bps=definition.auto_match_threshold_bps,
            cron_expression=definition.cron_expression,
            timezone=definition.timezone,
            next_run_at=definition.next_run_at,
            last_triggered_at=definition.last_triggered_at,
            selected_source_count=selected_count,
            version=definition.version,
            created_at=definition.created_at,
            updated_at=definition.updated_at,
        )

    @staticmethod
    def _require_definition(session: Session, definition_id: str) -> UnpackDefinition:
        definition = session.get(UnpackDefinition, definition_id)
        if definition is None:
            raise ApplicationError(
                code="UNPACK_DEFINITION_NOT_FOUND",
                status=404,
                title="数据拆包任务不存在",
                detail="未找到指定数据拆包任务",
            )
        return definition

    @staticmethod
    def _invalid(detail: str) -> ApplicationError:
        return ApplicationError(
            code="UNPACK_DEFINITION_INVALID",
            status=422,
            title="数据拆包任务配置无效",
            detail=detail,
        )

    @staticmethod
    def _conflict(detail: str) -> ApplicationError:
        return ApplicationError(
            code="UNPACK_DEFINITION_CONFLICT",
            status=409,
            title="数据拆包任务状态冲突",
            detail=detail,
        )
