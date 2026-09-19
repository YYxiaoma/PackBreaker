import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response

from backend.app.api.ai_agent import router as ai_agent_router
from backend.app.api.auth import router as auth_router
from backend.app.api.downloaders import router as downloader_router
from backend.app.api.health import router as health_router
from backend.app.api.notifications import router as notification_router
from backend.app.api.sites import router as site_router
from backend.app.api.system import router as system_router
from backend.app.api.task_definitions import router as task_definition_router
from backend.app.api.tasks import router as task_router
from backend.app.application.admin_notifications import AdminNotificationService
from backend.app.application.ai_agent import AIAgentService
from backend.app.application.ai_runtime import AIReadOnlyAgent
from backend.app.application.ai_telegram import AITelegramService
from backend.app.application.ai_telegram_driver import AITelegramDriver
from backend.app.application.ai_tools import AIToolService
from backend.app.application.auth import AuthService
from backend.app.application.backup_schedule import BackupDriver, BackupScheduleService
from backend.app.application.downloader_operations import (
    QbittorrentAddOperationService,
    QbittorrentJournalReconcileService,
    QbittorrentRecheckOperationService,
    QbittorrentRemoveOperationService,
    QbittorrentStartOperationService,
)
from backend.app.application.downloaders import DownloaderService
from backend.app.application.errors import ApplicationError
from backend.app.application.filesystem_operations import FilesystemOperationService
from backend.app.application.notification_driver import NotificationDriver
from backend.app.application.notifications import NotificationService
from backend.app.application.repair_downloader_operations import RepairDownloadOperationService
from backend.app.application.secrets import SecretStore
from backend.app.application.sites import SiteService
from backend.app.application.system_upgrades import SystemUpgradeService
from backend.app.application.task_actions import TaskActionService
from backend.app.application.task_adding import TaskAddingCoordinator
from backend.app.application.task_cancellation import TaskCancellationCoordinator
from backend.app.application.task_client_verification import TaskClientVerificationCoordinator
from backend.app.application.task_definition_driver import TaskDefinitionDriver
from backend.app.application.task_definition_executions import TaskDefinitionExecutionService
from backend.app.application.task_definitions import TaskDefinitionService
from backend.app.application.task_driver import ActiveTaskDriver
from backend.app.application.task_events import TaskEventService
from backend.app.application.task_linking import TaskLinkingCoordinator
from backend.app.application.task_operations import TaskOperationService
from backend.app.application.task_recovery import TaskRecoveryCoordinator
from backend.app.application.task_repair_actions import TaskRepairActionService
from backend.app.application.task_repairs import (
    TaskRepairCoordinator,
    TaskRepairIsolationCoordinator,
    TaskRepairPlanService,
)
from backend.app.application.task_seeding import TaskSeedingCoordinator
from backend.app.application.task_telegram_approvals import TaskTelegramApprovalService
from backend.app.application.tasks import TaskAnalysisService
from backend.app.application.transmission_operations import (
    TransmissionAddOperationService,
    TransmissionJournalReconcileService,
    TransmissionRemoveOperationService,
    TransmissionStartOperationService,
    TransmissionVerifyOperationService,
)
from backend.app.config import AppSettings
from backend.app.infrastructure.http_security import TrustedProxyPolicy, apply_security_headers
from backend.app.infrastructure.runtime import RuntimeManager
from backend.app.infrastructure.safe_filesystem import SafeFilesystemGateway
from backend.app.infrastructure.site_reliability import SiteReliabilityRegistry
from backend.app.versioning import app_version

_request_logger = logging.getLogger("packbreaker.http")
_recovery_logger = logging.getLogger("packbreaker.recovery")

_HTTP_EXACT_RESOURCE_LABELS: dict[str, str] = {
    "/": "管理页面",
    "/api/v1/notification-channels": "通知渠道列表",
    "/api/v1/notifications/inbox/unread-count": "站内通知未读数量",
    "/api/v1/system/health": "系统健康状态",
    "/api/v1/system/logs": "运行日志列表",
    "/api/v1/downloaders": "下载器列表",
    "/api/v1/sites": "站点列表",
    "/api/v1/task-definitions": "任务定义列表",
}

_HTTP_RESOURCE_LABELS: tuple[tuple[str, str], ...] = (
    ("/api/v1/notifications/inbox", "站内通知"),
    ("/api/v1/notification-channels", "通知渠道"),
    ("/api/v1/system/health", "系统健康状态"),
    ("/api/v1/system/logs/export", "运行日志导出"),
    ("/api/v1/system/logs", "运行日志"),
    ("/api/v1/system/backups", "备份配置"),
    ("/api/v1/system/upgrade", "系统升级"),
    ("/api/v1/health/live", "服务存活状态"),
    ("/api/v1/health/ready", "服务就绪状态"),
    ("/api/v1/task-definitions", "任务定义"),
    ("/api/v1/tasks", "执行引擎任务"),
    ("/api/v1/downloaders", "下载器"),
    ("/api/v1/sites", "站点"),
    ("/api/v1/ai-agent", "AI 助手"),
    ("/api/v1/auth/me", "管理员会话状态"),
    ("/api/v1/auth/login", "管理员登录"),
    ("/api/v1/auth/logout", "管理员退出"),
    ("/api/v1/auth/password", "管理员密码"),
    ("/assets", "前端静态资源"),
)


def _trace_id(value: str | None) -> UUID:
    if value:
        try:
            return UUID(value)
        except ValueError:
            return uuid4()
    return uuid4()


def _http_resource_label(path: str) -> str:
    exact = _HTTP_EXACT_RESOURCE_LABELS.get(path)
    if exact is not None:
        return exact
    for prefix, label in _HTTP_RESOURCE_LABELS:
        if path == prefix or path.startswith(f"{prefix}/"):
            return label
    return f"接口 {path}"


def _http_request_message(method: str, path: str, status_code: int) -> str:
    resource = _http_resource_label(path)
    if method == "GET":
        action = f"获取{resource}"
    elif method == "DELETE":
        action = f"删除{resource}"
    elif method in {"PUT", "PATCH"}:
        action = f"更新{resource}"
    elif method == "POST" and path.endswith("/test"):
        action = f"测试{resource}连接"
    elif method == "POST" and "/actions" in path:
        action = f"执行{resource}操作"
    elif method == "POST":
        action = f"提交{resource}操作"
    else:
        action = f"{method} {resource}"
    return f"{action}{'失败' if status_code >= 400 else '完成'}"


def _attach_frontend(app: FastAPI, settings: AppSettings) -> None:
    frontend_dir = settings.frontend_dir
    if frontend_dir is None:
        return

    index_path = frontend_dir / "index.html"
    assets_dir = frontend_dir / "assets"
    if not frontend_dir.is_dir() or not index_path.is_file() or not assets_dir.is_dir():
        raise ValueError("前端静态目录必须包含 index.html 与 assets 目录")

    app.mount("/assets", StaticFiles(directory=assets_dir), name="frontend-assets")

    @app.get("/", include_in_schema=False)
    async def frontend_index() -> FileResponse:
        return FileResponse(index_path)


def create_app(
    *,
    settings: AppSettings | None = None,
    runtime: RuntimeManager | None = None,
) -> FastAPI:
    if runtime is not None:
        resolved_settings = settings or runtime.settings
        if runtime.settings != resolved_settings:
            raise ValueError("注入的 runtime 与 settings 不一致")
        resolved_runtime = runtime
    else:
        resolved_settings = settings or AppSettings()
        resolved_runtime = RuntimeManager(resolved_settings)

    proxy_policy = TrustedProxyPolicy(resolved_settings.trusted_proxy_list)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        resolved_runtime.start()
        app.state.runtime = resolved_runtime
        app.state.auth_service = AuthService(resolved_runtime.session_factory)
        app.state.admin_notification_service = AdminNotificationService(
            resolved_runtime.session_factory
        )
        secret_store = SecretStore(
            resolved_runtime.session_factory,
            resolved_runtime.secret_cipher,
        )
        app.state.secret_store = secret_store
        ai_agent_service = AIAgentService(resolved_runtime.session_factory, secret_store)
        ai_agent_service.ensure_default()
        app.state.ai_agent_service = ai_agent_service
        notification_service = NotificationService(resolved_runtime.session_factory, secret_store)
        app.state.notification_service = notification_service
        downloader_service = DownloaderService(
            resolved_runtime.session_factory,
            secret_store,
            data_root=resolved_settings.data_dir,
        )
        app.state.downloader_service = downloader_service
        qbit_operations = QbittorrentAddOperationService(resolved_runtime.session_factory)
        app.state.qbittorrent_add_operation_service = qbit_operations
        qbit_recheck_operations = QbittorrentRecheckOperationService(
            resolved_runtime.session_factory
        )
        app.state.qbittorrent_recheck_operation_service = qbit_recheck_operations
        repair_download_operations = RepairDownloadOperationService(
            resolved_runtime.session_factory
        )
        app.state.repair_download_operation_service = repair_download_operations
        transmission_add_operations = TransmissionAddOperationService(
            resolved_runtime.session_factory
        )
        app.state.transmission_add_operation_service = transmission_add_operations
        transmission_verify_operations = TransmissionVerifyOperationService(
            resolved_runtime.session_factory
        )
        app.state.transmission_verify_operation_service = transmission_verify_operations
        transmission_start_operations = TransmissionStartOperationService(
            resolved_runtime.session_factory
        )
        app.state.transmission_start_operation_service = transmission_start_operations
        transmission_remove_operations = TransmissionRemoveOperationService(
            resolved_runtime.session_factory
        )
        app.state.transmission_remove_operation_service = transmission_remove_operations
        qbit_start_operations = QbittorrentStartOperationService(resolved_runtime.session_factory)
        app.state.qbittorrent_start_operation_service = qbit_start_operations
        qbit_remove_operations = QbittorrentRemoveOperationService(resolved_runtime.session_factory)
        app.state.qbittorrent_remove_operation_service = qbit_remove_operations
        qbit_journal_reconcile = QbittorrentJournalReconcileService(
            resolved_runtime.session_factory
        )
        app.state.qbittorrent_journal_reconcile_service = qbit_journal_reconcile
        transmission_journal_reconcile = TransmissionJournalReconcileService(
            resolved_runtime.session_factory
        )
        app.state.transmission_journal_reconcile_service = transmission_journal_reconcile
        site_reliability_registry = SiteReliabilityRegistry(
            event_sink=notification_service.record_site_reliability_event
        )
        app.state.site_reliability_registry = site_reliability_registry
        site_service = SiteService(
            resolved_runtime.session_factory,
            secret_store,
            reliability_registry=site_reliability_registry,
        )
        app.state.site_service = site_service
        app.state.task_definition_service = TaskDefinitionService(
            resolved_runtime.session_factory,
            data_root=resolved_settings.data_dir,
            timezone=resolved_settings.timezone,
        )
        task_analysis_service = TaskAnalysisService(
            resolved_runtime.session_factory,
            site_service,
            data_root=resolved_settings.data_dir,
        )
        app.state.task_analysis_service = task_analysis_service
        task_repair_plan_service = TaskRepairPlanService(
            resolved_runtime.session_factory,
            site_service,
            downloader_service,
            data_root=resolved_settings.data_dir,
        )
        app.state.task_repair_plan_service = task_repair_plan_service
        app.state.task_event_service = TaskEventService(resolved_runtime.session_factory)
        filesystem_operations = FilesystemOperationService(
            resolved_runtime.session_factory,
            SafeFilesystemGateway(resolved_settings.data_dir),
        )
        app.state.filesystem_operation_service = filesystem_operations
        task_repair_isolation_coordinator = TaskRepairIsolationCoordinator(
            task_repair_plan_service,
            filesystem_operations,
        )
        app.state.task_repair_isolation_coordinator = task_repair_isolation_coordinator
        task_repair_coordinator = TaskRepairCoordinator(
            resolved_runtime.session_factory,
            task_repair_plan_service,
            task_repair_isolation_coordinator,
        )
        app.state.task_repair_coordinator = task_repair_coordinator
        app.state.task_repair_action_service = TaskRepairActionService(
            resolved_runtime.session_factory,
            task_repair_coordinator,
        )
        app.state.task_operation_service = TaskOperationService(
            resolved_runtime.session_factory,
            filesystem_operations,
            downloader_service,
            qbit_journal_reconcile,
            transmission_journal_reconcile,
            repair_download_operations,
        )
        task_linking_coordinator = TaskLinkingCoordinator(
            resolved_runtime.session_factory,
            task_analysis_service,
            filesystem_operations,
            data_root=resolved_settings.data_dir,
        )
        app.state.task_linking_coordinator = task_linking_coordinator
        task_adding_coordinator = TaskAddingCoordinator(
            resolved_runtime.session_factory,
            site_service,
            downloader_service,
            qbit_operations,
            transmission_add_operations,
            data_root=resolved_settings.data_dir,
        )
        app.state.task_adding_coordinator = task_adding_coordinator
        task_client_verification_coordinator = TaskClientVerificationCoordinator(
            resolved_runtime.session_factory,
            downloader_service,
            qbit_recheck_operations,
            transmission_verify_operations,
            repair_download_operations,
            data_root=resolved_settings.data_dir,
        )
        app.state.task_client_verification_coordinator = task_client_verification_coordinator
        task_seeding_coordinator = TaskSeedingCoordinator(
            resolved_runtime.session_factory,
            downloader_service,
            qbit_start_operations,
            transmission_start_operations,
            data_root=resolved_settings.data_dir,
        )
        app.state.task_seeding_coordinator = task_seeding_coordinator
        task_cancellation_coordinator = TaskCancellationCoordinator(
            resolved_runtime.session_factory,
            downloader_service,
            qbit_remove_operations,
            filesystem_operations,
            transmission_remove_operations,
        )
        app.state.task_cancellation_coordinator = task_cancellation_coordinator
        task_action_service = TaskActionService(
            resolved_runtime.session_factory,
            task_linking_coordinator,
            task_cancellation_coordinator,
            task_cancellation_coordinator,
        )
        app.state.task_action_service = task_action_service
        task_definition_execution_service = TaskDefinitionExecutionService(
            resolved_runtime.session_factory,
            downloader_service,
            task_action_service,
            task_analysis_service,
            data_root=resolved_settings.data_dir,
            directory_scan_batch_size=resolved_settings.task_definition_directory_scan_batch_size,
        )
        app.state.task_definition_execution_service = task_definition_execution_service
        task_telegram_approval_service = TaskTelegramApprovalService(
            resolved_runtime.session_factory,
            task_definition_execution_service,
        )
        app.state.task_telegram_approval_service = task_telegram_approval_service
        task_recovery_coordinator = TaskRecoveryCoordinator(
            resolved_runtime.session_factory,
            task_linking_coordinator,
            task_adding_coordinator,
            task_client_verification_coordinator,
            task_seeding_coordinator,
            task_cancellation_coordinator,
        )
        app.state.task_recovery_coordinator = task_recovery_coordinator
        try:
            recovery_report = await task_recovery_coordinator.reconcile_once(
                recover_abandoned_analysis=True
            )
        except BaseException:
            resolved_runtime.stop()
            raise
        app.state.task_recovery_report = recovery_report
        if recovery_report.blocked_count:
            _recovery_logger.warning(
                "启动恢复发现阻断任务 blocked_tasks=%s scanned=%s truncated=%s",
                recovery_report.blocked_count,
                recovery_report.scanned_count,
                recovery_report.truncated,
            )
        elif recovery_report.scanned_count:
            _recovery_logger.info(
                "启动恢复完成 scanned=%s completed=%s waiting=%s truncated=%s",
                recovery_report.scanned_count,
                recovery_report.completed_count,
                recovery_report.waiting_count,
                recovery_report.truncated,
            )
        task_driver = ActiveTaskDriver(
            task_recovery_coordinator,
            interval_seconds=resolved_settings.task_driver_interval_seconds,
            limit=resolved_settings.task_driver_limit,
            max_steps_per_task=resolved_settings.task_driver_max_steps_per_task,
        )
        app.state.task_driver = task_driver
        task_driver.start()
        task_definition_driver = TaskDefinitionDriver(
            task_definition_execution_service,
            interval_seconds=resolved_settings.task_definition_driver_interval_seconds,
            scan_limit=resolved_settings.task_definition_driver_scan_limit,
            retry_limit=resolved_settings.task_definition_driver_retry_limit,
        )
        app.state.task_definition_driver = task_definition_driver
        task_definition_driver.start()
        notification_driver = NotificationDriver(
            notification_service,
            interval_seconds=resolved_settings.notification_driver_interval_seconds,
            limit=resolved_settings.notification_driver_limit,
            max_attempts=resolved_settings.notification_max_attempts,
        )
        app.state.notification_driver = notification_driver
        notification_driver.start()
        backup_schedule_service = BackupScheduleService(resolved_runtime.session_factory)
        app.state.backup_schedule_service = backup_schedule_service
        backup_driver = BackupDriver(
            backup_schedule_service,
            settings=resolved_settings,
            interval_seconds=resolved_settings.backup_driver_interval_seconds,
        )
        app.state.backup_driver = backup_driver
        system_upgrade_service = SystemUpgradeService(resolved_settings)
        app.state.system_upgrade_service = system_upgrade_service
        ai_tool_service = AIToolService(
            resolved_runtime.session_factory,
            settings=resolved_settings,
            runtime=resolved_runtime,
            site_reliability_registry=site_reliability_registry,
            task_definition_service=app.state.task_definition_service,
            downloader_service=downloader_service,
            site_service=site_service,
            system_upgrade_service=system_upgrade_service,
            task_driver=task_driver,
            notification_driver=notification_driver,
            backup_driver=backup_driver,
            help_root=Path(__file__).resolve().parents[2],
        )
        app.state.ai_tool_service = ai_tool_service
        app.state.ai_read_only_agent = AIReadOnlyAgent(ai_agent_service, ai_tool_service)
        ai_telegram_service = AITelegramService(
            resolved_runtime.session_factory,
            notification_service=notification_service,
            ai_agent_service=ai_agent_service,
            agent=app.state.ai_read_only_agent,
        )
        ai_telegram_service.ensure_default()
        app.state.ai_telegram_service = ai_telegram_service
        ai_telegram_driver = AITelegramDriver(
            ai_telegram_service,
            notification_service,
            interval_seconds=resolved_settings.ai_telegram_driver_interval_seconds,
            poll_timeout_seconds=resolved_settings.ai_telegram_poll_timeout_seconds,
            poll_limit=resolved_settings.ai_telegram_poll_limit,
            approval_service=task_telegram_approval_service,
        )
        app.state.ai_telegram_driver = ai_telegram_driver
        backup_driver.start()
        ai_telegram_driver.start()
        try:
            yield
        finally:
            await ai_telegram_driver.stop()
            await backup_driver.stop()
            await notification_driver.stop()
            await task_definition_driver.stop()
            await task_driver.stop()
            resolved_runtime.stop()

    app = FastAPI(
        title="PackBreaker",
        version=app_version(),
        openapi_url="/api/openapi.json",
        docs_url="/api/docs",
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.settings = resolved_settings
    app.state.runtime = resolved_runtime

    @app.exception_handler(ApplicationError)
    async def handle_application_error(request: Request, exc: ApplicationError) -> JSONResponse:
        trace_id = getattr(request.state, "trace_id", str(uuid4()))
        headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after is not None else None
        problem_type = exc.code.lower().replace("_", "-")
        return JSONResponse(
            status_code=exc.status,
            media_type="application/problem+json",
            headers=headers,
            content={
                "type": f"https://packbreaker.dev/problems/{problem_type}",
                "title": exc.title,
                "status": exc.status,
                "detail": exc.detail,
                "code": exc.code,
                "trace_id": trace_id,
            },
        )

    @app.middleware("http")
    async def request_context(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        started_at = time.perf_counter()
        trace_id = _trace_id(request.headers.get("X-Trace-Id"))
        request.state.trace_id = str(trace_id)
        network = proxy_policy.resolve(
            direct_host=request.client.host if request.client is not None else None,
            direct_scheme=request.url.scheme,
            forwarded_for=request.headers.get("X-Forwarded-For"),
            forwarded_proto=request.headers.get("X-Forwarded-Proto"),
        )
        request.state.client_source = network.client_source
        request.state.effective_scheme = network.scheme
        try:
            response = await call_next(request)
        except Exception:
            _request_logger.error(
                "请求处理失败",
                extra={
                    "fields": {
                        "trace_id": str(trace_id),
                        "method": request.method,
                        "path": request.url.path,
                        "client_source": network.client_source,
                        "duration_ms": round((time.perf_counter() - started_at) * 1000, 3),
                    }
                },
                exc_info=True,
            )
            raise
        response.headers["X-Trace-Id"] = str(trace_id)
        apply_security_headers(
            path=request.url.path, scheme=network.scheme, headers=response.headers
        )
        _request_logger.info(
            _http_request_message(request.method, request.url.path, response.status_code),
            extra={
                "fields": {
                    "trace_id": str(trace_id),
                    "method": request.method,
                    "path": request.url.path,
                    "status_code": response.status_code,
                    "client_source": network.client_source,
                    "duration_ms": round((time.perf_counter() - started_at) * 1000, 3),
                }
            },
        )
        return response

    app.include_router(ai_agent_router, prefix="/api/v1")
    app.include_router(auth_router, prefix="/api/v1")
    app.include_router(downloader_router, prefix="/api/v1")
    app.include_router(health_router, prefix="/api/v1")
    app.include_router(notification_router, prefix="/api/v1")
    app.include_router(site_router, prefix="/api/v1")
    app.include_router(system_router, prefix="/api/v1")
    app.include_router(task_definition_router, prefix="/api/v1")
    app.include_router(task_router, prefix="/api/v1")
    _attach_frontend(app, resolved_settings)
    return app


app = create_app()
