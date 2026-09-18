from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.errors import ApplicationError
from backend.app.application.task_definition_executions import (
    filter_source_inventory,
    reconcile_task_execution,
)
from backend.app.domain.downloader import (
    PathMappingRule,
    map_remote_path,
    reverse_map_container_path_unique,
)
from backend.app.domain.errors import DomainViolation
from backend.app.domain.task_definition import (
    DEFAULT_ARCHIVE_EXTENSIONS,
    DEFAULT_EXCLUDE_NAMES,
    DEFAULT_RETRY_INTERVALS_SECONDS,
    DEFAULT_TEMP_PATTERNS,
    DEFAULT_VIDEO_EXTENSIONS,
    TaskConflictPolicy,
    TaskDefinitionKind,
    TaskDefinitionStatus,
    TaskInitialScope,
    TaskOverlapPolicy,
    TaskSourceKind,
    TaskStorageMode,
    next_cron_run,
    normalize_cron_expression,
)
from backend.app.infrastructure.persistence.models import (
    Downloader,
    Site,
    TaskDefinition,
    TaskExecution,
    TaskExecutionPolicy,
    TaskFilter,
    TaskOutputPolicy,
    TaskSchedule,
    TaskSource,
    new_uuid,
    utc_now,
)
from backend.app.infrastructure.source_inventory import scan_source_inventory


@dataclass(frozen=True, slots=True)
class TaskSourceCreate:
    kind: TaskSourceKind
    downloader_id: str | None = None
    directory_path: str | None = None
    config: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class TaskFilterCreate:
    file_types: tuple[str, ...] = ("VIDEO",)
    video_extensions: tuple[str, ...] = DEFAULT_VIDEO_EXTENSIONS
    archive_extensions: tuple[str, ...] = DEFAULT_ARCHIVE_EXTENSIONS
    min_size_bytes: int | None = None
    max_size_bytes: int | None = None
    include_name: str | None = None
    exclude_names: tuple[str, ...] = DEFAULT_EXCLUDE_NAMES
    ignore_temp_files: bool = True
    temp_patterns: tuple[str, ...] = DEFAULT_TEMP_PATTERNS
    include_subdirectories: bool = True
    max_scan_depth: int | None = None


@dataclass(frozen=True, slots=True)
class TaskOutputPolicyCreate:
    output_directory: str
    storage_mode: TaskStorageMode = TaskStorageMode.HARDLINK
    preserve_structure: bool = True
    conflict_policy: TaskConflictPolicy = TaskConflictPolicy.VERIFY_REUSE_OR_STOP


@dataclass(frozen=True, slots=True)
class TaskExecutionPolicyCreate:
    stability_detection_enabled: bool = True
    stability_wait_seconds: int = 60
    only_completed_downloads: bool = True
    initial_scope: TaskInitialScope = TaskInitialScope.NEW_ONLY
    debounce_seconds: int = 30
    overlap_policy: TaskOverlapPolicy = TaskOverlapPolicy.SKIP
    auto_retry_enabled: bool = True
    max_auto_retries: int = 3
    retry_intervals_seconds: tuple[int, ...] = DEFAULT_RETRY_INTERVALS_SECONDS
    high_risk_preauthorization_enabled: bool = False
    high_risk_allowed_action_kinds: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TaskDefinitionCreate:
    name: str
    kind: TaskDefinitionKind
    site_id: str
    source: TaskSourceCreate
    output_policy: TaskOutputPolicyCreate
    cron_expression: str | None = None
    filters: TaskFilterCreate = TaskFilterCreate()
    execution_policy: TaskExecutionPolicyCreate = TaskExecutionPolicyCreate()


@dataclass(frozen=True, slots=True)
class TaskDefinitionPrecheckItemView:
    code: str
    status: str
    title: str
    detail: str


@dataclass(frozen=True, slots=True)
class TaskDefinitionPrecheckView:
    status: str
    items: tuple[TaskDefinitionPrecheckItemView, ...]


@dataclass(frozen=True, slots=True)
class TaskExecutionSummaryView:
    id: str
    status: str
    phase: str
    trigger: str
    success_count: int
    failed_count: int
    skipped_count: int
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


@dataclass(frozen=True, slots=True)
class TaskDefinitionView:
    id: str
    name: str
    kind: str
    status: str
    site_id: str | None
    site_name: str | None
    site_available: bool
    source_kind: str
    source_downloader_id: str | None
    source_downloader_name: str | None
    source_directory: str | None
    source_available: bool
    source_config: dict[str, Any]
    cron_expression: str | None
    timezone: str | None
    last_scan_at: datetime | None
    last_successful_scan_at: datetime | None
    next_run_at: datetime | None
    file_types: tuple[str, ...]
    video_extensions: tuple[str, ...]
    archive_extensions: tuple[str, ...]
    min_size_bytes: int | None
    max_size_bytes: int | None
    include_name: str | None
    exclude_names: tuple[str, ...]
    ignore_temp_files: bool
    temp_patterns: tuple[str, ...]
    include_subdirectories: bool
    max_scan_depth: int | None
    output_directory: str
    storage_mode: str
    preserve_structure: bool
    conflict_policy: str
    stability_detection_enabled: bool
    stability_wait_seconds: int
    only_completed_downloads: bool
    initial_scope: str
    debounce_seconds: int
    overlap_policy: str
    auto_retry_enabled: bool
    max_auto_retries: int
    retry_intervals_seconds: tuple[int, ...]
    high_risk_preauthorization_enabled: bool
    high_risk_allowed_action_kinds: tuple[str, ...]
    latest_execution: TaskExecutionSummaryView | None
    version: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class TaskDirectoryEntryView:
    name: str
    path: str


@dataclass(frozen=True, slots=True)
class TaskDirectoryFileView:
    relative_path: str
    size_bytes: int
    device: int
    inode: int
    mtime_ns: int


@dataclass(frozen=True, slots=True)
class TaskDirectoryPreviewView:
    directory_path: str
    matched_count: int
    total_size_bytes: int
    files: tuple[TaskDirectoryFileView, ...]


class TaskDefinitionService:
    """v0.1.5 长期任务定义；实际副作用继续由既有 UnpackTask 安全链承担。"""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        data_root: Path,
        timezone: str,
    ) -> None:
        self._session_factory = session_factory
        self._data_root = data_root
        self._timezone = timezone

    def browse_directories(self, *, path: str = ".") -> tuple[TaskDirectoryEntryView, ...]:
        normalized, resolved = self._resolve_existing_directory(path)
        entries: list[TaskDirectoryEntryView] = []
        try:
            scandir_entries = tuple(os.scandir(resolved))
        except OSError as exc:
            raise self._invalid("无法读取所选目录") from exc
        for entry in sorted(scandir_entries, key=lambda item: item.name.casefold()):
            try:
                item_stat = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISLNK(item_stat.st_mode) or not stat.S_ISDIR(item_stat.st_mode):
                continue
            child = entry.name if normalized == "." else f"{normalized}/{entry.name}"
            entries.append(TaskDirectoryEntryView(name=entry.name, path=child))
        return tuple(entries)

    def preview_directory(
        self,
        *,
        directory_path: str,
        filters: TaskFilterCreate,
    ) -> TaskDirectoryPreviewView:
        normalized_filters = self._normalize_filters(filters)
        normalized, resolved = self._resolve_existing_directory(directory_path)
        try:
            inventory = scan_source_inventory(resolved)
        except Exception as exc:
            raise self._invalid("来源目录无法安全扫描") from exc
        matched = filter_source_inventory(normalized_filters, inventory)
        files = tuple(
            TaskDirectoryFileView(
                relative_path=item.relative_path,
                size_bytes=item.length,
                device=item.snapshot.device,
                inode=item.snapshot.inode,
                mtime_ns=item.snapshot.mtime_ns,
            )
            for item in matched
        )
        return TaskDirectoryPreviewView(
            directory_path=normalized,
            matched_count=len(files),
            total_size_bytes=sum(item.size_bytes for item in files),
            files=files,
        )

    def precheck_definition(self, request: TaskDefinitionCreate) -> TaskDefinitionPrecheckView:
        """保存前只读预检；真实副作用阶段仍由 execution gate/plan 再次校验。"""

        items: list[TaskDefinitionPrecheckItemView] = []

        def add(code: str, status: str, title: str, detail: str) -> None:
            items.append(TaskDefinitionPrecheckItemView(code, status, title, detail))

        name = request.name.strip()
        if not name:
            add("TASK_NAME", "BLOCKED", "任务名称", "任务名称不能为空")
        elif len(name) > 120:
            add("TASK_NAME", "BLOCKED", "任务名称", "任务名称不能超过 120 个字符")
        else:
            add("TASK_NAME", "OK", "任务名称", "任务名称有效")

        try:
            filters = self._normalize_filters(request.filters)
        except ApplicationError as exc:
            filters = None
            add("FILTERS", "BLOCKED", "过滤规则", exc.detail)
        else:
            add("FILTERS", "OK", "过滤规则", "过滤规则可由当前安全执行链处理")

        try:
            self._validate_output_policy(request.output_policy)
        except ApplicationError as exc:
            add("OUTPUT_POLICY", "BLOCKED", "输出策略", exc.detail)
        else:
            add("OUTPUT_POLICY", "OK", "输出策略", "当前输出策略已接入安全执行器")

        if request.kind is TaskDefinitionKind.MONITOR:
            if request.cron_expression is None or not request.cron_expression.strip():
                add("CRON", "BLOCKED", "执行时间", "监控任务必须配置 Cron")
            else:
                try:
                    normalized_cron = normalize_cron_expression(request.cron_expression)
                    next_run = next_cron_run(
                        normalized_cron,
                        utc_now(),
                        timezone=self._timezone,
                    )
                except ValueError as exc:
                    add("CRON", "BLOCKED", "执行时间", str(exc))
                else:
                    add("CRON", "OK", "执行时间", f"Cron 有效；下次执行 {next_run.isoformat()}")
        elif request.cron_expression is not None and request.cron_expression.strip():
            add("CRON", "BLOCKED", "执行时间", "手动任务不能配置 Cron")
        else:
            add("CRON", "OK", "执行时间", "手动任务无需 Cron")

        try:
            normalized_output, output_anchor, output_exists = self._resolve_output_anchor(
                request.output_policy.output_directory
            )
        except ApplicationError as exc:
            normalized_output = None
            output_anchor = None
            output_exists = False
            add("OUTPUT_PATH", "BLOCKED", "输出目录", exc.detail)
        else:
            assert output_anchor is not None
            add(
                "OUTPUT_PATH",
                "OK" if output_exists else "WARNING",
                "输出目录",
                (
                    f"输出目录 {normalized_output} 已存在且可安全访问"
                    if output_exists
                    else (
                        "输出目录尚不存在；将从安全父目录 "
                        f"{output_anchor.relative_to(self._data_root)} 创建"
                    )
                ),
            )

        source_path: Path | None = None
        downloader: Downloader | None = None
        source: TaskSourceCreate | None = None
        with self._session_factory() as session:
            site = session.get(Site, request.site_id.strip())
            if site is None:
                add("SITE", "BLOCKED", "扫描站点", "扫描站点不存在")
            elif not site.enabled or site.connection_status == "FAILED":
                add("SITE", "BLOCKED", "扫描站点", "扫描站点已停用或最近连接失败")
            elif site.connection_status != "OK":
                add("SITE", "WARNING", "扫描站点", "扫描站点尚未完成成功连接验证")
            else:
                add("SITE", "OK", "扫描站点", "扫描站点已启用且最近连接验证成功")

            try:
                source = self._normalize_source(session, request.source)
                if (
                    request.kind is TaskDefinitionKind.MANUAL
                    and source.kind is TaskSourceKind.DOWNLOADER
                ):
                    source = TaskSourceCreate(
                        kind=source.kind,
                        downloader_id=source.downloader_id,
                        config=self._normalize_manual_downloader_config(source.config or {}),
                    )
                if (
                    request.kind is TaskDefinitionKind.MANUAL
                    and source.kind is TaskSourceKind.DIRECTORY
                ):
                    selected_files = (source.config or {}).get("selected_files")
                    if not isinstance(selected_files, list) or not selected_files:
                        raise self._invalid("手动目录任务必须先扫描预览并至少选择一个文件")
            except ApplicationError as exc:
                add("SOURCE", "BLOCKED", "拆包来源", exc.detail)
            else:
                if source.kind is TaskSourceKind.DIRECTORY:
                    try:
                        _, source_path = self._resolve_existing_directory(
                            source.directory_path or "."
                        )
                    except ApplicationError as exc:
                        add("SOURCE", "BLOCKED", "拆包来源", exc.detail)
                    else:
                        add("SOURCE", "OK", "拆包来源", "来源目录存在、可读且未经过符号链接")
                    target_downloader_id = (source.config or {}).get("target_downloader_id")
                    if not isinstance(target_downloader_id, str) or not target_downloader_id:
                        add(
                            "TARGET_DOWNLOADER",
                            "BLOCKED",
                            "目标下载器",
                            "目录来源任务必须选择目标下载器",
                        )
                    else:
                        downloader = session.get(Downloader, target_downloader_id)
                else:
                    downloader = session.get(Downloader, source.downloader_id or "")
                    add("SOURCE", "OK", "拆包来源", "下载器来源配置有效")

            if downloader is not None:
                if not downloader.enabled or downloader.connection_status == "FAILED":
                    add("DOWNLOADER", "BLOCKED", "下载器", "下载器已停用或最近连接失败")
                elif downloader.connection_status != "OK":
                    add("DOWNLOADER", "WARNING", "下载器", "下载器尚未完成成功连接验证")
                elif downloader.path_mapping_status != "OK":
                    add("DOWNLOADER", "BLOCKED", "下载器", "下载器路径映射尚未验证通过")
                else:
                    add("DOWNLOADER", "OK", "下载器", "下载器连接和路径映射均已验证通过")

                if normalized_output is not None:
                    try:
                        mappings = [
                            PathMappingRule(
                                remote_prefix=item["remote_prefix"],
                                container_prefix=item["container_prefix"],
                            )
                            for item in downloader.path_mappings
                        ]
                        intended_output = (self._data_root / normalized_output).resolve(
                            strict=False
                        )
                        remote_output = reverse_map_container_path_unique(
                            intended_output,
                            mappings,
                            allowed_root=self._data_root,
                        )
                    except (DomainViolation, KeyError, TypeError, ValueError):
                        add(
                            "OUTPUT_DOWNLOADER_MAPPING",
                            "BLOCKED",
                            "输出目录映射",
                            "输出目录无法唯一映射到目标下载器，请选择已配置路径映射范围内的目录",
                        )
                    else:
                        add(
                            "OUTPUT_DOWNLOADER_MAPPING",
                            "OK",
                            "输出目录映射",
                            f"输出目录可安全映射到下载器路径 {remote_output}",
                        )

        if (
            output_anchor is not None
            and request.output_policy.storage_mode is TaskStorageMode.HARDLINK
        ):
            source_devices: set[int] = set()
            if source_path is not None:
                source_devices.add(source_path.stat().st_dev)
            elif (
                source is not None
                and source.kind is TaskSourceKind.DOWNLOADER
                and downloader is not None
            ):
                source_devices.update(self._selected_downloader_source_devices(source, downloader))
            target_device = output_anchor.stat().st_dev
            if not source_devices:
                add(
                    "HARDLINK_FILESYSTEM",
                    "WARNING",
                    "硬链接文件系统",
                    "保存前无法确定具体源文件设备；execution plan 会在副作用前再次强制校验",
                )
            elif source_devices == {target_device}:
                add(
                    "HARDLINK_FILESYSTEM",
                    "OK",
                    "硬链接文件系统",
                    f"来源与目标均位于设备 {target_device}",
                )
            else:
                add(
                    "HARDLINK_FILESYSTEM",
                    "BLOCKED",
                    "硬链接文件系统",
                    f"来源设备 {sorted(source_devices)} 与目标设备 {target_device} 不一致",
                )

        if filters is not None and request.kind is TaskDefinitionKind.MONITOR:
            try:
                self._validate_execution_policy(request.execution_policy, kind=request.kind)
            except ApplicationError as exc:
                add("EXECUTION_POLICY", "BLOCKED", "执行策略", exc.detail)
            else:
                add("EXECUTION_POLICY", "OK", "执行策略", "稳定检测、重叠与自动重试参数有效")

        overall = (
            "BLOCKED"
            if any(item.status == "BLOCKED" for item in items)
            else ("WARNING" if any(item.status == "WARNING" for item in items) else "OK")
        )
        return TaskDefinitionPrecheckView(status=overall, items=tuple(items))

    def list_definitions(
        self, *, kind: TaskDefinitionKind | None = None
    ) -> list[TaskDefinitionView]:
        with self._session_factory() as session:
            statement = select(TaskDefinition)
            if kind is not None:
                statement = statement.where(TaskDefinition.kind == kind.value)
            records = list(
                session.scalars(
                    statement.order_by(TaskDefinition.updated_at.desc(), TaskDefinition.id)
                )
            )
            return [self._view(session, record) for record in records]

    def get_definition(self, definition_id: str) -> TaskDefinitionView:
        with self._session_factory() as session:
            record = session.get(TaskDefinition, definition_id)
            if record is None:
                raise self._not_found()
            return self._view(session, record)

    def update_definition(
        self,
        definition_id: str,
        request: TaskDefinitionCreate,
    ) -> TaskDefinitionView:
        name = request.name.strip()
        if not name:
            raise self._invalid("任务名称不能为空，系统不会自动生成默认名称")
        if len(name) > 120:
            raise self._invalid("任务名称不能超过 120 个字符")
        self._validate_output_policy(request.output_policy)
        filters = self._normalize_filters(request.filters)
        output_directory = self._normalize_relative_path(
            request.output_policy.output_directory,
            field_name="输出目录",
            allow_root=False,
        )
        cron_expression: str | None = None
        if request.kind is TaskDefinitionKind.MONITOR:
            if request.cron_expression is None or not request.cron_expression.strip():
                raise self._invalid("监控拆包任务必须配置 Cron 执行时间")
            try:
                cron_expression = normalize_cron_expression(request.cron_expression)
            except ValueError as exc:
                raise self._invalid(str(exc)) from exc
        elif request.cron_expression is not None and request.cron_expression.strip():
            raise self._invalid("手动拆包任务不接受 Cron 调度")

        with self._session_factory() as session:
            definition = session.get(TaskDefinition, definition_id)
            if definition is None:
                raise self._not_found()
            if definition.kind != request.kind.value:
                raise self._invalid("编辑任务时不能修改任务类型；如需切换类型请克隆后重新创建")
            site = session.get(Site, request.site_id.strip())
            if site is None:
                raise self._invalid("扫描站点不存在，请从已配置站点中选择")
            source = self._normalize_source(session, request.source)
            if (
                request.kind is TaskDefinitionKind.MANUAL
                and source.kind is TaskSourceKind.DOWNLOADER
            ):
                source = TaskSourceCreate(
                    kind=source.kind,
                    downloader_id=source.downloader_id,
                    config=self._normalize_manual_downloader_config(source.config or {}),
                )
            elif (
                request.kind is TaskDefinitionKind.MANUAL
                and source.kind is TaskSourceKind.DIRECTORY
            ):
                selected_files = (source.config or {}).get("selected_files")
                if not isinstance(selected_files, list) or not selected_files:
                    raise self._invalid("手动目录任务必须先扫描预览并至少选择一个文件")

            source_record = session.scalar(
                select(TaskSource).where(TaskSource.task_definition_id == definition_id)
            )
            filter_record = session.scalar(
                select(TaskFilter).where(TaskFilter.task_definition_id == definition_id)
            )
            output_record = session.scalar(
                select(TaskOutputPolicy).where(TaskOutputPolicy.task_definition_id == definition_id)
            )
            policy_record = session.scalar(
                select(TaskExecutionPolicy).where(
                    TaskExecutionPolicy.task_definition_id == definition_id
                )
            )
            if any(
                item is None
                for item in (source_record, filter_record, output_record, policy_record)
            ):
                raise RuntimeError("task definition child records are incomplete")
            assert source_record is not None
            assert filter_record is not None
            assert output_record is not None
            assert policy_record is not None

            previous_source = (
                source_record.kind,
                source_record.downloader_id,
                source_record.directory_path,
                dict(source_record.config or {}),
            )
            next_source = (
                source.kind.value,
                source.downloader_id,
                source.directory_path,
                dict(source.config or {}),
            )
            now = utc_now()
            was_paused = definition.status == TaskDefinitionStatus.PAUSED.value
            definition.name = name
            definition.site_id = site.id
            definition.status = (
                TaskDefinitionStatus.PAUSED.value
                if was_paused
                else (
                    TaskDefinitionStatus.ENABLED.value
                    if site.enabled and site.connection_status != "FAILED"
                    else TaskDefinitionStatus.SITE_UNAVAILABLE.value
                )
            )
            definition.version += 1
            definition.updated_at = now

            source_record.kind = source.kind.value
            source_record.downloader_id = source.downloader_id
            source_record.directory_path = source.directory_path
            source_record.config = dict(source.config or {})
            source_record.updated_at = now

            filter_record.file_types = list(filters.file_types)
            filter_record.video_extensions = list(filters.video_extensions)
            filter_record.archive_extensions = list(filters.archive_extensions)
            filter_record.min_size_bytes = filters.min_size_bytes
            filter_record.max_size_bytes = filters.max_size_bytes
            filter_record.include_name = filters.include_name
            filter_record.exclude_names = list(filters.exclude_names)
            filter_record.ignore_temp_files = filters.ignore_temp_files
            filter_record.temp_patterns = list(filters.temp_patterns)
            filter_record.include_subdirectories = filters.include_subdirectories
            filter_record.max_scan_depth = filters.max_scan_depth
            filter_record.updated_at = now

            output_record.output_directory = output_directory
            output_record.storage_mode = request.output_policy.storage_mode.value
            output_record.preserve_structure = request.output_policy.preserve_structure
            output_record.conflict_policy = request.output_policy.conflict_policy.value
            output_record.updated_at = now

            policy = request.execution_policy
            self._validate_execution_policy(policy, kind=request.kind)
            policy_record.stability_detection_enabled = policy.stability_detection_enabled
            policy_record.stability_wait_seconds = policy.stability_wait_seconds
            policy_record.only_completed_downloads = policy.only_completed_downloads
            policy_record.initial_scope = policy.initial_scope.value
            policy_record.debounce_seconds = policy.debounce_seconds
            policy_record.overlap_policy = policy.overlap_policy.value
            policy_record.auto_retry_enabled = policy.auto_retry_enabled
            policy_record.max_auto_retries = policy.max_auto_retries
            policy_record.retry_intervals_seconds = list(policy.retry_intervals_seconds)
            policy_record.high_risk_preauthorization_enabled = (
                policy.high_risk_preauthorization_enabled
            )
            policy_record.high_risk_allowed_action_kinds = list(
                policy.high_risk_allowed_action_kinds
            )
            policy_record.updated_at = now

            schedule = session.scalar(
                select(TaskSchedule).where(TaskSchedule.task_definition_id == definition_id)
            )
            if request.kind is TaskDefinitionKind.MONITOR:
                assert cron_expression is not None
                if schedule is None:
                    schedule = TaskSchedule(
                        id=new_uuid(),
                        task_definition_id=definition_id,
                        cron_expression=cron_expression,
                        timezone=self._timezone,
                        enabled=definition.status == TaskDefinitionStatus.ENABLED.value,
                        last_scan_at=None,
                        last_successful_scan_at=None,
                        next_run_at=next_cron_run(cron_expression, now, timezone=self._timezone),
                        scan_checkpoint={},
                        created_at=now,
                        updated_at=now,
                    )
                    session.add(schedule)
                else:
                    source_changed = previous_source != next_source
                    schedule.cron_expression = cron_expression
                    schedule.enabled = definition.status == TaskDefinitionStatus.ENABLED.value
                    schedule.next_run_at = next_cron_run(
                        cron_expression,
                        now,
                        timezone=schedule.timezone,
                    )
                    if source_changed:
                        schedule.last_scan_at = None
                        schedule.last_successful_scan_at = None
                        schedule.scan_checkpoint = {}
                    schedule.updated_at = now
            session.commit()
            session.refresh(definition)
            return self._view(session, definition)

    def delete_definition(self, definition_id: str) -> None:
        with self._session_factory() as session:
            definition = session.get(TaskDefinition, definition_id)
            if definition is None:
                raise self._not_found()
            session.delete(definition)
            session.commit()

    def set_monitor_paused(self, definition_id: str, *, paused: bool) -> TaskDefinitionView:
        with self._session_factory() as session:
            definition = session.get(TaskDefinition, definition_id)
            if definition is None:
                raise self._not_found()
            if definition.kind != TaskDefinitionKind.MONITOR.value:
                raise ApplicationError(
                    code="TASK_DEFINITION_ACTION_INVALID",
                    status=409,
                    title="任务操作不适用",
                    detail="暂停与恢复只适用于监控拆包任务",
                )
            schedule = session.scalar(
                select(TaskSchedule).where(TaskSchedule.task_definition_id == definition_id)
            )
            if schedule is None:
                raise ApplicationError(
                    code="TASK_DEFINITION_CORRUPT",
                    status=500,
                    title="任务定义数据不完整",
                    detail="监控任务缺少调度记录",
                )
            now = utc_now()
            if paused:
                definition.status = TaskDefinitionStatus.PAUSED.value
                schedule.enabled = False
            else:
                site = (
                    session.get(Site, definition.site_id)
                    if definition.site_id is not None
                    else None
                )
                if site is None or not site.enabled or site.connection_status == "FAILED":
                    raise ApplicationError(
                        code="TASK_DEFINITION_SITE_UNAVAILABLE",
                        status=409,
                        title="扫描站点不可用",
                        detail="恢复监控任务前需要先恢复任务绑定站点",
                    )
                definition.status = TaskDefinitionStatus.ENABLED.value
                schedule.enabled = True
                schedule.next_run_at = next_cron_run(
                    schedule.cron_expression,
                    now,
                    timezone=schedule.timezone,
                )
            definition.version += 1
            definition.updated_at = now
            schedule.updated_at = now
            session.flush()
            view = self._view(session, definition)
            session.commit()
            return view

    def create_definition(self, request: TaskDefinitionCreate) -> TaskDefinitionView:
        name = request.name.strip()
        if not name:
            raise self._invalid("任务名称不能为空，系统不会自动生成默认名称")
        if len(name) > 120:
            raise self._invalid("任务名称不能超过 120 个字符")
        cron_expression: str | None = None
        if request.kind is TaskDefinitionKind.MONITOR:
            if request.cron_expression is None or not request.cron_expression.strip():
                raise self._invalid("监控拆包任务必须配置 Cron 执行时间")
            try:
                cron_expression = normalize_cron_expression(request.cron_expression)
            except ValueError as exc:
                raise self._invalid(str(exc)) from exc
        elif request.cron_expression is not None and request.cron_expression.strip():
            raise self._invalid("手动拆包任务不接受 Cron 调度")

        filters = self._normalize_filters(request.filters)
        self._validate_output_policy(request.output_policy)
        output_directory = self._normalize_relative_path(
            request.output_policy.output_directory,
            field_name="输出目录",
            allow_root=False,
        )

        with self._session_factory() as session:
            site = session.get(Site, request.site_id.strip())
            if site is None:
                raise self._invalid("扫描站点不存在，请从已配置站点中选择")
            source = self._normalize_source(session, request.source)
            if (
                request.kind is TaskDefinitionKind.MANUAL
                and source.kind is TaskSourceKind.DOWNLOADER
            ):
                source = TaskSourceCreate(
                    kind=source.kind,
                    downloader_id=source.downloader_id,
                    config=self._normalize_manual_downloader_config(source.config or {}),
                )
            elif (
                request.kind is TaskDefinitionKind.MANUAL
                and source.kind is TaskSourceKind.DIRECTORY
            ):
                selected_files = (source.config or {}).get("selected_files")
                if not isinstance(selected_files, list) or not selected_files:
                    raise self._invalid("手动目录任务必须先扫描预览并至少选择一个文件")
            now = utc_now()
            definition_id = new_uuid()
            status = (
                TaskDefinitionStatus.ENABLED
                if site.enabled and site.connection_status != "FAILED"
                else TaskDefinitionStatus.SITE_UNAVAILABLE
            )
            definition = TaskDefinition(
                id=definition_id,
                name=name,
                kind=request.kind.value,
                status=status.value,
                site_id=site.id,
                version=1,
                created_at=now,
                updated_at=now,
            )
            session.add(definition)
            session.add(
                TaskSource(
                    id=new_uuid(),
                    task_definition_id=definition_id,
                    kind=source.kind.value,
                    downloader_id=source.downloader_id,
                    directory_path=source.directory_path,
                    config=dict(source.config or {}),
                    created_at=now,
                    updated_at=now,
                )
            )
            if cron_expression is not None:
                session.add(
                    TaskSchedule(
                        id=new_uuid(),
                        task_definition_id=definition_id,
                        cron_expression=cron_expression,
                        timezone=self._timezone,
                        enabled=status is TaskDefinitionStatus.ENABLED,
                        last_scan_at=None,
                        last_successful_scan_at=None,
                        next_run_at=next_cron_run(
                            cron_expression,
                            now,
                            timezone=self._timezone,
                        ),
                        scan_checkpoint={},
                        created_at=now,
                        updated_at=now,
                    )
                )
            session.add(
                TaskFilter(
                    id=new_uuid(),
                    task_definition_id=definition_id,
                    file_types=list(filters.file_types),
                    video_extensions=list(filters.video_extensions),
                    archive_extensions=list(filters.archive_extensions),
                    min_size_bytes=filters.min_size_bytes,
                    max_size_bytes=filters.max_size_bytes,
                    include_name=filters.include_name,
                    exclude_names=list(filters.exclude_names),
                    ignore_temp_files=filters.ignore_temp_files,
                    temp_patterns=list(filters.temp_patterns),
                    include_subdirectories=filters.include_subdirectories,
                    max_scan_depth=filters.max_scan_depth,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                TaskOutputPolicy(
                    id=new_uuid(),
                    task_definition_id=definition_id,
                    output_directory=output_directory,
                    storage_mode=request.output_policy.storage_mode.value,
                    preserve_structure=request.output_policy.preserve_structure,
                    conflict_policy=request.output_policy.conflict_policy.value,
                    created_at=now,
                    updated_at=now,
                )
            )
            policy = request.execution_policy
            self._validate_execution_policy(policy, kind=request.kind)
            session.add(
                TaskExecutionPolicy(
                    id=new_uuid(),
                    task_definition_id=definition_id,
                    stability_detection_enabled=policy.stability_detection_enabled,
                    stability_wait_seconds=policy.stability_wait_seconds,
                    only_completed_downloads=policy.only_completed_downloads,
                    initial_scope=policy.initial_scope.value,
                    debounce_seconds=policy.debounce_seconds,
                    overlap_policy=policy.overlap_policy.value,
                    auto_retry_enabled=policy.auto_retry_enabled,
                    max_auto_retries=policy.max_auto_retries,
                    retry_intervals_seconds=list(policy.retry_intervals_seconds),
                    high_risk_preauthorization_enabled=(policy.high_risk_preauthorization_enabled),
                    high_risk_allowed_action_kinds=list(policy.high_risk_allowed_action_kinds),
                    created_at=now,
                    updated_at=now,
                )
            )
            session.flush()
            view = self._view(session, definition)
            session.commit()
            return view

    def _resolve_existing_directory(self, value: str) -> tuple[str, Path]:
        normalized = self._normalize_relative_path(value, field_name="目录", allow_root=True)
        try:
            base = self._data_root.resolve(strict=True)
            base_stat = self._data_root.stat(follow_symlinks=False)
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
                raise self._invalid("所选目录不存在或不可读取") from exc
            if stat.S_ISLNK(item_stat.st_mode) or not stat.S_ISDIR(item_stat.st_mode):
                raise self._invalid("目录浏览不能经过符号链接或非目录路径")
        resolved = current.resolve(strict=True)
        if not resolved.is_relative_to(base):
            raise self._invalid("所选目录越过 PackBreaker 授权数据目录")
        return normalized, resolved

    def _resolve_output_anchor(self, value: str) -> tuple[str, Path, bool]:
        """Resolve a safe existing anchor for an output path without creating anything."""

        normalized = self._normalize_relative_path(value, field_name="输出目录", allow_root=False)
        try:
            base_stat = self._data_root.stat(follow_symlinks=False)
            base = self._data_root.resolve(strict=True)
        except OSError as exc:
            raise self._invalid("授权数据目录不可用") from exc
        if stat.S_ISLNK(base_stat.st_mode) or not stat.S_ISDIR(base_stat.st_mode):
            raise self._invalid("授权数据目录必须是真实目录且不能是符号链接")

        current = self._data_root
        parts = tuple(normalized.split("/"))
        for index, part in enumerate(parts):
            candidate = current / part
            try:
                item_stat = candidate.stat(follow_symlinks=False)
            except FileNotFoundError:
                # The remaining path may be created later by the journal-backed executor.
                return normalized, current.resolve(strict=True), False
            except OSError as exc:
                raise self._invalid("输出目录或其父目录不可访问") from exc
            if stat.S_ISLNK(item_stat.st_mode):
                raise self._invalid("输出目录不能经过符号链接")
            if not stat.S_ISDIR(item_stat.st_mode):
                detail = (
                    "输出目录的中间路径不是目录"
                    if index < len(parts) - 1
                    else "输出目录必须指向目录"
                )
                raise self._invalid(detail)
            current = candidate

        try:
            resolved = current.resolve(strict=True)
        except OSError as exc:
            raise self._invalid("输出目录不可访问") from exc
        if not resolved.is_relative_to(base):
            raise self._invalid("输出目录越过 PackBreaker 授权数据目录")
        return normalized, resolved, True

    def _selected_downloader_source_devices(
        self,
        source: TaskSourceCreate,
        downloader: Downloader,
    ) -> set[int]:
        snapshots = (source.config or {}).get("selected_torrents")
        if not isinstance(snapshots, list):
            return set()
        try:
            mappings = [
                PathMappingRule(
                    remote_prefix=item["remote_prefix"],
                    container_prefix=item["container_prefix"],
                )
                for item in downloader.path_mappings
            ]
        except (KeyError, TypeError, ValueError):
            return set()

        devices: set[int] = set()
        for snapshot in snapshots:
            if not isinstance(snapshot, dict):
                continue
            raw_path = snapshot.get("content_path") or snapshot.get("save_path")
            if not isinstance(raw_path, str) or not raw_path.strip():
                continue
            try:
                mapped = map_remote_path(
                    raw_path, mappings, allowed_root=self._data_root
                ).container_path
                item_stat = mapped.stat(follow_symlinks=False)
            except (DomainViolation, OSError):
                continue
            if stat.S_ISLNK(item_stat.st_mode):
                continue
            devices.add(item_stat.st_dev)
        return devices

    def _normalize_source(self, session: Session, request: TaskSourceCreate) -> TaskSourceCreate:
        if request.kind is TaskSourceKind.DOWNLOADER:
            downloader_id = (request.downloader_id or "").strip()
            if not downloader_id or request.directory_path is not None:
                raise self._invalid("下载器来源必须且只能选择一个已配置下载器")
            if session.get(Downloader, downloader_id) is None:
                raise self._invalid("来源下载器不存在，请从已配置下载器中选择")
            config = dict(request.config or {})
            config["target_downloader_id"] = downloader_id
            return TaskSourceCreate(
                kind=request.kind,
                downloader_id=downloader_id,
                config=config,
            )
        if request.downloader_id is not None:
            raise self._invalid("目录来源不能同时指定下载器")
        directory_path = self._normalize_relative_path(
            request.directory_path or "", field_name="来源目录", allow_root=True
        )
        resolved = (self._data_root / directory_path).resolve(strict=False)
        try:
            resolved.relative_to(self._data_root.resolve())
        except ValueError as exc:
            raise self._invalid("来源目录必须位于 PackBreaker 授权数据目录内") from exc
        config = dict(request.config or {})
        target_downloader_id = config.get("target_downloader_id")
        if target_downloader_id is not None:
            if not isinstance(target_downloader_id, str) or not target_downloader_id.strip():
                raise self._invalid("目标下载器 ID 格式无效")
            target_downloader_id = target_downloader_id.strip()
            if session.get(Downloader, target_downloader_id) is None:
                raise self._invalid("目标下载器不存在，请从已配置下载器中选择")
            config["target_downloader_id"] = target_downloader_id
        if config.get("selected_files") is not None:
            raw_selected = config.get("selected_files")
            if not isinstance(raw_selected, list) or not raw_selected:
                raise self._invalid("手动目录任务的已选文件快照不能为空")
            normalized_selected: list[dict[str, int | str]] = []
            seen_paths: set[str] = set()
            for item in raw_selected:
                if not isinstance(item, dict):
                    raise self._invalid("手动目录任务的已选文件快照格式无效")
                relative_path = item.get("relative_path")
                if not isinstance(relative_path, str) or not relative_path.strip():
                    raise self._invalid("手动目录任务的文件相对路径无效")
                normalized_relative = self._normalize_relative_path(
                    relative_path, field_name="文件相对路径", allow_root=False
                )
                snapshot_values: dict[str, int] = {}
                for key in ("size_bytes", "device", "inode", "mtime_ns"):
                    value = item.get(key)
                    if not isinstance(value, int) or value < 0:
                        raise self._invalid("手动目录任务的文件快照字段无效")
                    snapshot_values[key] = value
                if normalized_relative in seen_paths:
                    continue
                seen_paths.add(normalized_relative)
                normalized_selected.append(
                    {"relative_path": normalized_relative, **snapshot_values}
                )
            config["selected_files"] = normalized_selected
        return TaskSourceCreate(
            kind=request.kind,
            directory_path=directory_path,
            config=config,
        )

    def _normalize_filters(self, filters: TaskFilterCreate) -> TaskFilterCreate:
        file_types = tuple(dict.fromkeys(value.strip().upper() for value in filters.file_types))
        if file_types != ("VIDEO",):
            raise self._invalid(
                "v0.1.5 当前安全执行链仅支持视频文件；"
                "压缩包、ISO 和其他类型尚未接入 TaskUnit 执行器"
            )
        if filters.min_size_bytes is not None and filters.min_size_bytes < 0:
            raise self._invalid("最小文件大小不能小于 0")
        if filters.max_size_bytes is not None and filters.max_size_bytes < 0:
            raise self._invalid("最大文件大小不能小于 0")
        if (
            filters.min_size_bytes is not None
            and filters.max_size_bytes is not None
            and filters.min_size_bytes > filters.max_size_bytes
        ):
            raise self._invalid("最小文件大小不能大于最大文件大小")
        if filters.max_scan_depth is not None and filters.max_scan_depth < 0:
            raise self._invalid("最大扫描深度不能小于 0")
        return TaskFilterCreate(
            file_types=file_types,
            video_extensions=self._normalize_extensions(filters.video_extensions),
            archive_extensions=self._normalize_extensions(filters.archive_extensions),
            min_size_bytes=filters.min_size_bytes,
            max_size_bytes=filters.max_size_bytes,
            include_name=(filters.include_name or "").strip() or None,
            exclude_names=tuple(
                dict.fromkeys(value.strip() for value in filters.exclude_names if value.strip())
            ),
            ignore_temp_files=filters.ignore_temp_files,
            temp_patterns=tuple(
                dict.fromkeys(value.strip() for value in filters.temp_patterns if value.strip())
            ),
            include_subdirectories=filters.include_subdirectories,
            max_scan_depth=filters.max_scan_depth,
        )

    def _normalize_extensions(self, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized: list[str] = []
        for value in values:
            item = value.strip().lower()
            if not item:
                continue
            if not item.startswith("."):
                item = f".{item}"
            if "/" in item or "\\" in item or "\x00" in item:
                raise self._invalid("文件扩展名格式无效")
            normalized.append(item)
        return tuple(dict.fromkeys(normalized))

    def _normalize_manual_downloader_config(self, config: dict[str, Any]) -> dict[str, Any]:
        raw_hashes = config.get("selected_torrent_hashes")
        if not isinstance(raw_hashes, list) or not raw_hashes:
            raise self._invalid("手动下载器任务至少选择一个真实种子")
        hashes: list[str] = []
        for value in raw_hashes:
            if not isinstance(value, str):
                raise self._invalid("已选择种子 hash 格式无效")
            normalized = value.strip().lower()
            if len(normalized) not in {40, 64} or any(
                char not in "0123456789abcdef" for char in normalized
            ):
                raise self._invalid("已选择种子 hash 格式无效")
            hashes.append(normalized)
        result = dict(config)
        result["selected_torrent_hashes"] = list(dict.fromkeys(hashes))
        snapshots = result.get("selected_torrents")
        if snapshots is not None and not isinstance(snapshots, list):
            raise self._invalid("已选择种子快照格式无效")
        return result

    def _validate_execution_policy(
        self,
        policy: TaskExecutionPolicyCreate,
        *,
        kind: TaskDefinitionKind,
    ) -> None:
        if policy.stability_wait_seconds < 0 or policy.stability_wait_seconds > 86400:
            raise self._invalid("文件稳定等待时间必须位于 0..86400 秒")
        if policy.debounce_seconds < 0 or policy.debounce_seconds > 86400:
            raise self._invalid("事件冷却时间必须位于 0..86400 秒")
        if policy.max_auto_retries < 0 or policy.max_auto_retries > 20:
            raise self._invalid("最大自动重试次数必须位于 0..20")
        if len(policy.retry_intervals_seconds) < policy.max_auto_retries:
            raise self._invalid("自动重试间隔数量不能少于最大自动重试次数")
        if any(value <= 0 or value > 86400 for value in policy.retry_intervals_seconds):
            raise self._invalid("自动重试间隔必须位于 1..86400 秒")
        allowed_kinds = tuple(dict.fromkeys(policy.high_risk_allowed_action_kinds))
        if kind is not TaskDefinitionKind.MONITOR and (
            policy.high_risk_preauthorization_enabled or allowed_kinds
        ):
            raise self._invalid("高风险预授权只允许配置在监控拆包任务")
        if policy.high_risk_preauthorization_enabled and not allowed_kinds:
            raise self._invalid("开启高风险预授权时必须至少选择一个允许的 action kind")
        if not policy.high_risk_preauthorization_enabled and allowed_kinds:
            raise self._invalid("未开启高风险预授权时不能保留 action kind 白名单")
        for value in allowed_kinds:
            if (
                not value
                or len(value) > 64
                or value != value.upper()
                or not value.replace("_", "").isalnum()
            ):
                raise self._invalid("高风险预授权 action kind 必须使用大写字母、数字或下划线")

    def _validate_output_policy(self, policy: TaskOutputPolicyCreate) -> None:
        if policy.storage_mode is not TaskStorageMode.HARDLINK:
            raise self._invalid("v0.1.5 当前仅 HARDLINK 已接入 journal-backed 安全执行器")
        if not policy.preserve_structure:
            raise self._invalid("v0.1.5 当前安全执行计划固定保持 torrent 原目录结构")
        if policy.conflict_policy is not TaskConflictPolicy.VERIFY_REUSE_OR_STOP:
            raise self._invalid("v0.1.5 当前仅支持“校验一致后复用，否则停止”的安全冲突策略")

    @staticmethod
    def _normalize_relative_path(value: str, *, field_name: str, allow_root: bool) -> str:
        normalized = value.strip().replace("\\", "/")
        if not normalized:
            if allow_root:
                return "."
            raise TaskDefinitionService._invalid(f"{field_name}不能为空")
        path = PurePosixPath(normalized)
        if path.is_absolute() or ".." in path.parts or "\x00" in normalized:
            raise TaskDefinitionService._invalid(f"{field_name}必须使用授权数据目录内的相对路径")
        result = path.as_posix()
        if not allow_root and result in {"", "."}:
            raise TaskDefinitionService._invalid(f"{field_name}不能直接使用数据根目录")
        return result

    def _view(self, session: Session, record: TaskDefinition) -> TaskDefinitionView:
        source = session.scalar(
            select(TaskSource).where(TaskSource.task_definition_id == record.id)
        )
        filters = session.scalar(
            select(TaskFilter).where(TaskFilter.task_definition_id == record.id)
        )
        output = session.scalar(
            select(TaskOutputPolicy).where(TaskOutputPolicy.task_definition_id == record.id)
        )
        policy = session.scalar(
            select(TaskExecutionPolicy).where(TaskExecutionPolicy.task_definition_id == record.id)
        )
        schedule = session.scalar(
            select(TaskSchedule).where(TaskSchedule.task_definition_id == record.id)
        )
        if source is None or filters is None or output is None or policy is None:
            raise ApplicationError(
                code="TASK_DEFINITION_CORRUPT",
                status=500,
                title="任务定义数据不完整",
                detail="任务定义缺少来源、过滤、输出或执行策略记录",
            )
        site = session.get(Site, record.site_id) if record.site_id is not None else None
        downloader = (
            session.get(Downloader, source.downloader_id)
            if source.downloader_id is not None
            else None
        )
        latest = session.scalar(
            select(TaskExecution)
            .where(TaskExecution.task_definition_id == record.id)
            .order_by(TaskExecution.created_at.desc(), TaskExecution.id.desc())
            .limit(1)
        )
        if latest is not None:
            reconcile_task_execution(session, latest)
            session.flush()
        site_available = bool(
            site is not None and site.enabled and site.connection_status != "FAILED"
        )
        source_available = source.kind == TaskSourceKind.DIRECTORY.value or downloader is not None
        status = record.status
        if not site_available:
            status = TaskDefinitionStatus.SITE_UNAVAILABLE.value
        return TaskDefinitionView(
            id=record.id,
            name=record.name,
            kind=record.kind,
            status=status,
            site_id=record.site_id,
            site_name=site.name if site is not None else None,
            site_available=site_available,
            source_kind=source.kind,
            source_downloader_id=source.downloader_id,
            source_downloader_name=downloader.name if downloader is not None else None,
            source_directory=source.directory_path,
            source_available=source_available,
            source_config=dict(source.config),
            cron_expression=schedule.cron_expression if schedule is not None else None,
            timezone=schedule.timezone if schedule is not None else None,
            last_scan_at=schedule.last_scan_at if schedule is not None else None,
            last_successful_scan_at=(
                schedule.last_successful_scan_at if schedule is not None else None
            ),
            next_run_at=schedule.next_run_at if schedule is not None else None,
            file_types=tuple(filters.file_types),
            video_extensions=tuple(filters.video_extensions),
            archive_extensions=tuple(filters.archive_extensions),
            min_size_bytes=filters.min_size_bytes,
            max_size_bytes=filters.max_size_bytes,
            include_name=filters.include_name,
            exclude_names=tuple(filters.exclude_names),
            ignore_temp_files=filters.ignore_temp_files,
            temp_patterns=tuple(filters.temp_patterns),
            include_subdirectories=filters.include_subdirectories,
            max_scan_depth=filters.max_scan_depth,
            output_directory=output.output_directory,
            storage_mode=output.storage_mode,
            preserve_structure=output.preserve_structure,
            conflict_policy=output.conflict_policy,
            stability_detection_enabled=policy.stability_detection_enabled,
            stability_wait_seconds=policy.stability_wait_seconds,
            only_completed_downloads=policy.only_completed_downloads,
            initial_scope=policy.initial_scope,
            debounce_seconds=policy.debounce_seconds,
            overlap_policy=policy.overlap_policy,
            auto_retry_enabled=policy.auto_retry_enabled,
            max_auto_retries=policy.max_auto_retries,
            retry_intervals_seconds=tuple(policy.retry_intervals_seconds),
            high_risk_preauthorization_enabled=policy.high_risk_preauthorization_enabled,
            high_risk_allowed_action_kinds=tuple(policy.high_risk_allowed_action_kinds),
            latest_execution=(
                TaskExecutionSummaryView(
                    id=latest.id,
                    status=latest.status,
                    phase=latest.phase,
                    trigger=latest.trigger,
                    success_count=latest.success_count,
                    failed_count=latest.failed_count,
                    skipped_count=latest.skipped_count,
                    created_at=latest.created_at,
                    started_at=latest.started_at,
                    finished_at=latest.finished_at,
                )
                if latest is not None
                else None
            ),
            version=record.version,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )

    @staticmethod
    def _invalid(detail: str) -> ApplicationError:
        return ApplicationError(
            code="TASK_DEFINITION_INVALID",
            status=422,
            title="任务定义无效",
            detail=detail,
        )

    @staticmethod
    def _not_found() -> ApplicationError:
        return ApplicationError(
            code="TASK_DEFINITION_NOT_FOUND",
            status=404,
            title="任务定义不存在",
            detail="未找到指定的 v0.1.5 任务定义",
        )
