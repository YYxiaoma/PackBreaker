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
from backend.app.api.cookiecloud import router as cookiecloud_router
from backend.app.api.downloaders import router as downloader_router
from backend.app.api.health import router as health_router
from backend.app.api.movie_dedup import router as movie_dedup_router
from backend.app.api.notifications import router as notification_router
from backend.app.api.sites import router as site_router
from backend.app.api.system import router as system_router
from backend.app.api.unpack import router as unpack_router
from backend.app.application.admin_notifications import AdminNotificationService
from backend.app.application.ai_agent import AIAgentService
from backend.app.application.ai_runtime import AIReadOnlyAgent
from backend.app.application.ai_telegram import AITelegramService
from backend.app.application.ai_telegram_driver import AITelegramDriver
from backend.app.application.ai_tools import AIToolService
from backend.app.application.auth import AuthService
from backend.app.application.backup_schedule import BackupDriver, BackupScheduleService
from backend.app.application.cookiecloud import CookieCloudService
from backend.app.application.cookiecloud_driver import CookieCloudDriver
from backend.app.application.downloaders import DownloaderService
from backend.app.application.errors import ApplicationError
from backend.app.application.movie_dedup import MovieDedupService
from backend.app.application.movie_dedup_driver import MovieDedupDriver
from backend.app.application.notification_driver import NotificationDriver
from backend.app.application.notifications import NotificationService
from backend.app.application.secrets import SecretStore
from backend.app.application.sites import SiteService
from backend.app.application.system_health import SystemResourceSampler
from backend.app.application.system_upgrades import SystemUpgradeService
from backend.app.application.unpack_auxiliary import UnpackAuxiliaryStagingService
from backend.app.application.unpack_content_verification import (
    UnpackContentVerificationService,
)
from backend.app.application.unpack_definitions import UnpackDefinitionService
from backend.app.application.unpack_discovery import UnpackDiscoveryService
from backend.app.application.unpack_driver import UnpackDriver
from backend.app.application.unpack_execution_plans import UnpackExecutionPlanService
from backend.app.application.unpack_executions import UnpackExecutionQueryService
from backend.app.application.unpack_item_actions import UnpackItemActionService
from backend.app.application.unpack_matching import UnpackMatchCoordinator
from backend.app.application.unpack_materialization import UnpackMaterializationService
from backend.app.application.unpack_monitor import UnpackMonitorService
from backend.app.application.unpack_seeding import UnpackSeedingService
from backend.app.application.unpack_source_scans import UnpackSourceScanService
from backend.app.config import AppSettings
from backend.app.infrastructure.authorized_paths import AuthorizedPathScope
from backend.app.infrastructure.http_security import TrustedProxyPolicy, apply_security_headers
from backend.app.infrastructure.runtime import RuntimeManager
from backend.app.infrastructure.site_reliability import SiteReliabilityRegistry
from backend.app.versioning import app_version

_request_logger = logging.getLogger("packbreaker.http")
_recovery_logger = logging.getLogger("packbreaker.recovery")
_QUIET_SUCCESSFUL_GET_PATHS = frozenset(
    {
        "/api/v1/health/live",
        "/api/v1/health/ready",
        "/api/v1/notifications/inbox/unread-count",
        "/api/v1/auth/me",
    }
)

_HTTP_EXACT_RESOURCE_LABELS: dict[str, str] = {
    "/": "管理页面",
    "/api/v1/notification-channels": "通知渠道列表",
    "/api/v1/notifications/inbox/unread-count": "站内通知未读数量",
    "/api/v1/system/health": "系统健康状态",
    "/api/v1/system/logs": "运行日志列表",
    "/api/v1/downloaders": "下载器列表",
    "/api/v1/movie-dedup/jobs": "影片去重任务列表",
    "/api/v1/unpack/definitions": "数据拆包任务列表",
    "/api/v1/unpack/source-scans": "数据拆包目录扫描",
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
    ("/api/v1/movie-dedup", "影片去重"),
    ("/api/v1/unpack", "数据拆包"),
    ("/api/v1/files/tree", "目录选择"),
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


def _should_log_http_request(method: str, path: str, status_code: int, duration_ms: float) -> bool:
    # Suppress high-frequency successful polling only. Errors and unusually
    # slow requests still produce the full trace_id and status diagnostics.
    return not (
        method == "GET"
        and path in _QUIET_SUCCESSFUL_GET_PATHS
        and 200 <= status_code < 300
        and duration_ms < 1000
    )


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
        path_scope = AuthorizedPathScope.from_runtime(
            legacy_data_root=resolved_settings.data_dir,
            config_dir=resolved_settings.config_dir,
        )
        app.state.authorized_path_scope = path_scope
        unpack_source_scan_service = UnpackSourceScanService(
            resolved_runtime.session_factory,
            path_scope=path_scope,
        )
        app.state.unpack_source_scan_service = unpack_source_scan_service
        app.state.unpack_definition_service = UnpackDefinitionService(
            resolved_runtime.session_factory,
            data_root=resolved_settings.data_dir,
            timezone=resolved_settings.timezone,
            path_scope=path_scope,
        )
        unpack_discovery_service = UnpackDiscoveryService(
            resolved_runtime.session_factory,
            path_scope=path_scope,
        )
        app.state.unpack_discovery_service = unpack_discovery_service
        app.state.unpack_execution_query_service = UnpackExecutionQueryService(
            resolved_runtime.session_factory
        )
        app.state.unpack_item_action_service = UnpackItemActionService(
            resolved_runtime.session_factory
        )
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
        movie_dedup_service = MovieDedupService(
            resolved_runtime.session_factory,
            data_root=resolved_settings.data_dir,
            path_scope=path_scope,
        )
        app.state.movie_dedup_service = movie_dedup_service
        downloader_service = DownloaderService(
            resolved_runtime.session_factory,
            secret_store,
            data_root=resolved_settings.data_dir,
        )
        app.state.downloader_service = downloader_service
        unpack_monitor_service = UnpackMonitorService(
            resolved_runtime.session_factory,
            downloader_service,
        )
        app.state.unpack_monitor_service = unpack_monitor_service
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
        unpack_match_coordinator = UnpackMatchCoordinator(
            resolved_runtime.session_factory,
            site_service,
            max_site_concurrency=resolved_settings.unpack_match_site_concurrency,
            max_queries_per_site=resolved_settings.unpack_match_max_queries_per_site,
            site_timeout_seconds=resolved_settings.unpack_match_site_timeout_seconds,
            max_candidates_per_item=resolved_settings.unpack_match_candidate_limit,
        )
        app.state.unpack_match_coordinator = unpack_match_coordinator
        unpack_content_verification_service = UnpackContentVerificationService(
            resolved_runtime.session_factory,
            site_service,
            path_scope=path_scope,
            fetch_timeout_seconds=resolved_settings.unpack_content_fetch_timeout_seconds,
        )
        app.state.unpack_content_verification_service = unpack_content_verification_service
        unpack_auxiliary_service = UnpackAuxiliaryStagingService(
            resolved_runtime.session_factory,
            site_service,
            downloader_service,
            path_scope=path_scope,
            fetch_timeout_seconds=resolved_settings.unpack_auxiliary_fetch_timeout_seconds,
        )
        app.state.unpack_auxiliary_service = unpack_auxiliary_service
        unpack_execution_plan_service = UnpackExecutionPlanService(
            resolved_runtime.session_factory,
            site_service,
            downloader_service,
            path_scope=path_scope,
            fetch_timeout_seconds=resolved_settings.unpack_content_fetch_timeout_seconds,
        )
        app.state.unpack_execution_plan_service = unpack_execution_plan_service
        unpack_materialization_service = UnpackMaterializationService(
            resolved_runtime.session_factory,
            data_root=resolved_settings.data_dir,
            path_scope=path_scope,
        )
        app.state.unpack_materialization_service = unpack_materialization_service
        unpack_seeding_service = UnpackSeedingService(
            resolved_runtime.session_factory,
            site_service,
            downloader_service,
            fetch_timeout_seconds=resolved_settings.unpack_content_fetch_timeout_seconds,
        )
        app.state.unpack_seeding_service = unpack_seeding_service
        cookiecloud_service = CookieCloudService(
            resolved_runtime.session_factory,
            secret_store,
            site_service=site_service,
        )
        cookiecloud_service.ensure_default()
        app.state.cookiecloud_service = cookiecloud_service
        cookiecloud_driver = CookieCloudDriver(
            cookiecloud_service,
            interval_seconds=resolved_settings.cookiecloud_driver_interval_seconds,
            timezone=resolved_settings.timezone,
        )
        app.state.cookiecloud_driver = cookiecloud_driver
        unpack_driver = UnpackDriver(
            unpack_discovery_service,
            unpack_monitor_service,
            unpack_match_coordinator,
            unpack_content_verification_service,
            unpack_auxiliary_service,
            unpack_execution_plan_service,
            unpack_materialization_service,
            unpack_seeding_service,
            notification_service,
            interval_seconds=resolved_settings.unpack_driver_interval_seconds,
            execution_limit=resolved_settings.unpack_driver_execution_limit,
            discovery_batch_size=resolved_settings.unpack_discovery_batch_size,
            match_batch_size=resolved_settings.unpack_match_batch_size,
            content_verification_batch_size=resolved_settings.unpack_content_verify_batch_size,
            auxiliary_batch_size=resolved_settings.unpack_auxiliary_batch_size,
            execution_plan_batch_size=resolved_settings.unpack_execution_plan_batch_size,
            materialization_batch_size=resolved_settings.unpack_materialization_batch_size,
            seeding_batch_size=resolved_settings.unpack_seeding_batch_size,
        )
        app.state.unpack_driver = unpack_driver
        unpack_driver.start()
        movie_dedup_driver = MovieDedupDriver(
            movie_dedup_service,
            interval_seconds=resolved_settings.movie_dedup_driver_interval_seconds,
            job_limit=resolved_settings.movie_dedup_driver_job_limit,
            scan_batch_size=resolved_settings.movie_dedup_scan_batch_size,
            match_batch_size=resolved_settings.movie_dedup_match_batch_size,
            verify_batch_size=resolved_settings.movie_dedup_verify_batch_size,
        )
        app.state.movie_dedup_driver = movie_dedup_driver
        movie_dedup_driver.start()
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
            unpack_definition_service=app.state.unpack_definition_service,
            unpack_execution_query_service=app.state.unpack_execution_query_service,
            downloader_service=downloader_service,
            site_service=site_service,
            system_upgrade_service=system_upgrade_service,
            unpack_driver=unpack_driver,
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
            approval_service=None,
        )
        app.state.ai_telegram_driver = ai_telegram_driver
        cookiecloud_driver.start()
        backup_driver.start()
        ai_telegram_driver.start()
        resource_sampler = SystemResourceSampler()
        app.state.resource_sampler = resource_sampler
        resource_sampler.start()
        try:
            yield
        finally:
            await resource_sampler.stop()
            await ai_telegram_driver.stop()
            await backup_driver.stop()
            await cookiecloud_driver.stop()
            await notification_driver.stop()
            await movie_dedup_driver.stop()
            await unpack_driver.stop()
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
        duration_ms = round((time.perf_counter() - started_at) * 1000, 3)
        if _should_log_http_request(
            request.method, request.url.path, response.status_code, duration_ms
        ):
            _request_logger.info(
                _http_request_message(request.method, request.url.path, response.status_code),
                extra={
                    "fields": {
                        "trace_id": str(trace_id),
                        "method": request.method,
                        "path": request.url.path,
                        "status_code": response.status_code,
                        "client_source": network.client_source,
                        "duration_ms": duration_ms,
                    }
                },
            )
        return response

    app.include_router(ai_agent_router, prefix="/api/v1")
    app.include_router(auth_router, prefix="/api/v1")
    app.include_router(cookiecloud_router, prefix="/api/v1")
    app.include_router(downloader_router, prefix="/api/v1")
    app.include_router(health_router, prefix="/api/v1")
    app.include_router(movie_dedup_router, prefix="/api/v1")
    app.include_router(notification_router, prefix="/api/v1")
    app.include_router(site_router, prefix="/api/v1")
    app.include_router(system_router, prefix="/api/v1")
    app.include_router(unpack_router, prefix="/api/v1")
    _attach_frontend(app, resolved_settings)
    return app


app = create_app()
