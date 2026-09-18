import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from backend.app.api.dependencies import (
    AccessPrincipal,
    admin_notification_service,
    require_admin_csrf_principal,
    require_admin_principal,
)
from backend.app.application.backup_schedule import (
    BackupDriver,
    BackupPolicyView,
    BackupRunReport,
    BackupScheduleService,
)
from backend.app.application.errors import ApplicationError
from backend.app.application.notification_driver import NotificationDriver
from backend.app.application.system_health import (
    HealthStatus,
    MetricValue,
    SystemHealthReport,
    SystemHealthService,
)
from backend.app.application.system_upgrades import (
    SystemUpgradeActionResult,
    SystemUpgradeService,
    SystemUpgradeStatus,
)
from backend.app.application.task_driver import ActiveTaskDriver
from backend.app.infrastructure.app_logging import redact_fields, sanitize_message
from backend.app.infrastructure.backups import BackupError
from backend.app.infrastructure.diagnostics import build_diagnostic_bundle
from backend.app.infrastructure.operational_logs import (
    DEFAULT_LOG_QUERY_LIMIT,
    DEFAULT_LOG_WINDOW_MINUTES,
    MAX_LOG_EXPORT_LIMIT,
    MAX_LOG_QUERY_LIMIT,
    MAX_LOG_WINDOW_MINUTES,
    OperationalLogLevel,
    OperationalLogQueryResult,
    query_operational_logs,
)
from backend.app.infrastructure.persistence.models import (
    TaskExecution,
    TaskExecutionEvent,
    UnpackTask,
)
from backend.app.infrastructure.release_preflight import (
    ReleasePreflightReport,
    run_release_preflight,
)
from backend.app.infrastructure.runtime import RuntimeManager
from backend.app.infrastructure.site_reliability import SiteReliabilityRegistry
from backend.app.infrastructure.updater_protocol import UpdaterStatus
from backend.app.versioning import app_version

router = APIRouter(tags=["system"])
CONFIG_READ_ACCESS = require_admin_principal
CONFIG_WRITE_ACCESS = require_admin_csrf_principal


class SystemHealthCheckResponse(BaseModel):
    name: str
    status: HealthStatus
    code: str
    detail: str
    metrics: dict[str, MetricValue]


class SystemHealthResponse(BaseModel):
    status: HealthStatus
    generated_at: datetime
    version: str
    checks: list[SystemHealthCheckResponse]


class OperationalLogEntryResponse(BaseModel):
    timestamp: datetime
    level: OperationalLogLevel
    source: Literal["SYSTEM", "TASK_EVENT"] = "SYSTEM"
    logger: str
    message: str
    event_code: str | None = None
    task_id: str | None = None
    task_name: str | None = None
    execution_id: str | None = None
    trace_id: str | None = None
    fields: dict[str, Any]
    exception: str | None


class OperationalLogListResponse(BaseModel):
    window_minutes: int
    limit: int
    count: int
    truncated: bool
    max_file_bytes: int
    backup_count: int
    approximate_capacity_bytes: int
    items: list[OperationalLogEntryResponse]


class BackupPolicyResponse(BaseModel):
    enabled: bool
    interval_hours: int
    retention_days: int
    keep_latest: int
    version: int
    last_attempt_at: datetime | None
    last_success_at: datetime | None
    last_error_code: str | None
    driver_running: bool
    driver_consecutive_errors: int


class BackupPolicyUpdateRequest(BaseModel):
    enabled: bool
    interval_hours: int = Field(ge=1, le=168)
    retention_days: int = Field(ge=1, le=3650)
    keep_latest: int = Field(ge=1, le=100)


class BackupActionRequest(BaseModel):
    action: Literal["run_now"]


class BackupRunResponse(BaseModel):
    created: bool
    skipped_reason: Literal["DISABLED", "NOT_DUE"] | None
    created_at: datetime | None
    database_file: str | None
    database_size_bytes: int | None
    retention_deleted_count: int
    retention_blocked_count: int
    retention_error_code: str | None


class ReleasePreflightCheckResponse(BaseModel):
    name: str
    status: Literal["ok", "warning", "blocked"]
    code: str
    detail: str


class ReleasePreflightResponse(BaseModel):
    status: Literal["ready", "blocked"]
    app_version: str
    checks: list[ReleasePreflightCheckResponse]


class UpdaterStatusResponse(BaseModel):
    protocol_version: int
    helper_version: str
    phase: Literal[
        "idle",
        "accepted",
        "pulling",
        "stopping",
        "starting",
        "verifying",
        "succeeded",
        "rolling_back",
        "rolled_back",
        "failed",
        "manual_recovery_required",
    ]
    message: str
    request_id: str | None
    current_version: str | None
    target_version: str | None
    target_image: str | None
    backup_database_file: str | None
    started_at: str | None
    updated_at: str | None
    finished_at: str | None
    rollback_performed: bool


class SystemUpgradeStatusResponse(BaseModel):
    current_version: str
    latest_version: str | None
    update_available: bool
    target_tag: str | None
    target_image_digest: str | None
    immutable_image: str | None
    platform: str | None
    release_error_code: str | None
    helper_available: bool
    helper_status: UpdaterStatusResponse | None
    can_upgrade: bool
    blocked_reasons: list[str]


class SystemUpgradeActionRequest(BaseModel):
    action: Literal["upgrade"]
    target_version: str = Field(pattern=r"^\d+\.\d+\.\d+$", max_length=32)
    target_image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$", max_length=71)


class SystemUpgradeActionResponse(BaseModel):
    request_id: str
    current_version: str
    target_version: str
    target_image: str
    backup_database_file: str | None
    helper_status: UpdaterStatusResponse
    idempotency_replayed: bool


def _backup_policy_response(
    policy: BackupPolicyView,
    driver: BackupDriver,
) -> BackupPolicyResponse:
    state = driver.state
    return BackupPolicyResponse(
        enabled=policy.enabled,
        interval_hours=policy.interval_hours,
        retention_days=policy.retention_days,
        keep_latest=policy.keep_latest,
        version=policy.version,
        last_attempt_at=policy.last_attempt_at,
        last_success_at=policy.last_success_at,
        last_error_code=policy.last_error_code,
        driver_running=state.running,
        driver_consecutive_errors=state.consecutive_errors,
    )


def _backup_run_response(report: BackupRunReport) -> BackupRunResponse:
    return BackupRunResponse(
        created=report.created,
        skipped_reason=report.skipped_reason,
        created_at=report.created_at,
        database_file=report.database_file,
        database_size_bytes=report.database_size_bytes,
        retention_deleted_count=report.retention_deleted_count,
        retention_blocked_count=report.retention_blocked_count,
        retention_error_code=report.retention_error_code,
    )


def _release_preflight_response(report: ReleasePreflightReport) -> ReleasePreflightResponse:
    return ReleasePreflightResponse(
        status="ready" if report.ready else "blocked",
        app_version=report.app_version,
        checks=[
            ReleasePreflightCheckResponse(
                name=check.name,
                status=check.status,
                code=check.code,
                detail=check.detail,
            )
            for check in report.checks
        ],
    )


def _updater_status_response(status: UpdaterStatus) -> UpdaterStatusResponse:
    return UpdaterStatusResponse(
        protocol_version=status.protocol_version,
        helper_version=status.helper_version,
        phase=status.phase,
        message=status.message,
        request_id=status.request_id,
        current_version=status.current_version,
        target_version=status.target_version,
        target_image=status.target_image,
        backup_database_file=status.backup_database_file,
        started_at=status.started_at,
        updated_at=status.updated_at,
        finished_at=status.finished_at,
        rollback_performed=status.rollback_performed,
    )


def _system_upgrade_status_response(status: SystemUpgradeStatus) -> SystemUpgradeStatusResponse:
    return SystemUpgradeStatusResponse(
        current_version=status.current_version,
        latest_version=status.latest_version,
        update_available=status.update_available,
        target_tag=status.target_tag,
        target_image_digest=status.target_image_digest,
        immutable_image=status.immutable_image,
        platform=status.platform,
        release_error_code=status.release_error_code,
        helper_available=status.helper_available,
        helper_status=(
            None if status.helper_status is None else _updater_status_response(status.helper_status)
        ),
        can_upgrade=status.can_upgrade,
        blocked_reasons=list(status.blocked_reasons),
    )


def _system_upgrade_action_response(
    result: SystemUpgradeActionResult,
) -> SystemUpgradeActionResponse:
    return SystemUpgradeActionResponse(
        request_id=result.request_id,
        current_version=result.current_version,
        target_version=result.target_version,
        target_image=result.target_image,
        backup_database_file=result.backup_database_file,
        helper_status=_updater_status_response(result.helper_status),
        idempotency_replayed=result.idempotency_replayed,
    )


def _backup_service(request: Request) -> BackupScheduleService:
    return cast(BackupScheduleService, request.app.state.backup_schedule_service)


def _backup_driver(request: Request) -> BackupDriver:
    return cast(BackupDriver, request.app.state.backup_driver)


def _system_upgrade_service(request: Request) -> SystemUpgradeService:
    return cast(SystemUpgradeService, request.app.state.system_upgrade_service)


def _expected_backup_policy_version(value: str | None) -> int:
    if value is None:
        raise ApplicationError(
            code="IF_MATCH_REQUIRED",
            status=428,
            title="缺少版本前置条件",
            detail="修改备份策略时必须提供 If-Match 版本",
        )
    if len(value) < 3 or not value.startswith('"') or not value.endswith('"'):
        raise ApplicationError(
            code="IF_MATCH_INVALID",
            status=400,
            title="If-Match 格式无效",
            detail='If-Match 必须使用形如 "3" 的强 ETag',
        )
    try:
        parsed = int(value[1:-1])
    except ValueError as exc:
        raise ApplicationError(
            code="IF_MATCH_INVALID",
            status=400,
            title="If-Match 格式无效",
            detail='If-Match 必须使用形如 "3" 的强 ETag',
        ) from exc
    if parsed < 1:
        raise ApplicationError(
            code="IF_MATCH_INVALID",
            status=400,
            title="If-Match 格式无效",
            detail="备份策略版本必须大于等于 1",
        )
    return parsed


def _health_response(report: SystemHealthReport) -> SystemHealthResponse:
    return SystemHealthResponse(
        status=report.status,
        generated_at=report.generated_at,
        version=report.version,
        checks=[
            SystemHealthCheckResponse(
                name=check.name,
                status=check.status,
                code=check.code,
                detail=check.detail,
                metrics=check.metrics,
            )
            for check in report.checks
        ],
    )


def _operational_logs_response(
    result: OperationalLogQueryResult,
    *,
    max_file_bytes: int,
    backup_count: int,
    task_entries: tuple[OperationalLogEntryResponse, ...] = (),
    limit: int | None = None,
) -> OperationalLogListResponse:
    requested_limit = limit or result.limit
    system_entries = [
        OperationalLogEntryResponse(
            timestamp=entry.timestamp,
            level=entry.level,
            source="SYSTEM",
            logger=entry.logger,
            message=entry.message,
            trace_id=_string_field(entry.fields, "trace_id"),
            task_id=_string_field(entry.fields, "task_id")
            or _string_field(entry.fields, "task_definition_id"),
            execution_id=_string_field(entry.fields, "execution_id"),
            event_code=_string_field(entry.fields, "event_code"),
            fields=entry.fields,
            exception=entry.exception,
        )
        for entry in result.entries
    ]
    combined = [*system_entries, *task_entries]
    combined.sort(key=lambda entry: entry.timestamp, reverse=True)
    return OperationalLogListResponse(
        window_minutes=result.window_minutes,
        limit=requested_limit,
        count=min(len(combined), requested_limit),
        truncated=result.truncated or len(combined) > requested_limit,
        max_file_bytes=max_file_bytes,
        backup_count=backup_count,
        approximate_capacity_bytes=max_file_bytes * (backup_count + 1),
        items=combined[:requested_limit],
    )


def _string_field(fields: dict[str, Any], key: str) -> str | None:
    value = fields.get(key)
    return value if isinstance(value, str) and value else None


def _task_event_level(event_code: str) -> OperationalLogLevel:
    if "FAILED" in event_code:
        return "ERROR"
    if "REJECTED" in event_code:
        return "WARNING"
    return "INFO"


def _task_event_logs(
    request: Request,
    *,
    window_minutes: int,
    level: OperationalLogLevel | None,
    query: str | None,
    event_code: str | None,
    task_id: str | None,
    execution_id: str | None,
    trace_id: str | None,
    limit: int,
) -> tuple[OperationalLogEntryResponse, ...]:
    runtime = cast(RuntimeManager, request.app.state.runtime)
    now = datetime.now(UTC)
    cutoff = now - timedelta(minutes=window_minutes)
    statement = (
        select(TaskExecutionEvent, TaskExecution.task_definition_id, TaskExecution.task_name)
        .join(TaskExecution, TaskExecution.id == TaskExecutionEvent.execution_id)
        .where(
            TaskExecutionEvent.created_at >= cutoff,
            TaskExecutionEvent.created_at <= now + timedelta(minutes=1),
        )
        .order_by(TaskExecutionEvent.created_at.desc(), TaskExecutionEvent.id.desc())
        .limit(MAX_LOG_EXPORT_LIMIT + 1)
    )
    if event_code:
        statement = statement.where(TaskExecutionEvent.event_code == event_code)
    if task_id:
        statement = statement.where(TaskExecution.task_definition_id == task_id)
    if execution_id:
        statement = statement.where(TaskExecutionEvent.execution_id == execution_id)
    if trace_id:
        statement = statement.where(TaskExecutionEvent.trace_id == trace_id)

    normalized_query = query.strip().casefold() if query else ""
    entries: list[OperationalLogEntryResponse] = []
    with runtime.session_factory() as session:
        rows = session.execute(statement)
        for event, definition_id, task_name in rows:
            event_level = _task_event_level(event.event_code)
            if level is not None and event_level != level:
                continue
            context = redact_fields(event.context)
            safe_context = context if isinstance(context, dict) else {"value": context}
            if normalized_query:
                haystack = " ".join(
                    (
                        event.event_code,
                        event.message,
                        event.trace_id,
                        event.execution_id,
                        definition_id or "",
                        task_name,
                        json.dumps(safe_context, ensure_ascii=False, sort_keys=True),
                    )
                ).casefold()
                if normalized_query not in haystack:
                    continue
            entries.append(
                OperationalLogEntryResponse(
                    timestamp=event.created_at,
                    level=event_level,
                    source="TASK_EVENT",
                    logger="packbreaker.task_execution",
                    message=sanitize_message(event.message)[:4096],
                    event_code=event.event_code,
                    task_id=definition_id
                    or _string_field(cast(dict[str, Any], safe_context), "task_id"),
                    task_name=task_name,
                    execution_id=event.execution_id,
                    trace_id=event.trace_id,
                    fields=cast(dict[str, Any], safe_context),
                    exception=None,
                )
            )
            if len(entries) > limit:
                break
    return tuple(entries)


def _query_logs(
    request: Request,
    *,
    window_minutes: int,
    limit: int,
    level: OperationalLogLevel | None,
    query: str | None,
    source: Literal["SYSTEM", "TASK_EVENT"] | None = None,
    event_code: str | None = None,
    task_id: str | None = None,
    execution_id: str | None = None,
    trace_id: str | None = None,
) -> tuple[OperationalLogListResponse, int, int]:
    runtime = cast(RuntimeManager, request.app.state.runtime)
    settings = runtime.settings
    system_query = query or trace_id or execution_id or task_id or event_code
    result = query_operational_logs(
        settings.log_dir,
        backup_count=settings.log_file_backup_count,
        max_file_bytes=settings.log_file_max_bytes,
        window_minutes=window_minutes,
        limit=MAX_LOG_EXPORT_LIMIT,
        level=level,
        query=system_query,
    )
    if source == "TASK_EVENT":
        result = OperationalLogQueryResult(
            window_minutes=result.window_minutes,
            limit=result.limit,
            truncated=False,
            entries=(),
        )
    elif task_id or execution_id or trace_id or event_code:
        filtered_entries = tuple(
            entry
            for entry in result.entries
            if (
                not task_id
                or (
                    _string_field(entry.fields, "task_id")
                    or _string_field(entry.fields, "task_definition_id")
                )
                == task_id
            )
            and (not execution_id or _string_field(entry.fields, "execution_id") == execution_id)
            and (not trace_id or _string_field(entry.fields, "trace_id") == trace_id)
            and (not event_code or _string_field(entry.fields, "event_code") == event_code)
        )
        result = OperationalLogQueryResult(
            window_minutes=result.window_minutes,
            limit=result.limit,
            truncated=result.truncated,
            entries=filtered_entries,
        )

    task_entries: tuple[OperationalLogEntryResponse, ...] = ()
    if source != "SYSTEM":
        task_entries = _task_event_logs(
            request,
            window_minutes=window_minutes,
            level=level,
            query=query,
            event_code=event_code,
            task_id=task_id,
            execution_id=execution_id,
            trace_id=trace_id,
            limit=limit,
        )
    response = _operational_logs_response(
        result,
        max_file_bytes=settings.log_file_max_bytes,
        backup_count=settings.log_file_backup_count,
        task_entries=task_entries,
        limit=limit,
    )
    return response, settings.log_file_max_bytes, settings.log_file_backup_count


def _health_service(request: Request) -> SystemHealthService:
    runtime = cast(RuntimeManager, request.app.state.runtime)
    registry = cast(SiteReliabilityRegistry, request.app.state.site_reliability_registry)
    return SystemHealthService(
        runtime.session_factory,
        settings=runtime.settings,
        runtime=runtime,
        site_reliability_registry=registry,
    )


async def _health_report(request: Request) -> SystemHealthReport:
    task_driver = cast(ActiveTaskDriver, request.app.state.task_driver)
    notification_driver = cast(NotificationDriver, request.app.state.notification_driver)
    backup_driver = _backup_driver(request)
    return await _health_service(request).snapshot(
        task_driver=task_driver.state,
        notification_driver=notification_driver.state,
        backup_driver=backup_driver.state,
    )


@router.get("/system/status")
async def system_status(
    request: Request,
    principal: Annotated[AccessPrincipal, Depends(CONFIG_READ_ACCESS)],
) -> dict[str, object]:
    runtime = cast(RuntimeManager, request.app.state.runtime)
    task_driver = cast(ActiveTaskDriver, request.app.state.task_driver)
    driver_state = task_driver.state
    with runtime.session_factory() as session:
        rows = session.execute(
            select(UnpackTask.status, func.count()).group_by(UnpackTask.status)
        ).all()
    by_status = {status: count for status, count in rows}
    return {
        "version": app_version(),
        "authenticated_via": principal.kind,
        "tasks": {"total": sum(by_status.values()), "by_status": by_status},
        "checks": runtime.readiness().as_dict()["checks"],
        "worker": {
            "running": driver_state.running,
            "ticks_started": driver_state.ticks_started,
            "ticks_completed": driver_state.ticks_completed,
            "ticks_skipped": driver_state.ticks_skipped,
            "consecutive_errors": driver_state.consecutive_errors,
            "last_error_type": driver_state.last_error_type,
            "last_tick_started_at": (
                None
                if driver_state.last_tick_started_at is None
                else driver_state.last_tick_started_at.isoformat()
            ),
            "last_tick_completed_at": (
                None
                if driver_state.last_tick_completed_at is None
                else driver_state.last_tick_completed_at.isoformat()
            ),
            "last_scanned_count": driver_state.last_scanned_count,
            "last_completed_count": driver_state.last_completed_count,
            "last_waiting_count": driver_state.last_waiting_count,
            "last_blocked_count": driver_state.last_blocked_count,
            "last_truncated": driver_state.last_truncated,
        },
    }


@router.get("/system/release/preflight", response_model=ReleasePreflightResponse)
def release_preflight(
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(CONFIG_READ_ACCESS)],
) -> JSONResponse:
    """只做本地发布预检；不连接外部服务，也不创建备份演练文件。"""

    runtime = cast(RuntimeManager, request.app.state.runtime)
    report = run_release_preflight(
        runtime.settings,
        app_version=app_version(),
        exercise_backup=False,
    )
    response = _release_preflight_response(report)
    return JSONResponse(response.model_dump(mode="json"), headers={"Cache-Control": "no-store"})


@router.get("/system/upgrade", response_model=SystemUpgradeStatusResponse)
async def system_upgrade_status(
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(CONFIG_READ_ACCESS)],
) -> JSONResponse:
    status = await _system_upgrade_service(request).status()
    if status.update_available and status.latest_version is not None:
        admin_notification_service(request).record_version_update(
            current_version=status.current_version,
            latest_version=status.latest_version,
        )
    response = _system_upgrade_status_response(status)
    return JSONResponse(response.model_dump(mode="json"), headers={"Cache-Control": "no-store"})


@router.post(
    "/system/upgrade/actions",
    response_model=SystemUpgradeActionResponse,
    status_code=202,
)
async def system_upgrade_action(
    request: Request,
    payload: SystemUpgradeActionRequest,
    _principal: Annotated[AccessPrincipal, Depends(CONFIG_WRITE_ACCESS)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> SystemUpgradeActionResponse:
    result = await _system_upgrade_service(request).execute(
        target_version=payload.target_version,
        target_image_digest=payload.target_image_digest,
        idempotency_key=idempotency_key,
        backup_driver=_backup_driver(request),
    )
    return _system_upgrade_action_response(result)


@router.get("/system/backups/policy", response_model=BackupPolicyResponse)
async def get_backup_policy(
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(CONFIG_READ_ACCESS)],
) -> JSONResponse:
    policy = _backup_service(request).get()
    response = _backup_policy_response(policy, _backup_driver(request))
    return JSONResponse(
        response.model_dump(mode="json"),
        headers={"ETag": f'"{policy.version}"', "Cache-Control": "no-store"},
    )


@router.put("/system/backups/policy", response_model=BackupPolicyResponse)
async def update_backup_policy(
    request: Request,
    payload: BackupPolicyUpdateRequest,
    _principal: Annotated[AccessPrincipal, Depends(CONFIG_WRITE_ACCESS)],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> JSONResponse:
    policy = _backup_service(request).update(
        expected_version=_expected_backup_policy_version(if_match),
        enabled=payload.enabled,
        interval_hours=payload.interval_hours,
        retention_days=payload.retention_days,
        keep_latest=payload.keep_latest,
    )
    response = _backup_policy_response(policy, _backup_driver(request))
    return JSONResponse(
        response.model_dump(mode="json"),
        headers={"ETag": f'"{policy.version}"', "Cache-Control": "no-store"},
    )


@router.post("/system/backups/actions", response_model=BackupRunResponse)
async def backup_action(
    request: Request,
    payload: BackupActionRequest,
    _principal: Annotated[AccessPrincipal, Depends(CONFIG_WRITE_ACCESS)],
) -> BackupRunResponse:
    if payload.action != "run_now":
        raise ApplicationError(
            code="BACKUP_ACTION_INVALID",
            status=422,
            title="备份动作无效",
            detail="当前仅支持 run_now",
        )
    try:
        report = await _backup_driver(request).run_once(force=True)
    except (BackupError, OSError) as exc:
        raise ApplicationError(
            code="BACKUP_RUN_FAILED",
            status=503,
            title="一致性备份失败",
            detail="本地一致性备份未完成，请查看已脱敏运行日志",
        ) from exc
    if report is None:
        raise ApplicationError(
            code="BACKUP_RUN_BUSY",
            status=409,
            title="已有备份正在执行",
            detail="计划或手动备份正在执行，请稍后读取状态，不会并发创建第二份备份",
        )
    return _backup_run_response(report)


@router.get("/system/health", response_model=SystemHealthResponse)
async def system_health(
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(CONFIG_READ_ACCESS)],
) -> SystemHealthResponse:
    """聚合已有本地/依赖证据；不会主动访问 PT、下载器或通知渠道。"""

    return _health_response(await _health_report(request))


@router.get("/system/diagnostics/export")
async def export_system_diagnostics(
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(CONFIG_READ_ACCESS)],
) -> Response:
    """导出白名单聚合指标 ZIP；不包含日志、URL、路径、ID、hash 或凭证。"""

    bundle = build_diagnostic_bundle(await _health_report(request))
    return Response(
        content=bundle.content,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{bundle.filename}"',
            "X-Content-SHA256": bundle.sha256,
            "Cache-Control": "no-store",
        },
    )


@router.get("/system/logs", response_model=OperationalLogListResponse)
async def list_system_logs(
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(CONFIG_READ_ACCESS)],
    window_minutes: Annotated[
        int, Query(ge=1, le=MAX_LOG_WINDOW_MINUTES)
    ] = DEFAULT_LOG_WINDOW_MINUTES,
    limit: Annotated[int, Query(ge=1, le=MAX_LOG_QUERY_LIMIT)] = DEFAULT_LOG_QUERY_LIMIT,
    level: OperationalLogLevel | None = None,
    query: Annotated[str | None, Query(alias="q", max_length=128)] = None,
    source: Literal["SYSTEM", "TASK_EVENT"] | None = None,
    event_code: Annotated[str | None, Query(max_length=96)] = None,
    task_id: Annotated[str | None, Query(max_length=36)] = None,
    execution_id: Annotated[str | None, Query(max_length=36)] = None,
    trace_id: Annotated[str | None, Query(max_length=64)] = None,
) -> OperationalLogListResponse:
    """查询有界、持久、已脱敏的本地运行日志；不读取 Docker daemon 日志。"""

    result, _, _ = _query_logs(
        request,
        window_minutes=window_minutes,
        limit=limit,
        level=level,
        query=query,
        source=source,
        event_code=event_code,
        task_id=task_id,
        execution_id=execution_id,
        trace_id=trace_id,
    )
    return result


@router.get("/system/logs/export")
async def export_system_logs(
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(CONFIG_READ_ACCESS)],
    window_minutes: Annotated[
        int, Query(ge=1, le=MAX_LOG_WINDOW_MINUTES)
    ] = DEFAULT_LOG_WINDOW_MINUTES,
    limit: Annotated[int, Query(ge=1, le=MAX_LOG_EXPORT_LIMIT)] = MAX_LOG_EXPORT_LIMIT,
    level: OperationalLogLevel | None = None,
    query: Annotated[str | None, Query(alias="q", max_length=128)] = None,
    source: Literal["SYSTEM", "TASK_EVENT"] | None = None,
    event_code: Annotated[str | None, Query(max_length=96)] = None,
    task_id: Annotated[str | None, Query(max_length=36)] = None,
    execution_id: Annotated[str | None, Query(max_length=36)] = None,
    trace_id: Annotated[str | None, Query(max_length=64)] = None,
) -> Response:
    """导出同一受控查询结果；硬上限 2000 条并再次经过读取端脱敏。"""

    result, max_file_bytes, backup_count = _query_logs(
        request,
        window_minutes=window_minutes,
        limit=limit,
        level=level,
        query=query,
        source=source,
        event_code=event_code,
        task_id=task_id,
        execution_id=execution_id,
        trace_id=trace_id,
    )
    payload = {
        "format_version": 1,
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "log_policy": {
            "window_minutes": result.window_minutes,
            "limit": result.limit,
            "truncated": result.truncated,
            "max_file_bytes": max_file_bytes,
            "backup_count": backup_count,
            "approximate_capacity_bytes": max_file_bytes * (backup_count + 1),
        },
        "items": [entry.model_dump(mode="json") for entry in result.items],
    }
    content = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    digest = hashlib.sha256(content).hexdigest()
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return Response(
        content=content,
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="packbreaker-logs-{timestamp}.json"',
            "X-Content-SHA256": digest,
            "Cache-Control": "no-store",
        },
    )
