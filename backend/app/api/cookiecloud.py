from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, SecretStr

from backend.app.api.dependencies import (
    AccessPrincipal,
    cookiecloud_service,
    require_admin_csrf_principal,
    require_admin_principal,
)
from backend.app.application.cookiecloud import (
    CookieCloudSettingUpdate,
    CookieCloudSettingView,
)
from backend.app.application.errors import ApplicationError
from backend.app.domain.cookiecloud import (
    CookieCloudConnectionStatus,
    CookieCloudCryptoType,
    CookieCloudSyncStatus,
)

router = APIRouter(tags=["cookiecloud"])
CONFIG_READ_ACCESS = require_admin_principal
CONFIG_WRITE_ACCESS = require_admin_csrf_principal


class CookieCloudSettingResponse(BaseModel):
    enabled: bool
    server_url: str
    uuid: str
    password_configured: bool
    auto_sync: bool
    sync_interval_minutes: int
    request_timeout_seconds: int
    connection_status: CookieCloudConnectionStatus
    last_test_at: datetime | None
    last_sync_at: datetime | None
    last_sync_status: CookieCloudSyncStatus
    last_sync_error_code: str | None
    matched_sites: int
    updated_sites: int
    unmatched_domains: int
    version: int
    created_at: datetime
    updated_at: datetime


class CookieCloudSettingUpdateRequest(BaseModel):
    enabled: bool = False
    server_url: str = Field(default="", max_length=2048)
    uuid: str = Field(default="", max_length=255)
    password_action: Literal["KEEP", "SET", "CLEAR"] = "KEEP"
    password: SecretStr | None = Field(default=None, max_length=512)
    auto_sync: bool = True
    sync_interval_minutes: int = Field(default=30, ge=5, le=10080)
    request_timeout_seconds: int = Field(default=15, ge=1, le=120)


class CookieCloudProbeResponse(BaseModel):
    status: Literal["ok"]
    tested_at: datetime
    crypto_type: CookieCloudCryptoType
    domain_count: int
    cookie_count: int
    update_time: str | None
    version: int


class CookieCloudSyncResponse(BaseModel):
    status: Literal["ok"]
    synced_at: datetime
    crypto_type: CookieCloudCryptoType
    matched_sites: int
    updated_sites: int
    unmatched_domains: int
    update_time: str | None
    version: int


def _view(record: CookieCloudSettingView) -> CookieCloudSettingResponse:
    return CookieCloudSettingResponse(
        enabled=record.enabled,
        server_url=record.server_url,
        uuid=record.uuid,
        password_configured=record.password_configured,
        auto_sync=record.auto_sync,
        sync_interval_minutes=record.sync_interval_minutes,
        request_timeout_seconds=record.request_timeout_seconds,
        connection_status=record.connection_status,
        last_test_at=record.last_test_at,
        last_sync_at=record.last_sync_at,
        last_sync_status=record.last_sync_status,
        last_sync_error_code=record.last_sync_error_code,
        matched_sites=record.matched_sites,
        updated_sites=record.updated_sites,
        unmatched_domains=record.unmatched_domains,
        version=record.version,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _expected_version(value: str | None) -> int:
    if value is None:
        raise ApplicationError(
            code="IF_MATCH_REQUIRED",
            status=428,
            title="缺少版本前置条件",
            detail="修改 CookieCloud 配置时必须提供 If-Match 版本",
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
            detail="CookieCloud 配置版本必须大于等于 1",
        )
    return parsed


def _response(record: CookieCloudSettingView) -> JSONResponse:
    return JSONResponse(
        _view(record).model_dump(mode="json"),
        headers={"ETag": f'"{record.version}"', "Cache-Control": "no-store"},
    )


@router.get("/cookiecloud/config", response_model=CookieCloudSettingResponse)
async def get_cookiecloud_config(
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(CONFIG_READ_ACCESS)],
) -> JSONResponse:
    return _response(cookiecloud_service(request).get())


@router.put("/cookiecloud/config", response_model=CookieCloudSettingResponse)
async def update_cookiecloud_config(
    request: Request,
    payload: CookieCloudSettingUpdateRequest,
    _principal: Annotated[AccessPrincipal, Depends(CONFIG_WRITE_ACCESS)],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> JSONResponse:
    if payload.password_action == "CLEAR" and payload.password is not None:
        raise ApplicationError(
            code="COOKIECLOUD_CONFIG_INVALID",
            status=422,
            title="CookieCloud 配置无效",
            detail="清除密码时不能同时提交新密码",
        )
    password = payload.password.get_secret_value() if payload.password is not None else None
    record = cookiecloud_service(request).update(
        CookieCloudSettingUpdate(
            enabled=payload.enabled,
            server_url=payload.server_url,
            uuid=payload.uuid,
            auto_sync=payload.auto_sync,
            sync_interval_minutes=payload.sync_interval_minutes,
            request_timeout_seconds=payload.request_timeout_seconds,
            password_action=payload.password_action,
            password=password,
        ),
        expected_version=_expected_version(if_match),
    )
    return _response(record)


@router.post("/cookiecloud/test", response_model=CookieCloudProbeResponse)
async def test_cookiecloud(
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(CONFIG_WRITE_ACCESS)],
) -> JSONResponse:
    record, probe = await cookiecloud_service(request).probe_saved()
    response = CookieCloudProbeResponse(
        status="ok",
        tested_at=probe.tested_at,
        crypto_type=probe.crypto_type,
        domain_count=probe.domain_count,
        cookie_count=probe.cookie_count,
        update_time=probe.update_time,
        version=record.version,
    )
    return JSONResponse(
        response.model_dump(mode="json"),
        headers={"ETag": f'"{record.version}"', "Cache-Control": "no-store"},
    )


@router.post("/cookiecloud/sync", response_model=CookieCloudSyncResponse)
async def sync_cookiecloud(
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(CONFIG_WRITE_ACCESS)],
) -> JSONResponse:
    record, report = await cookiecloud_service(request).sync_now()
    response = CookieCloudSyncResponse(
        status="ok",
        synced_at=report.synced_at,
        crypto_type=report.crypto_type,
        matched_sites=report.matched_sites,
        updated_sites=report.updated_sites,
        unmatched_domains=report.unmatched_domains,
        update_time=report.update_time,
        version=record.version,
    )
    return JSONResponse(
        response.model_dump(mode="json"),
        headers={"ETag": f'"{record.version}"', "Cache-Control": "no-store"},
    )
