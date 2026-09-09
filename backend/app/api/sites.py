from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, SecretStr

from backend.app.api.dependencies import (
    AccessPrincipal,
    require_admin_csrf_or_scope,
    require_admin_or_scope,
    site_service,
)
from backend.app.application.errors import ApplicationError
from backend.app.application.sites import SiteUpdate, SiteView
from backend.app.domain.auth import ApiScope
from backend.app.domain.site_config import SiteKind, SiteProbeStatus

router = APIRouter(tags=["sites"])
CONFIG_READ_ACCESS = require_admin_or_scope(ApiScope.CONFIG_READ)
CONFIG_WRITE_ACCESS = require_admin_csrf_or_scope(ApiScope.CONFIG_WRITE)


class SiteViewResponse(BaseModel):
    id: str
    name: str
    type: SiteKind
    base_url: str
    api_key_configured: bool
    capabilities: dict[str, Any]
    connection_status: SiteProbeStatus
    enabled: bool
    version: int
    last_test_at: datetime | None
    created_at: datetime
    updated_at: datetime


class SiteListResponse(BaseModel):
    items: list[SiteViewResponse]


class SiteCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    type: SiteKind
    base_url: str = Field(min_length=1, max_length=2048)
    api_key: SecretStr | None = Field(default=None, min_length=1, max_length=512)


class SitePatchRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    type: SiteKind | None = None
    base_url: str | None = Field(default=None, min_length=1, max_length=2048)
    api_key: SecretStr | None = Field(default=None, min_length=1, max_length=512)
    clear_api_key: bool = False


class SiteActionRequest(BaseModel):
    action: Literal["enable", "disable", "refresh_capabilities"]


class SiteProbeResponse(BaseModel):
    status: Literal["ok"]
    capabilities: dict[str, Any]


def _view(record: SiteView) -> SiteViewResponse:
    return SiteViewResponse(
        id=record.id,
        name=record.name,
        type=record.type,
        base_url=record.base_url,
        api_key_configured=record.api_key_configured,
        capabilities=record.capabilities,
        connection_status=record.connection_status,
        enabled=record.enabled,
        version=record.version,
        last_test_at=record.last_test_at,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _expected_version(value: str | None) -> int:
    if value is None:
        raise ApplicationError(
            code="IF_MATCH_REQUIRED",
            status=428,
            title="缺少版本前置条件",
            detail="修改或删除站点时必须提供 If-Match 版本",
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
            detail="站点版本必须大于等于 1",
        )
    return parsed


def _json_with_etag(record: SiteView, *, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        _view(record).model_dump(mode="json"),
        status_code=status_code,
        headers={"ETag": f'"{record.version}"'},
    )


@router.get("/sites", response_model=SiteListResponse)
async def list_sites(
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(CONFIG_READ_ACCESS)],
) -> SiteListResponse:
    return SiteListResponse(items=[_view(item) for item in site_service(request).list_sites()])


@router.post("/sites", response_model=SiteViewResponse, status_code=status.HTTP_201_CREATED)
async def create_site(
    request: Request,
    payload: SiteCreateRequest,
    _principal: Annotated[AccessPrincipal, Depends(CONFIG_WRITE_ACCESS)],
) -> JSONResponse:
    created = site_service(request).create(
        name=payload.name,
        kind=payload.type,
        base_url=payload.base_url,
        api_key=payload.api_key.get_secret_value() if payload.api_key is not None else None,
    )
    return _json_with_etag(created, status_code=status.HTTP_201_CREATED)


@router.get("/sites/{site_id}", response_model=SiteViewResponse)
async def get_site(
    site_id: str,
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(CONFIG_READ_ACCESS)],
) -> JSONResponse:
    return _json_with_etag(site_service(request).get(site_id))


@router.patch("/sites/{site_id}", response_model=SiteViewResponse)
async def patch_site(
    site_id: str,
    request: Request,
    payload: SitePatchRequest,
    _principal: Annotated[AccessPrincipal, Depends(CONFIG_WRITE_ACCESS)],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> JSONResponse:
    if payload.clear_api_key and payload.api_key is not None:
        raise ApplicationError(
            code="SITE_API_KEY_INVALID",
            status=422,
            title="站点 API Key 操作冲突",
            detail="api_key 与 clear_api_key 不能同时使用",
        )
    action: Literal["KEEP", "SET", "CLEAR"] = "KEEP"
    if payload.clear_api_key:
        action = "CLEAR"
    elif "api_key" in payload.model_fields_set and payload.api_key is not None:
        action = "SET"
    updated = site_service(request).update(
        site_id,
        expected_version=_expected_version(if_match),
        update_request=SiteUpdate(
            name=payload.name,
            type=payload.type,
            base_url=payload.base_url,
            api_key_action=action,
            api_key=payload.api_key.get_secret_value() if payload.api_key is not None else None,
        ),
    )
    return _json_with_etag(updated)


@router.delete("/sites/{site_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_site(
    site_id: str,
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(CONFIG_WRITE_ACCESS)],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Response:
    site_service(request).delete(site_id, expected_version=_expected_version(if_match))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/sites/{site_id}/test", response_model=SiteProbeResponse)
async def test_site(
    site_id: str,
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(CONFIG_WRITE_ACCESS)],
) -> dict[str, object]:
    return await site_service(request).test_connection(site_id)


@router.post("/sites/{site_id}/actions")
async def site_action(
    site_id: str,
    request: Request,
    payload: SiteActionRequest,
    _principal: Annotated[AccessPrincipal, Depends(CONFIG_WRITE_ACCESS)],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Response:
    if payload.action == "refresh_capabilities":
        result = await site_service(request).test_connection(site_id)
        return JSONResponse(result)
    record = site_service(request).set_enabled(
        site_id,
        expected_version=_expected_version(if_match),
        enabled=payload.action == "enable",
    )
    return _json_with_etag(record)
