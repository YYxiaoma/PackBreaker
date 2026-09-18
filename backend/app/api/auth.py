from datetime import datetime
from time import time

from fastapi import APIRouter, Cookie, Header, Request, Response, status
from pydantic import BaseModel, Field, SecretStr

from backend.app.api.dependencies import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    auth_service,
    client_source,
    effective_scheme,
)

router = APIRouter(tags=["auth"])


class PasswordRequest(BaseModel):
    password: SecretStr = Field(min_length=12, max_length=256)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: SecretStr = Field(min_length=12, max_length=256)


class ChangePasswordRequest(BaseModel):
    current_password: SecretStr = Field(min_length=12, max_length=256)
    new_password: SecretStr = Field(min_length=12, max_length=256)
    confirm_password: SecretStr = Field(min_length=12, max_length=256)


class SetupResponse(BaseModel):
    configured: bool


class LoginResponse(BaseModel):
    authenticated: bool
    expires_at: datetime
    username: str
    must_change_password: bool


class AuthStatusResponse(BaseModel):
    configured: bool
    authenticated: bool
    permissions: list[str]
    expires_at: datetime | None = None
    username: str | None = None
    must_change_password: bool = False


@router.post("/auth/setup", status_code=status.HTTP_201_CREATED)
async def setup(request: Request, payload: PasswordRequest) -> SetupResponse:
    auth_service(request).setup(payload.password.get_secret_value())
    return SetupResponse(configured=True)


@router.post("/auth/login")
async def login(request: Request, response: Response, payload: LoginRequest) -> LoginResponse:
    result = auth_service(request).login(
        username=payload.username,
        password=payload.password.get_secret_value(),
        source=client_source(request),
    )
    secure = effective_scheme(request) == "https"
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
    return LoginResponse(
        authenticated=True,
        expires_at=result.expires_at,
        username=result.username,
        must_change_password=result.must_change_password,
    )


@router.get("/auth/me")
async def me(
    request: Request,
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE),
) -> AuthStatusResponse:
    current = auth_service(request).me(session_token)
    return AuthStatusResponse(
        configured=current.configured,
        authenticated=current.authenticated,
        permissions=list(current.permissions),
        expires_at=current.expires_at,
        username=current.username,
        must_change_password=current.must_change_password,
    )


@router.post("/auth/password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    request: Request,
    payload: ChangePasswordRequest,
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    csrf_cookie: str | None = Cookie(default=None, alias=CSRF_COOKIE),
    csrf_header: str | None = Header(default=None, alias="X-CSRF-Token"),
) -> Response:
    auth_service(request).change_password(
        token=session_token,
        csrf_cookie=csrf_cookie,
        csrf_header=csrf_header,
        current_password=payload.current_password.get_secret_value(),
        new_password=payload.new_password.get_secret_value(),
        confirmation=payload.confirm_password.get_secret_value(),
    )
    secure = effective_scheme(request) == "https"
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(SESSION_COOKIE, path="/", secure=secure, samesite="strict")
    response.delete_cookie(CSRF_COOKIE, path="/", secure=secure, samesite="strict")
    return response


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    csrf_cookie: str | None = Cookie(default=None, alias=CSRF_COOKIE),
    csrf_header: str | None = Header(default=None, alias="X-CSRF-Token"),
) -> Response:
    auth_service(request).logout(
        token=session_token,
        csrf_cookie=csrf_cookie,
        csrf_header=csrf_header,
    )
    secure = effective_scheme(request) == "https"
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(SESSION_COOKIE, path="/", secure=secure, samesite="strict")
    response.delete_cookie(CSRF_COOKIE, path="/", secure=secure, samesite="strict")
    return response
