import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response

from backend.app.api.auth import router as auth_router
from backend.app.api.automation_access import router as api_token_router
from backend.app.api.downloaders import router as downloader_router
from backend.app.api.health import router as health_router
from backend.app.api.sites import router as site_router
from backend.app.api.system import router as system_router
from backend.app.api.tasks import router as task_router
from backend.app.application.auth import AuthService
from backend.app.application.automation_access import ApiTokenService
from backend.app.application.downloader_operations import (
    QbittorrentAddOperationService,
    QbittorrentRecheckOperationService,
    QbittorrentRemoveOperationService,
    QbittorrentStartOperationService,
)
from backend.app.application.downloaders import DownloaderService
from backend.app.application.errors import ApplicationError
from backend.app.application.filesystem_operations import FilesystemOperationService
from backend.app.application.secrets import SecretStore
from backend.app.application.sites import SiteService
from backend.app.application.task_actions import TaskActionService
from backend.app.application.task_adding import TaskAddingCoordinator
from backend.app.application.task_cancellation import TaskCancellationCoordinator
from backend.app.application.task_client_verification import TaskClientVerificationCoordinator
from backend.app.application.task_driver import ActiveTaskDriver
from backend.app.application.task_events import TaskEventService
from backend.app.application.task_linking import TaskLinkingCoordinator
from backend.app.application.task_recovery import TaskRecoveryCoordinator
from backend.app.application.task_seeding import TaskSeedingCoordinator
from backend.app.application.tasks import TaskAnalysisService
from backend.app.config import AppSettings
from backend.app.infrastructure.http_security import TrustedProxyPolicy, apply_security_headers
from backend.app.infrastructure.runtime import RuntimeManager
from backend.app.infrastructure.safe_filesystem import SafeFilesystemGateway

_request_logger = logging.getLogger("packbreaker.http")
_recovery_logger = logging.getLogger("packbreaker.recovery")


def _trace_id(value: str | None) -> UUID:
    if value:
        try:
            return UUID(value)
        except ValueError:
            return uuid4()
    return uuid4()


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
        app.state.api_token_service = ApiTokenService(resolved_runtime.session_factory)
        secret_store = SecretStore(
            resolved_runtime.session_factory,
            resolved_runtime.secret_cipher,
        )
        app.state.secret_store = secret_store
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
        qbit_start_operations = QbittorrentStartOperationService(resolved_runtime.session_factory)
        app.state.qbittorrent_start_operation_service = qbit_start_operations
        qbit_remove_operations = QbittorrentRemoveOperationService(resolved_runtime.session_factory)
        app.state.qbittorrent_remove_operation_service = qbit_remove_operations
        site_service = SiteService(resolved_runtime.session_factory, secret_store)
        app.state.site_service = site_service
        task_analysis_service = TaskAnalysisService(
            resolved_runtime.session_factory,
            site_service,
            data_root=resolved_settings.data_dir,
        )
        app.state.task_analysis_service = task_analysis_service
        app.state.task_event_service = TaskEventService(resolved_runtime.session_factory)
        filesystem_operations = FilesystemOperationService(
            resolved_runtime.session_factory,
            SafeFilesystemGateway(resolved_settings.data_dir),
        )
        app.state.filesystem_operation_service = filesystem_operations
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
            data_root=resolved_settings.data_dir,
        )
        app.state.task_adding_coordinator = task_adding_coordinator
        task_client_verification_coordinator = TaskClientVerificationCoordinator(
            resolved_runtime.session_factory,
            downloader_service,
            qbit_recheck_operations,
            data_root=resolved_settings.data_dir,
        )
        app.state.task_client_verification_coordinator = task_client_verification_coordinator
        task_seeding_coordinator = TaskSeedingCoordinator(
            resolved_runtime.session_factory,
            downloader_service,
            qbit_start_operations,
            data_root=resolved_settings.data_dir,
        )
        app.state.task_seeding_coordinator = task_seeding_coordinator
        task_cancellation_coordinator = TaskCancellationCoordinator(
            resolved_runtime.session_factory,
            downloader_service,
            qbit_remove_operations,
            filesystem_operations,
        )
        app.state.task_cancellation_coordinator = task_cancellation_coordinator
        app.state.task_action_service = TaskActionService(
            resolved_runtime.session_factory,
            task_linking_coordinator,
            task_cancellation_coordinator,
        )
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
            recovery_report = await task_recovery_coordinator.reconcile_once()
        except BaseException:
            resolved_runtime.stop()
            raise
        app.state.task_recovery_report = recovery_report
        if recovery_report.blocked_count:
            _recovery_logger.warning(
                "startup recovery blocked tasks=%s scanned=%s truncated=%s",
                recovery_report.blocked_count,
                recovery_report.scanned_count,
                recovery_report.truncated,
            )
        elif recovery_report.scanned_count:
            _recovery_logger.info(
                "startup recovery scanned=%s completed=%s waiting=%s truncated=%s",
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
        try:
            yield
        finally:
            await task_driver.stop()
            resolved_runtime.stop()

    app = FastAPI(
        title="PackBreaker",
        version="0.1.0",
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
                "request.failed",
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
            "request.complete",
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

    app.include_router(api_token_router, prefix="/api/v1")
    app.include_router(auth_router, prefix="/api/v1")
    app.include_router(downloader_router, prefix="/api/v1")
    app.include_router(health_router, prefix="/api/v1")
    app.include_router(site_router, prefix="/api/v1")
    app.include_router(system_router, prefix="/api/v1")
    app.include_router(task_router, prefix="/api/v1")
    _attach_frontend(app, resolved_settings)
    return app


app = create_app()
