from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.errors import ApplicationError
from backend.app.domain.errors import DomainViolation
from backend.app.domain.task_definition import (
    TaskConflictPolicy,
    TaskStorageMode,
    normalize_cron_expression,
)
from backend.app.domain.unpack import (
    UnpackDefinitionStatus,
    UnpackExecutionScopeKind,
    UnpackExecutionStatus,
    UnpackExecutionTrigger,
    UnpackSourceKind,
    UnpackTriggerKind,
    validate_auto_match_threshold_bps,
)
from backend.app.infrastructure.authorized_paths import AuthorizedPathScope
from backend.app.infrastructure.persistence.models import (
    Downloader,
    Site,
    UnpackDefinition,
    UnpackDefinitionSelectedSource,
    UnpackExecution,
    new_uuid,
    utc_now,
)


@dataclass(frozen=True, slots=True)
class UnpackSelectedSourceCreate:
    source_object_key: str
    canonical_path_hint: str
    filename: str
    size_bytes_at_selection: int | None = None


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
            version=1,
            created_at=now,
            updated_at=now,
        )
        with self._session_factory() as session:
            self._validate_foreign_references(session, normalized)
            session.add(definition)
            session.flush()
            for item in normalized.selected_sources:
                session.add(
                    UnpackDefinitionSelectedSource(
                        id=new_uuid(),
                        definition_id=definition.id,
                        source_object_key=item.source_object_key,
                        canonical_path_hint=item.canonical_path_hint,
                        filename=item.filename,
                        size_bytes_at_selection=item.size_bytes_at_selection,
                        created_at=now,
                    )
                )
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
                definition.status = UnpackDefinitionStatus.ENABLED.value
                definition.version += 1
                definition.updated_at = utc_now()
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

        selected_sources = request.selected_sources
        if request.execution_scope_kind is UnpackExecutionScopeKind.SELECTED_MEDIA:
            if (
                request.trigger_kind is not UnpackTriggerKind.MANUAL
                or request.source_kind is not UnpackSourceKind.DIRECTORY
            ):
                raise self._invalid("指定影片范围仅支持手动目录任务")
            if not selected_sources:
                raise self._invalid("选择影片模式至少需要勾选一个影视文件")
            normalized_selected = tuple(
                self._normalize_selected_source(item) for item in selected_sources
            )
        else:
            if selected_sources:
                raise self._invalid("全部影视文件模式不能同时提交指定影片清单")
            normalized_selected = ()

        file_filter = self._normalize_file_filter(request.file_filter)
        output_config = self._normalize_output_config(request.output_config)

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
            selected_sources=normalized_selected,
        )

    def _normalize_file_filter(self, raw: dict[str, Any]) -> dict[str, Any]:
        result = dict(raw)
        extensions = raw.get("extensions", ())
        if not isinstance(extensions, (list, tuple)):
            raise self._invalid("后缀名过滤必须是数组")
        normalized_extensions: list[str] = []
        for value in extensions:
            if not isinstance(value, str):
                raise self._invalid("后缀名过滤包含无效值")
            item = value.strip().lower()
            if not item:
                continue
            if "/" in item or "\\" in item or "\x00" in item or len(item) > 32:
                raise self._invalid("后缀名过滤包含无效值")
            if not item.startswith("."):
                item = f".{item}"
            if item not in normalized_extensions:
                normalized_extensions.append(item)
        result["extensions"] = normalized_extensions
        return result

    def _normalize_output_config(self, raw: dict[str, Any]) -> dict[str, Any]:
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
        return UnpackSelectedSourceCreate(
            source_object_key=key,
            canonical_path_hint=normalized_path,
            filename=filename,
            size_bytes_at_selection=item.size_bytes_at_selection,
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
