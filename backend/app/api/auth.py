from time import time

from fastapi import APIRouter, Cookie, Header, Request, Response, status
from fastapi.responses import JSONResponse
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


@router.post("/auth/setup", status_code=status.HTTP_201_CREATED)
async def setup(request: Request, payload: PasswordRequest) -> dict[str, bool]:
    auth_service(request).setup(payload.password.get_secret_value())
    return {"configured": True}


@router.post("/auth/login")
async def login(request: Request, payload: PasswordRequest) -> JSONResponse:
    result = auth_service(request).login(
        password=payload.password.get_secret_value(),
        source=client_source(request),
    )
    secure = effective_scheme(request) == "https"
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
    return auth_service(request).me(session_token)


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
