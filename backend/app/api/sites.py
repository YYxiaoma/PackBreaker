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
from backend.app.domain.site_config import SiteCredentialKind, SiteKind, SiteProbeStatus
from backend.app.infrastructure.site_reliability import SiteReliabilityHealth

router = APIRouter(tags=["sites"])
CONFIG_READ_ACCESS = require_admin_or_scope(ApiScope.CONFIG_READ)
CONFIG_WRITE_ACCESS = require_admin_csrf_or_scope(ApiScope.CONFIG_WRITE)


class SiteViewResponse(BaseModel):
    id: str
    name: str
    type: SiteKind
    base_url: str
    credential_kind: SiteCredentialKind
    credential_configured: bool
    capabilities: dict[str, Any]
    connection_status: SiteProbeStatus
    enabled: bool
    version: int
    last_test_at: datetime | None
    created_at: datetime
    updated_at: datetime


class SiteListResponse(BaseModel):
    items: list[SiteViewResponse]


class SiteCredentialInput(BaseModel):
    kind: SiteCredentialKind
    value: SecretStr = Field(min_length=1, max_length=8192)


class SiteCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    type: SiteKind
    base_url: str = Field(min_length=1, max_length=2048)
    credential: SiteCredentialInput | None = None


class SitePatchRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    type: SiteKind | None = None
    base_url: str | None = Field(default=None, min_length=1, max_length=2048)
    credential: SiteCredentialInput | None = None
    clear_credential: bool = False


class SiteActionRequest(BaseModel):
    action: Literal["enable", "disable", "refresh_capabilities", "reset_circuit"]


class SiteProbeResponse(BaseModel):
    status: Literal["ok"]
    capabilities: dict[str, Any]


class SiteHealthResponse(BaseModel):
    config_version: int
    circuit_state: Literal["CLOSED", "OPEN", "HALF_OPEN"]
    failure_count: int
    retry_after_seconds: float | None
    half_open_probe_in_flight: bool
    rate_limit_wait_seconds: float
    cache_entries: int
    cache_hits: int
    cache_misses: int
    cache_evictions: int
    requests_started: int
    requests_succeeded: int
    requests_failed: int
    retries_scheduled: int
    last_error_code: str | None


def _view(record: SiteView) -> SiteViewResponse:
    return SiteViewResponse(
        id=record.id,
        name=record.name,
        type=record.type,
        base_url=record.base_url,
        credential_kind=record.credential_kind,
        credential_configured=record.credential_configured,
        capabilities=record.capabilities,
        connection_status=record.connection_status,
        enabled=record.enabled,
        version=record.version,
        last_test_at=record.last_test_at,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _health(record: SiteReliabilityHealth) -> SiteHealthResponse:
    return SiteHealthResponse(
        config_version=record.config_version,
        circuit_state=record.circuit_state,
        failure_count=record.failure_count,
        retry_after_seconds=record.retry_after_seconds,
        half_open_probe_in_flight=record.half_open_probe_in_flight,
        rate_limit_wait_seconds=record.rate_limit_wait_seconds,
        cache_entries=record.cache_entries,
        cache_hits=record.cache_hits,
        cache_misses=record.cache_misses,
        cache_evictions=record.cache_evictions,
        requests_started=record.requests_started,
        requests_succeeded=record.requests_succeeded,
        requests_failed=record.requests_failed,
        retries_scheduled=record.retries_scheduled,
        last_error_code=record.last_error_code,
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
        credential_kind=payload.credential.kind if payload.credential is not None else None,
        credential=(
            payload.credential.value.get_secret_value() if payload.credential is not None else None
        ),
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
    if payload.clear_credential and payload.credential is not None:
        raise ApplicationError(
            code="SITE_CREDENTIAL_INVALID",
            status=422,
            title="站点凭证操作冲突",
            detail="credential 与 clear_credential 不能同时使用",
        )
    action: Literal["KEEP", "SET", "CLEAR"] = "KEEP"
    if payload.clear_credential:
        action = "CLEAR"
    elif "credential" in payload.model_fields_set and payload.credential is not None:
        action = "SET"
    updated = site_service(request).update(
        site_id,
        expected_version=_expected_version(if_match),
        update_request=SiteUpdate(
            name=payload.name,
            type=payload.type,
            base_url=payload.base_url,
            credential_action=action,
            credential_kind=payload.credential.kind if payload.credential is not None else None,
            credential=(
                payload.credential.value.get_secret_value()
                if payload.credential is not None
                else None
            ),
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


@router.get("/sites/{site_id}/health", response_model=SiteHealthResponse)
async def get_site_health(
    site_id: str,
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(CONFIG_READ_ACCESS)],
) -> SiteHealthResponse:
    return _health(await site_service(request).health(site_id))


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
    if payload.action == "reset_circuit":
        health = await site_service(request).reset_circuit(
            site_id,
            expected_version=_expected_version(if_match),
        )
        return JSONResponse(
            _health(health).model_dump(mode="json"),
            headers={"ETag": f'"{health.config_version}"'},
        )
    record = site_service(request).set_enabled(
        site_id,
        expected_version=_expected_version(if_match),
        enabled=payload.action == "enable",
    )
    return _json_with_etag(record)
