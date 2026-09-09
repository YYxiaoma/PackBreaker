from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.responses import Response

from backend.app.api.auth import router as auth_router
from backend.app.api.automation_access import router as api_token_router
from backend.app.api.downloaders import router as downloader_router
from backend.app.api.health import router as health_router
from backend.app.api.system import router as system_router
from backend.app.application.auth import AuthService
from backend.app.application.automation_access import ApiTokenService
from backend.app.application.downloaders import DownloaderService
from backend.app.application.errors import ApplicationError
from backend.app.application.secrets import SecretStore
from backend.app.config import AppSettings
from backend.app.infrastructure.http_security import TrustedProxyPolicy, apply_security_headers
from backend.app.infrastructure.runtime import RuntimeManager


def _trace_id(value: str | None) -> UUID:
    if value:
        try:
            return UUID(value)
        except ValueError:
            return uuid4()
    return uuid4()


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
        app.state.downloader_service = DownloaderService(
            resolved_runtime.session_factory,
            secret_store,
            data_root=resolved_settings.data_dir,
        )
        try:
            yield
        finally:
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
        response = await call_next(request)
        response.headers["X-Trace-Id"] = str(trace_id)
        apply_security_headers(
            path=request.url.path, scheme=network.scheme, headers=response.headers
        )
        return response

    app.include_router(api_token_router, prefix="/api/v1")
    app.include_router(auth_router, prefix="/api/v1")
    app.include_router(downloader_router, prefix="/api/v1")
    app.include_router(health_router, prefix="/api/v1")
    app.include_router(system_router, prefix="/api/v1")
    return app


app = create_app()
