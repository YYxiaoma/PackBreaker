from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, cast

from fastapi import Cookie, Header, Request

from backend.app.application.auth import AuthIdentity, AuthService
from backend.app.application.automation_access import ApiTokenService
from backend.app.application.downloaders import DownloaderService
from backend.app.domain.auth import ApiScope

SESSION_COOKIE = "packbreaker_session"
CSRF_COOKIE = "packbreaker_csrf"


@dataclass(frozen=True, slots=True)
class AccessPrincipal:
    kind: Literal["admin_session", "api_token"]
    subject_id: str
    scopes: frozenset[ApiScope]


def auth_service(request: Request) -> AuthService:
    return cast(AuthService, request.app.state.auth_service)


def api_token_service(request: Request) -> ApiTokenService:
    return cast(ApiTokenService, request.app.state.api_token_service)


def downloader_service(request: Request) -> DownloaderService:
    return cast(DownloaderService, request.app.state.downloader_service)


def client_source(request: Request) -> str:
    return cast(str, getattr(request.state, "client_source", "unknown"))


def effective_scheme(request: Request) -> str:
    return cast(str, getattr(request.state, "effective_scheme", request.url.scheme))


async def require_admin_session(
    request: Request,
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE),
) -> AuthIdentity:
    return auth_service(request).require_session(session_token)


async def require_admin_csrf(
    request: Request,
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    csrf_cookie: str | None = Cookie(default=None, alias=CSRF_COOKIE),
    csrf_header: str | None = Header(default=None, alias="X-CSRF-Token"),
) -> AuthIdentity:
    return auth_service(request).require_csrf(
        token=session_token,
        csrf_cookie=csrf_cookie,
        csrf_header=csrf_header,
    )


def require_admin_or_scope(
    scope: ApiScope,
) -> Callable[..., Awaitable[AccessPrincipal]]:
    async def dependency(
        request: Request,
        authorization: str | None = Header(default=None, alias="Authorization"),
        session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    ) -> AccessPrincipal:
        if authorization is not None:
            scheme, separator, credentials = authorization.partition(" ")
            if separator != " " or scheme.lower() != "bearer" or not credentials:
                from backend.app.application.auth import AuthError

                raise AuthError(
                    code="API_TOKEN_INVALID",
                    status=401,
                    title="API Token 无效",
                    detail="Authorization 必须使用 Bearer Token",
                )
            identity = api_token_service(request).authenticate(credentials, scope)
            return AccessPrincipal("api_token", identity.token_id, identity.scopes)

        admin = auth_service(request).require_session(session_token)
        return AccessPrincipal("admin_session", admin.session_id, frozenset(ApiScope))

    return dependency


def require_admin_csrf_or_scope(
    scope: ApiScope,
) -> Callable[..., Awaitable[AccessPrincipal]]:
    async def dependency(
        request: Request,
        authorization: str | None = Header(default=None, alias="Authorization"),
        session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE),
        csrf_cookie: str | None = Cookie(default=None, alias=CSRF_COOKIE),
        csrf_header: str | None = Header(default=None, alias="X-CSRF-Token"),
    ) -> AccessPrincipal:
        if authorization is not None:
            scheme, separator, credentials = authorization.partition(" ")
            if separator != " " or scheme.lower() != "bearer" or not credentials:
                from backend.app.application.auth import AuthError

                raise AuthError(
                    code="API_TOKEN_INVALID",
                    status=401,
                    title="API Token 无效",
                    detail="Authorization 必须使用 Bearer Token",
                )
            identity = api_token_service(request).authenticate(credentials, scope)
            return AccessPrincipal("api_token", identity.token_id, identity.scopes)

        admin = auth_service(request).require_csrf(
            token=session_token,
            csrf_cookie=csrf_cookie,
            csrf_header=csrf_header,
        )
        return AccessPrincipal("admin_session", admin.session_id, frozenset(ApiScope))

    return dependency
