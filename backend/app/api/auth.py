from time import time
from typing import cast

from fastapi import APIRouter, Cookie, Header, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, SecretStr

from backend.app.application.auth import AuthService

SESSION_COOKIE = "packbreaker_session"
CSRF_COOKIE = "packbreaker_csrf"

router = APIRouter(tags=["auth"])


class PasswordRequest(BaseModel):
    password: SecretStr = Field(min_length=12, max_length=256)


def _auth_service(request: Request) -> AuthService:
    return cast(AuthService, request.app.state.auth_service)


def _client_source(request: Request) -> str:
    return request.client.host if request.client is not None else "unknown"


def _secure_cookie(request: Request) -> bool:
    return request.url.scheme == "https"


@router.post("/auth/setup", status_code=status.HTTP_201_CREATED)
async def setup(request: Request, payload: PasswordRequest) -> dict[str, bool]:
    _auth_service(request).setup(payload.password.get_secret_value())
    return {"configured": True}


@router.post("/auth/login")
async def login(request: Request, payload: PasswordRequest) -> JSONResponse:
    result = _auth_service(request).login(
        password=payload.password.get_secret_value(),
        source=_client_source(request),
    )
    secure = _secure_cookie(request)
    response = JSONResponse(
        {
            "authenticated": True,
            "expires_at": result.expires_at.isoformat().replace("+00:00", "Z"),
        }
    )
    max_age = max(1, int(result.expires_at.timestamp() - time()))
    response.set_cookie(
        SESSION_COOKIE,
        result.token,
        max_age=max_age,
        httponly=True,
        secure=secure,
        samesite="strict",
        path="/",
    )
    response.set_cookie(
        CSRF_COOKIE,
        result.csrf_token,
        max_age=max_age,
        httponly=False,
        secure=secure,
        samesite="strict",
        path="/",
    )
    return response


@router.get("/auth/me")
async def me(
    request: Request,
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE),
) -> dict[str, object]:
    return _auth_service(request).me(session_token)


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    csrf_cookie: str | None = Cookie(default=None, alias=CSRF_COOKIE),
    csrf_header: str | None = Header(default=None, alias="X-CSRF-Token"),
) -> Response:
    _auth_service(request).logout(
        token=session_token,
        csrf_cookie=csrf_cookie,
        csrf_header=csrf_header,
    )
    secure = _secure_cookie(request)
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(SESSION_COOKIE, path="/", secure=secure, samesite="strict")
    response.delete_cookie(CSRF_COOKIE, path="/", secure=secure, samesite="strict")
    return response
