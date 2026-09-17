from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field

from backend.app.api.dependencies import (
    AccessPrincipal,
    require_admin_csrf_principal,
    require_admin_principal,
    task_definition_execution_service,
    task_definition_service,
)
from backend.app.application.errors import ApplicationError
from backend.app.application.task_actions import TaskActionActor
from backend.app.application.task_definition_executions import (
    TaskExecutionPageView,
    TaskExecutionView,
    TaskMonitorScanView,
)
from backend.app.application.task_definitions import (
    TaskDefinitionCreate,
    TaskDefinitionPrecheckView,
    TaskDefinitionView,
    TaskExecutionPolicyCreate,
    TaskFilterCreate,
    TaskOutputPolicyCreate,
    TaskSourceCreate,
)
from backend.app.domain.task_definition import (
    DEFAULT_ARCHIVE_EXTENSIONS,
    DEFAULT_EXCLUDE_NAMES,
    DEFAULT_RETRY_INTERVALS_SECONDS,
    DEFAULT_TEMP_PATTERNS,
    DEFAULT_VIDEO_EXTENSIONS,
    TaskConflictPolicy,
    TaskDefinitionKind,
    TaskExecutionTrigger,
    TaskInitialScope,
    TaskOverlapPolicy,
    TaskSourceKind,
    TaskStorageMode,
    next_cron_run,
    normalize_cron_expression,
)

router = APIRouter(tags=["task-definitions"])
TASKS_READ_ACCESS = require_admin_principal
TASKS_WRITE_ACCESS = require_admin_csrf_principal


def _describe_cron(value: str) -> str:
    minute, hour, day, month, weekday = value.split()
    if minute.startswith("*/") and hour == day == month == weekday == "*":
        return f"每 {minute[2:]} 分钟执行一次"
    if minute.isdigit() and hour.startswith("*/") and day == month == weekday == "*":
        return f"每 {hour[2:]} 小时执行一次（第 {int(minute):02d} 分钟）"
    if minute.isdigit() and hour.isdigit() and day == month == weekday == "*":
        return f"每天 {int(hour):02d}:{int(minute):02d} 执行"
    if minute.isdigit() and hour.isdigit() and day == month == "*" and weekday.isdigit():
        labels = {
            "0": "周日",
            "1": "周一",
            "2": "周二",
            "3": "周三",
            "4": "周四",
            "5": "周五",
            "6": "周六",
            "7": "周日",
        }
        return f"每{labels.get(weekday, f'周{weekday}')} {int(hour):02d}:{int(minute):02d} 执行"
    if minute.isdigit() and hour.isdigit() and day.isdigit() and month == weekday == "*":
        return f"每月 {int(day)} 日 {int(hour):02d}:{int(minute):02d} 执行"
    return f"自定义 Cron：{value}"


class TaskSourceInput(BaseModel):
    kind: TaskSourceKind
    downloader_id: str | None = Field(default=None, max_length=36)
    directory_path: str | None = Field(default=None, max_length=4096)
    config: dict[str, Any] = Field(default_factory=dict)


class TaskFilterInput(BaseModel):
    file_types: list[str] = Field(default_factory=lambda: ["VIDEO"], min_length=1, max_length=4)
    video_extensions: list[str] = Field(
        default_factory=lambda: list(DEFAULT_VIDEO_EXTENSIONS), max_length=64
    )
    archive_extensions: list[str] = Field(
        default_factory=lambda: list(DEFAULT_ARCHIVE_EXTENSIONS), max_length=64
    )
    min_size_bytes: int | None = Field(default=None, ge=0)
    max_size_bytes: int | None = Field(default=None, ge=0)
    include_name: str | None = Field(default=None, max_length=512)
    exclude_names: list[str] = Field(
        default_factory=lambda: list(DEFAULT_EXCLUDE_NAMES), max_length=128
    )
    ignore_temp_files: bool = True
    temp_patterns: list[str] = Field(
        default_factory=lambda: list(DEFAULT_TEMP_PATTERNS), max_length=128
    )
    include_subdirectories: bool = True
    max_scan_depth: int | None = Field(default=None, ge=0, le=1000)


class TaskOutputPolicyInput(BaseModel):
    output_directory: str = Field(min_length=1, max_length=4096)
    storage_mode: TaskStorageMode = TaskStorageMode.HARDLINK
    preserve_structure: bool = True
    conflict_policy: TaskConflictPolicy = TaskConflictPolicy.VERIFY_REUSE_OR_STOP


class TaskExecutionPolicyInput(BaseModel):
    stability_detection_enabled: bool = True
    stability_wait_seconds: int = Field(default=60, ge=0, le=86400)
    only_completed_downloads: bool = True
    initial_scope: TaskInitialScope = TaskInitialScope.NEW_ONLY
    debounce_seconds: int = Field(default=30, ge=0, le=86400)
    overlap_policy: TaskOverlapPolicy = TaskOverlapPolicy.SKIP
    auto_retry_enabled: bool = True
    max_auto_retries: int = Field(default=3, ge=0, le=20)
    retry_intervals_seconds: list[int] = Field(
        default_factory=lambda: list(DEFAULT_RETRY_INTERVALS_SECONDS), max_length=20
    )


class TaskDirectoryEntryResponse(BaseModel):
    name: str
    path: str


class TaskDirectoryBrowseResponse(BaseModel):
    current_path: str
    entries: list[TaskDirectoryEntryResponse]


class TaskDirectoryPreviewRequest(BaseModel):
    directory_path: str = Field(min_length=1, max_length=4096)
    filters: TaskFilterInput = Field(default_factory=TaskFilterInput)


class TaskDirectoryFileResponse(BaseModel):
    relative_path: str
    size_bytes: int
    device: int
    inode: int
    mtime_ns: int


class TaskDirectoryPreviewResponse(BaseModel):
    directory_path: str
    matched_count: int
    total_size_bytes: int
    files: list[TaskDirectoryFileResponse]


class TaskCronPreviewRequest(BaseModel):
    cron_expression: str = Field(min_length=1, max_length=160)


class TaskCronPreviewResponse(BaseModel):
    cron_expression: str
    timezone: str
    description: str
    next_runs: list[datetime]


class TaskDefinitionCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    kind: TaskDefinitionKind
    site_id: str = Field(min_length=1, max_length=36)
    source: TaskSourceInput
    cron_expression: str | None = Field(default=None, max_length=160)
    filters: TaskFilterInput = Field(default_factory=TaskFilterInput)
    output_policy: TaskOutputPolicyInput
    execution_policy: TaskExecutionPolicyInput = Field(default_factory=TaskExecutionPolicyInput)


class TaskExecutionSummaryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

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


class TaskDefinitionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

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
    file_types: list[str]
    video_extensions: list[str]
    archive_extensions: list[str]
    min_size_bytes: int | None
    max_size_bytes: int | None
    include_name: str | None
    exclude_names: list[str]
    ignore_temp_files: bool
    temp_patterns: list[str]
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
    retry_intervals_seconds: list[int]
    latest_execution: TaskExecutionSummaryResponse | None
    version: int
    created_at: datetime
    updated_at: datetime


class TaskDefinitionListResponse(BaseModel):
    items: list[TaskDefinitionResponse]


class TaskDefinitionPrecheckItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    code: str
    status: str
    title: str
    detail: str


class TaskDefinitionPrecheckResponse(BaseModel):
    status: str
    items: list[TaskDefinitionPrecheckItemResponse]


class TaskDefinitionActionRequest(BaseModel):
    action: Literal["pause", "resume"]


class TaskExecutionItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    unpack_task_id: str | None
    source_object_key: str
    name: str
    source: str
    size_bytes: int | None
    phase: str
    progress: int | None
    result: str | None
    error_code: str | None
    error_summary_zh: str | None
    technical_detail: str | None
    retryable: bool
    retry_count: int


class TaskExecutionPlanResponse(BaseModel):
    id: str
    ready: bool
    current: bool
    target_root: str
    target_downloader_id: str | None
    verification_level: str
    hardlink_count: int
    client_fetch_count: int
    create_directory_count: int
    estimated_download_bytes_upper_bound: int
    blocked_reasons: list[str]
    execution_allowed: bool
    side_effects_started: bool


class TaskExecutionEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    event_code: str
    message: str
    trace_id: str
    context: dict[str, Any]
    created_at: datetime


class TaskExecutionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    task_definition_id: str | None
    task_name: str
    trigger: str
    status: str
    phase: str
    source_execution_id: str | None
    discovered_count: int
    success_count: int
    failed_count: int
    skipped_count: int
    trace_id: str
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    config_snapshot: dict[str, Any]
    items: list[TaskExecutionItemResponse]
    events: list[TaskExecutionEventResponse]


class TaskExecutionListItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    task_definition_id: str | None
    task_name: str
    trigger: str
    status: str
    phase: str
    source_execution_id: str | None
    discovered_count: int
    success_count: int
    failed_count: int
    skipped_count: int
    trace_id: str
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime


class TaskExecutionListResponse(BaseModel):
    items: list[TaskExecutionListItemResponse]
    page: int
    page_size: int
    total: int


class TaskMonitorScanResponse(BaseModel):
    task_definition_id: str
    trigger: str
    outcome: str
    discovered_count: int
    new_count: int
    next_run_at: datetime | None
    execution: TaskExecutionResponse | None


def _create_request(payload: TaskDefinitionCreateRequest) -> TaskDefinitionCreate:
    return TaskDefinitionCreate(
        name=payload.name,
        kind=payload.kind,
        site_id=payload.site_id,
        source=TaskSourceCreate(
            kind=payload.source.kind,
            downloader_id=payload.source.downloader_id,
            directory_path=payload.source.directory_path,
            config=payload.source.config,
        ),
        cron_expression=payload.cron_expression,
        filters=TaskFilterCreate(
            file_types=tuple(payload.filters.file_types),
            video_extensions=tuple(payload.filters.video_extensions),
            archive_extensions=tuple(payload.filters.archive_extensions),
            min_size_bytes=payload.filters.min_size_bytes,
            max_size_bytes=payload.filters.max_size_bytes,
            include_name=payload.filters.include_name,
            exclude_names=tuple(payload.filters.exclude_names),
            ignore_temp_files=payload.filters.ignore_temp_files,
            temp_patterns=tuple(payload.filters.temp_patterns),
            include_subdirectories=payload.filters.include_subdirectories,
            max_scan_depth=payload.filters.max_scan_depth,
        ),
        output_policy=TaskOutputPolicyCreate(
            output_directory=payload.output_policy.output_directory,
            storage_mode=payload.output_policy.storage_mode,
            preserve_structure=payload.output_policy.preserve_structure,
            conflict_policy=payload.output_policy.conflict_policy,
        ),
        execution_policy=TaskExecutionPolicyCreate(
            stability_detection_enabled=payload.execution_policy.stability_detection_enabled,
            stability_wait_seconds=payload.execution_policy.stability_wait_seconds,
            only_completed_downloads=payload.execution_policy.only_completed_downloads,
            initial_scope=payload.execution_policy.initial_scope,
            debounce_seconds=payload.execution_policy.debounce_seconds,
            overlap_policy=payload.execution_policy.overlap_policy,
            auto_retry_enabled=payload.execution_policy.auto_retry_enabled,
            max_auto_retries=payload.execution_policy.max_auto_retries,
            retry_intervals_seconds=tuple(payload.execution_policy.retry_intervals_seconds),
        ),
    )


def _response(view: TaskDefinitionView) -> TaskDefinitionResponse:
    return TaskDefinitionResponse.model_validate(view)


def _precheck_response(view: TaskDefinitionPrecheckView) -> TaskDefinitionPrecheckResponse:
    return TaskDefinitionPrecheckResponse(
        status=view.status,
        items=[TaskDefinitionPrecheckItemResponse.model_validate(item) for item in view.items],
    )


def _execution_response(view: TaskExecutionView) -> TaskExecutionResponse:
    return TaskExecutionResponse.model_validate(view)


def _execution_list_response(view: TaskExecutionPageView) -> TaskExecutionListResponse:
    return TaskExecutionListResponse(
        items=[TaskExecutionListItemResponse.model_validate(item) for item in view.items],
        page=view.page,
        page_size=view.page_size,
        total=view.total,
    )


def _monitor_scan_response(view: TaskMonitorScanView) -> TaskMonitorScanResponse:
    return TaskMonitorScanResponse(
        task_definition_id=view.task_definition_id,
        trigger=view.trigger,
        outcome=view.outcome,
        discovered_count=view.discovered_count,
        new_count=view.new_count,
        next_run_at=view.next_run_at,
        execution=_execution_response(view.execution) if view.execution is not None else None,
    )


@router.get("/task-definitions", response_model=TaskDefinitionListResponse)
async def list_task_definitions(
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(TASKS_READ_ACCESS)],
    kind: TaskDefinitionKind | None = None,
) -> TaskDefinitionListResponse:
    items = task_definition_service(request).list_definitions(kind=kind)
    return TaskDefinitionListResponse(items=[_response(item) for item in items])


@router.get("/task-definitions/source-directories", response_model=TaskDirectoryBrowseResponse)
async def browse_task_source_directories(
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(TASKS_READ_ACCESS)],
    path: Annotated[str, Query(max_length=4096)] = ".",
) -> TaskDirectoryBrowseResponse:
    entries = task_definition_service(request).browse_directories(path=path)
    return TaskDirectoryBrowseResponse(
        current_path=path,
        entries=[TaskDirectoryEntryResponse(name=item.name, path=item.path) for item in entries],
    )


@router.post("/task-definitions/directory-preview", response_model=TaskDirectoryPreviewResponse)
async def preview_task_source_directory(
    request: Request,
    payload: TaskDirectoryPreviewRequest,
    _principal: Annotated[AccessPrincipal, Depends(TASKS_WRITE_ACCESS)],
) -> TaskDirectoryPreviewResponse:
    view = task_definition_service(request).preview_directory(
        directory_path=payload.directory_path,
        filters=TaskFilterCreate(
            file_types=tuple(payload.filters.file_types),
            video_extensions=tuple(payload.filters.video_extensions),
            archive_extensions=tuple(payload.filters.archive_extensions),
            min_size_bytes=payload.filters.min_size_bytes,
            max_size_bytes=payload.filters.max_size_bytes,
            include_name=payload.filters.include_name,
            exclude_names=tuple(payload.filters.exclude_names),
            ignore_temp_files=payload.filters.ignore_temp_files,
            temp_patterns=tuple(payload.filters.temp_patterns),
            include_subdirectories=payload.filters.include_subdirectories,
            max_scan_depth=payload.filters.max_scan_depth,
        ),
    )
    return TaskDirectoryPreviewResponse(
        directory_path=view.directory_path,
        matched_count=view.matched_count,
        total_size_bytes=view.total_size_bytes,
        files=[
            TaskDirectoryFileResponse(
                relative_path=item.relative_path,
                size_bytes=item.size_bytes,
                device=item.device,
                inode=item.inode,
                mtime_ns=item.mtime_ns,
            )
            for item in view.files
        ],
    )


@router.post("/task-definitions/cron-preview", response_model=TaskCronPreviewResponse)
def preview_task_definition_cron(
    payload: TaskCronPreviewRequest,
    request: Request,
    _: Annotated[AccessPrincipal, Depends(TASKS_READ_ACCESS)],
) -> TaskCronPreviewResponse:
    try:
        cron_expression = normalize_cron_expression(payload.cron_expression)
    except ValueError as exc:
        raise ApplicationError(
            code="TASK_CRON_INVALID",
            status=422,
            title="Cron 表达式无效",
            detail=str(exc),
        ) from exc
    timezone = request.app.state.settings.timezone
    next_runs: list[datetime] = []
    cursor = datetime.now(UTC)
    for _index in range(5):
        cursor = next_cron_run(cron_expression, cursor, timezone=timezone)
        next_runs.append(cursor)
    return TaskCronPreviewResponse(
        cron_expression=cron_expression,
        timezone=timezone,
        description=_describe_cron(cron_expression),
        next_runs=next_runs,
    )


@router.post("/task-definitions/precheck", response_model=TaskDefinitionPrecheckResponse)
def precheck_task_definition(
    payload: TaskDefinitionCreateRequest,
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(TASKS_WRITE_ACCESS)],
) -> TaskDefinitionPrecheckResponse:
    result = task_definition_service(request).precheck_definition(_create_request(payload))
    return _precheck_response(result)


@router.get("/task-definitions/{definition_id}", response_model=TaskDefinitionResponse)
async def get_task_definition(
    definition_id: str,
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(TASKS_READ_ACCESS)],
) -> TaskDefinitionResponse:
    return _response(task_definition_service(request).get_definition(definition_id))


@router.post(
    "/task-definitions",
    response_model=TaskDefinitionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_task_definition(
    request: Request,
    payload: TaskDefinitionCreateRequest,
    _principal: Annotated[AccessPrincipal, Depends(TASKS_WRITE_ACCESS)],
) -> TaskDefinitionResponse:
    created = task_definition_service(request).create_definition(_create_request(payload))
    return _response(created)


@router.put("/task-definitions/{definition_id}", response_model=TaskDefinitionResponse)
async def update_task_definition(
    definition_id: str,
    request: Request,
    payload: TaskDefinitionCreateRequest,
    _principal: Annotated[AccessPrincipal, Depends(TASKS_WRITE_ACCESS)],
) -> TaskDefinitionResponse:
    updated = task_definition_service(request).update_definition(
        definition_id,
        _create_request(payload),
    )
    return _response(updated)


@router.delete("/task-definitions/{definition_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_task_definition(
    definition_id: str,
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(TASKS_WRITE_ACCESS)],
) -> Response:
    task_definition_service(request).delete_definition(definition_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/task-definitions/{definition_id}/scan",
    response_model=TaskMonitorScanResponse,
)
async def scan_monitor_task_definition(
    definition_id: str,
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(TASKS_WRITE_ACCESS)],
) -> TaskMonitorScanResponse:
    result = await task_definition_execution_service(request).scan_monitor(
        definition_id,
        trigger=TaskExecutionTrigger.IMMEDIATE_SCAN,
        trace_id=str(getattr(request.state, "trace_id", "unknown")),
    )
    return _monitor_scan_response(result)


@router.post(
    "/task-definitions/{definition_id}/actions",
    response_model=TaskDefinitionResponse,
)
async def act_on_task_definition(
    definition_id: str,
    payload: TaskDefinitionActionRequest,
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(TASKS_WRITE_ACCESS)],
) -> TaskDefinitionResponse:
    updated = task_definition_service(request).set_monitor_paused(
        definition_id,
        paused=payload.action == "pause",
    )
    return _response(updated)


@router.post(
    "/task-definitions/{definition_id}/executions",
    response_model=TaskExecutionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def execute_manual_task_definition(
    definition_id: str,
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(TASKS_WRITE_ACCESS)],
) -> TaskExecutionResponse:
    execution = await task_definition_execution_service(request).materialize_manual(
        definition_id,
        trace_id=str(getattr(request.state, "trace_id", "unknown")),
    )
    return _execution_response(execution)


@router.get(
    "/task-definitions/{definition_id}/executions",
    response_model=TaskExecutionListResponse,
)
async def list_task_definition_executions(
    definition_id: str,
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(TASKS_READ_ACCESS)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 20,
    status_filter: Annotated[str | None, Query(alias="status", max_length=24)] = None,
    trigger: Annotated[str | None, Query(max_length=24)] = None,
    search: Annotated[str | None, Query(max_length=512)] = None,
    started_from: datetime | None = None,
    started_to: datetime | None = None,
) -> TaskExecutionListResponse:
    result = task_definition_execution_service(request).list_executions(
        definition_id,
        page=page,
        page_size=page_size,
        status=status_filter,
        trigger=trigger,
        search=search,
        started_from=started_from,
        started_to=started_to,
    )
    return _execution_list_response(result)


@router.get(
    "/task-definitions/{definition_id}/executions/{execution_id}",
    response_model=TaskExecutionResponse,
)
async def get_task_definition_execution(
    definition_id: str,
    execution_id: str,
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(TASKS_READ_ACCESS)],
) -> TaskExecutionResponse:
    execution = task_definition_execution_service(request).get_execution(execution_id)
    if execution.task_definition_id != definition_id:
        raise ApplicationError(
            code="TASK_EXECUTION_NOT_FOUND",
            status=404,
            title="执行记录不存在",
            detail="未找到该任务定义下的指定执行记录",
        )
    return _execution_response(execution)


@router.post(
    "/task-definitions/{definition_id}/executions/{execution_id}/items/{item_id}/execution-plan",
    response_model=TaskExecutionPlanResponse,
)
async def create_task_definition_execution_plan(
    definition_id: str,
    execution_id: str,
    item_id: str,
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(TASKS_WRITE_ACCESS)],
) -> TaskExecutionPlanResponse:
    plan = await task_definition_execution_service(request).create_execution_plan(
        definition_id, execution_id, item_id
    )
    return TaskExecutionPlanResponse(
        id=plan.id,
        ready=plan.ready,
        current=plan.current,
        target_root=plan.target_root,
        target_downloader_id=plan.target_downloader_id,
        verification_level=plan.verification_level,
        hardlink_count=plan.hardlink_count,
        client_fetch_count=plan.client_fetch_count,
        create_directory_count=plan.create_directory_count,
        estimated_download_bytes_upper_bound=plan.estimated_download_bytes_upper_bound,
        blocked_reasons=list(plan.blocked_reasons),
        execution_allowed=plan.execution_allowed,
        side_effects_started=plan.side_effects_started,
    )


@router.post(
    "/task-definitions/{definition_id}/executions/{execution_id}/retry-failed",
    response_model=TaskExecutionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def retry_failed_task_definition_execution(
    definition_id: str,
    execution_id: str,
    request: Request,
    principal: Annotated[AccessPrincipal, Depends(TASKS_WRITE_ACCESS)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> TaskExecutionResponse:
    source = task_definition_execution_service(request).get_execution(execution_id)
    if source.task_definition_id != definition_id:
        raise ApplicationError(
            code="TASK_EXECUTION_NOT_FOUND",
            status=404,
            title="执行记录不存在",
            detail="未找到该任务定义下的指定执行记录",
        )
    execution = await task_definition_execution_service(request).retry_failed(
        execution_id,
        actor=TaskActionActor(principal.kind, principal.subject_id),
        idempotency_key=idempotency_key,
        trace_id=str(getattr(request.state, "trace_id", "unknown")),
    )
    return _execution_response(execution)
