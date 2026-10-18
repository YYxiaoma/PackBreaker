from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from backend.app.api.dependencies import (
    AccessPrincipal,
    require_admin_csrf_principal,
    require_admin_principal,
    site_service,
)
from backend.app.application.errors import ApplicationError
from backend.app.application.sites import SiteUpdate, SiteView
from backend.app.domain.site_config import (
    SiteCredentialKind,
    SiteKind,
    SiteProbeStatus,
    SiteSupportStatus,
    site_profile,
    site_profiles,
)
from backend.app.infrastructure.site_reliability import SiteReliabilityHealth

router = APIRouter(tags=["sites"])
CONFIG_READ_ACCESS = require_admin_principal
CONFIG_WRITE_ACCESS = require_admin_csrf_principal


class SiteUserSummaryResponse(BaseModel):
    username: str | None
    user_level: str | None
    uploaded_bytes: int | None
    downloaded_bytes: int | None
    ratio: float | None
    seeding_count: int | None
    fetched_at: datetime | None


class SiteViewResponse(BaseModel):
    id: str
    name: str
    user_summary: SiteUserSummaryResponse | None = None
    type: SiteKind
    base_url: str
    credential_kind: SiteCredentialKind
    credential_configured: bool
    download_credential_configured: bool
    request_timeout_seconds: int
    search_interval_seconds: int
    user_agent: str | None
    browser_emulation_enabled: bool
    proxy_enabled: bool
    proxy_host: str | None
    proxy_port: int | None
    proxy_username: str | None
    proxy_credential_configured: bool
    capabilities: dict[str, Any]
    support_status: SiteSupportStatus
    connection_status: SiteProbeStatus
    enabled: bool
    version: int
    last_test_at: datetime | None
    created_at: datetime
    updated_at: datetime


class SiteListResponse(BaseModel):
    items: list[SiteViewResponse]


class SiteCredentialInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: SiteCredentialKind
    value: SecretStr = Field(min_length=1, max_length=8192)


class SiteProxyCreateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    host: str | None = Field(default=None, min_length=1, max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    username: str | None = Field(default=None, min_length=1, max_length=255)
    password: SecretStr | None = Field(default=None, min_length=1, max_length=512)


class SiteProxyPatchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    host: str | None = Field(default=None, min_length=1, max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    username: str | None = Field(default=None, min_length=1, max_length=255)
    password: SecretStr | None = Field(default=None, min_length=1, max_length=512)
    clear_password: bool = False


class SiteCreateRequest(BaseModel):
    # v0.1.5 客户端仍可能提交 base_url。v0.1.6 不再暴露该字段，并忽略旧值，
    # 实际目标地址始终来自受信任 SiteProfileRegistry。
    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=1, max_length=80)
    type: SiteKind
    credential: SiteCredentialInput | None = None
    download_cookie: SecretStr | None = Field(default=None, min_length=1, max_length=8192)
    # Missing/null means use the trusted profile default for this site kind.
    request_timeout_seconds: int | None = Field(default=None, ge=1, le=120)
    search_interval_seconds: int = Field(default=0, ge=0, le=3600)
    user_agent: str | None = Field(default=None, max_length=512)
    browser_emulation_enabled: bool = False
    proxy: SiteProxyCreateInput = Field(default_factory=SiteProxyCreateInput)


class SitePatchRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str | None = Field(default=None, min_length=1, max_length=80)
    type: SiteKind | None = None
    credential: SiteCredentialInput | None = None
    clear_credential: bool = False
    download_cookie: SecretStr | None = Field(default=None, min_length=1, max_length=8192)
    clear_download_cookie: bool = False
    request_timeout_seconds: int | None = Field(default=None, ge=1, le=120)
    search_interval_seconds: int | None = Field(default=None, ge=0, le=3600)
    user_agent: str | None = Field(default=None, max_length=512)
    browser_emulation_enabled: bool | None = None
    proxy: SiteProxyPatchInput | None = None


class SiteTemporaryProbeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: SiteKind
    credential: SiteCredentialInput
    download_cookie: SecretStr | None = Field(default=None, min_length=1, max_length=8192)
    # Missing/null means use the trusted profile default for this site kind.
    request_timeout_seconds: int | None = Field(default=None, ge=1, le=120)
    search_interval_seconds: int = Field(default=0, ge=0, le=3600)
    user_agent: str | None = Field(default=None, max_length=512)
    browser_emulation_enabled: bool = False
    proxy: SiteProxyCreateInput = Field(default_factory=SiteProxyCreateInput)


class SiteProfileResponse(BaseModel):
    kind: SiteKind
    display_name: str
    base_url: str
    credential_kind: SiteCredentialKind
    request_timeout_seconds: int
    search_interval_seconds: float
    supports_user_agent: bool
    supports_browser_emulation: bool
    supports_proxy: bool
    support_status: SiteSupportStatus


class SiteProfileListResponse(BaseModel):
    items: list[SiteProfileResponse]


class SiteActionRequest(BaseModel):
    action: Literal["enable", "disable", "refresh_capabilities"]


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


class SiteUserProfileResponse(BaseModel):
    uid: str | None
    username: str | None
    user_level: str | None
    join_time: datetime | None
    last_access_at: datetime | None
    hnr_status: str | None
    hnr_count: int | None
    message_count: int | None
    remark: str | None


class SiteUserStatisticsResponse(BaseModel):
    real_uploaded_bytes: int | None
    real_downloaded_bytes: int | None
    uploaded_bytes: int | None
    downloaded_bytes: int | None
    ratio: float | None
    torrents_posted: int | None
    seeding_count: int | None
    seeding_size_bytes: int | None
    bonus: float | None
    seeding_points: float | None
    bonus_per_hour: float | None


class SiteDetailConnectionResponse(BaseModel):
    credential_configured: bool
    credential_kind: SiteCredentialKind
    last_test_at: datetime | None
    last_sync_at: datetime | None
    last_sync_duration_ms: int | None
    circuit_state: Literal["CLOSED", "OPEN", "HALF_OPEN"]
    last_error_code: str | None


class SiteDetailCapabilitiesResponse(BaseModel):
    search: bool
    torrent_detail: bool
    torrent_download: bool
    user_info: bool
    seeding_info: bool
    api: bool
    cookie: bool
    browser_emulation: bool
    proxy: bool


class SiteDetailResponse(BaseModel):
    site: SiteViewResponse
    account: SiteUserProfileResponse
    statistics: SiteUserStatisticsResponse
    connection: SiteDetailConnectionResponse
    support_status: SiteSupportStatus
    site_version: str | None
    config_version: int
    capabilities: SiteDetailCapabilitiesResponse


class SiteSyncHistoryItemResponse(BaseModel):
    id: str
    status: Literal["SUCCESS", "FAILED"]
    started_at: datetime
    finished_at: datetime
    duration_ms: int
    field_count: int
    error_code: str | None
    error_message: str | None


class SiteSyncHistoryResponse(BaseModel):
    items: list[SiteSyncHistoryItemResponse]


class SiteSeedingItemResponse(BaseModel):
    torrent_id: str
    title: str
    size_bytes: int | None
    status: str | None
    seeding_time_seconds: int | None
    last_activity_at: datetime | None


class SiteSeedingResponse(BaseModel):
    supported: bool
    items: list[SiteSeedingItemResponse]
    next_cursor: str | None


def _user_profile_response(profile: Any) -> SiteUserProfileResponse:
    return SiteUserProfileResponse(
        uid=profile.uid,
        username=profile.username,
        user_level=profile.user_level,
        join_time=profile.join_time,
        last_access_at=profile.last_access_at,
        hnr_status=profile.hnr_status,
        hnr_count=profile.hnr_count,
        message_count=profile.message_count,
        remark=profile.remark,
    )


def _user_statistics_response(profile: Any) -> SiteUserStatisticsResponse:
    return SiteUserStatisticsResponse(
        real_uploaded_bytes=profile.real_uploaded_bytes,
        real_downloaded_bytes=profile.real_downloaded_bytes,
        uploaded_bytes=profile.uploaded_bytes,
        downloaded_bytes=profile.downloaded_bytes,
        ratio=profile.ratio,
        torrents_posted=profile.torrents_posted,
        seeding_count=profile.seeding_count,
        seeding_size_bytes=profile.seeding_size_bytes,
        bonus=profile.bonus,
        seeding_points=profile.seeding_points,
        bonus_per_hour=profile.bonus_per_hour,
    )


async def _site_detail_response(request: Request, site_id: str) -> SiteDetailResponse:
    service = site_service(request)
    site = service.get(site_id)
    profile = service.get_user_profile(site_id)
    history = service.list_user_profile_sync_history(site_id, limit=1)
    health = _health(await service.health(site_id))
    last_sync_duration_ms = (
        history[0]["duration_ms"]
        if history and isinstance(history[0]["duration_ms"], int)
        else None
    )
    empty_account = SiteUserProfileResponse(
        uid=None,
        username=None,
        user_level=None,
        join_time=None,
        last_access_at=None,
        hnr_status=None,
        hnr_count=None,
        message_count=None,
        remark=None,
    )
    empty_statistics = SiteUserStatisticsResponse(
        real_uploaded_bytes=None,
        real_downloaded_bytes=None,
        uploaded_bytes=None,
        downloaded_bytes=None,
        ratio=None,
        torrents_posted=None,
        seeding_count=None,
        seeding_size_bytes=None,
        bonus=None,
        seeding_points=None,
        bonus_per_hour=None,
    )
    return SiteDetailResponse(
        site=_view(site),
        account=_user_profile_response(profile) if profile is not None else empty_account,
        statistics=_user_statistics_response(profile) if profile is not None else empty_statistics,
        connection=SiteDetailConnectionResponse(
            credential_configured=site.credential_configured,
            credential_kind=site.credential_kind,
            last_test_at=site.last_test_at,
            last_sync_at=profile.fetched_at if profile is not None else None,
            last_sync_duration_ms=last_sync_duration_ms,
            circuit_state=health.circuit_state,
            last_error_code=health.last_error_code,
        ),
        support_status=site.support_status,
        site_version=None,
        config_version=site.version,
        capabilities=SiteDetailCapabilitiesResponse(**service.detail_capabilities(site_id)),
    )


def _view(record: SiteView, profile: Any | None = None) -> SiteViewResponse:
    return SiteViewResponse(
        user_summary=(
            SiteUserSummaryResponse(
                username=profile.username,
                user_level=profile.user_level,
                uploaded_bytes=profile.uploaded_bytes,
                downloaded_bytes=profile.downloaded_bytes,
                ratio=profile.ratio,
                seeding_count=profile.seeding_count,
                fetched_at=profile.fetched_at,
            )
            if profile is not None
            else None
        ),
        id=record.id,
        name=record.name,
        type=record.type,
        base_url=record.base_url,
        credential_kind=record.credential_kind,
        credential_configured=record.credential_configured,
        download_credential_configured=record.download_credential_configured,
        request_timeout_seconds=record.request_timeout_seconds,
        search_interval_seconds=record.search_interval_seconds,
        user_agent=record.user_agent,
        browser_emulation_enabled=record.browser_emulation_enabled,
        proxy_enabled=record.proxy_enabled,
        proxy_host=record.proxy_host,
        proxy_port=record.proxy_port,
        proxy_username=record.proxy_username,
        proxy_credential_configured=record.proxy_credential_configured,
        capabilities=record.capabilities,
        support_status=record.support_status,
        connection_status=record.connection_status,
        enabled=record.enabled,
        version=record.version,
        last_test_at=record.last_test_at,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _runtime_patch(payload: SitePatchRequest) -> dict[str, Any] | None:
    result: dict[str, Any] = {}
    for field_name in (
        "request_timeout_seconds",
        "search_interval_seconds",
        "user_agent",
        "browser_emulation_enabled",
    ):
        if field_name in payload.model_fields_set:
            result[field_name] = getattr(payload, field_name)
    if payload.proxy is not None:
        for field_name in ("enabled", "host", "port", "username"):
            if field_name in payload.proxy.model_fields_set:
                result[f"proxy_{field_name}"] = getattr(payload.proxy, field_name)
    return result or None


def _proxy_password_action(
    proxy: SiteProxyPatchInput | None,
) -> tuple[Literal["KEEP", "SET", "CLEAR"], str | None]:
    if proxy is None:
        return "KEEP", None
    if proxy.clear_password and proxy.password is not None:
        raise ApplicationError(
            code="SITE_PROXY_INVALID",
            status=422,
            title="站点代理配置无效",
            detail="proxy.password 与 proxy.clear_password 不能同时使用",
        )
    if proxy.clear_password:
        return "CLEAR", None
    if "password" in proxy.model_fields_set and proxy.password is not None:
        return "SET", proxy.password.get_secret_value()
    return "KEEP", None


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
    service = site_service(request)
    items = service.list_sites()
    return SiteListResponse(
        items=[_view(item, service.get_user_profile(item.id)) for item in items]
    )


@router.get("/sites/profiles", response_model=SiteProfileListResponse)
async def list_site_profiles(
    _principal: Annotated[AccessPrincipal, Depends(CONFIG_READ_ACCESS)],
) -> SiteProfileListResponse:
    return SiteProfileListResponse(
        items=[
            SiteProfileResponse(
                kind=profile.kind,
                display_name=profile.display_name,
                base_url=profile.base_url,
                credential_kind=profile.credential_kind,
                request_timeout_seconds=profile.request_timeout_seconds,
                search_interval_seconds=profile.search_interval_seconds,
                supports_user_agent=profile.supports_user_agent,
                supports_browser_emulation=profile.supports_browser_emulation,
                supports_proxy=profile.supports_proxy,
                support_status=profile.support_status,
            )
            for profile in site_profiles()
        ]
    )


@router.post("/sites/probe", response_model=SiteProbeResponse)
async def probe_site(
    request: Request,
    payload: SiteTemporaryProbeRequest,
    _principal: Annotated[AccessPrincipal, Depends(CONFIG_WRITE_ACCESS)],
) -> dict[str, object]:
    proxy = payload.proxy
    return await site_service(request).probe(
        kind=payload.type,
        credential_kind=payload.credential.kind,
        credential=payload.credential.value.get_secret_value(),
        download_cookie=(
            payload.download_cookie.get_secret_value()
            if payload.download_cookie is not None
            else None
        ),
        request_timeout_seconds=(
            payload.request_timeout_seconds
            if payload.request_timeout_seconds is not None
            else site_profile(payload.type).request_timeout_seconds
        ),
        search_interval_seconds=payload.search_interval_seconds,
        user_agent=payload.user_agent,
        browser_emulation_enabled=payload.browser_emulation_enabled,
        proxy_enabled=proxy.enabled,
        proxy_host=proxy.host,
        proxy_port=proxy.port,
        proxy_username=proxy.username,
        proxy_password=(proxy.password.get_secret_value() if proxy.password is not None else None),
    )


@router.post("/sites", response_model=SiteViewResponse, status_code=status.HTTP_201_CREATED)
async def create_site(
    request: Request,
    payload: SiteCreateRequest,
    _principal: Annotated[AccessPrincipal, Depends(CONFIG_WRITE_ACCESS)],
) -> JSONResponse:
    created = site_service(request).create(
        name=payload.name,
        kind=payload.type,
        credential_kind=payload.credential.kind if payload.credential is not None else None,
        credential=(
            payload.credential.value.get_secret_value() if payload.credential is not None else None
        ),
        download_cookie=(
            payload.download_cookie.get_secret_value()
            if payload.download_cookie is not None
            else None
        ),
        request_timeout_seconds=(
            payload.request_timeout_seconds
            if payload.request_timeout_seconds is not None
            else site_profile(payload.type).request_timeout_seconds
        ),
        search_interval_seconds=payload.search_interval_seconds,
        user_agent=payload.user_agent,
        browser_emulation_enabled=payload.browser_emulation_enabled,
        proxy_enabled=payload.proxy.enabled,
        proxy_host=payload.proxy.host,
        proxy_port=payload.proxy.port,
        proxy_username=payload.proxy.username,
        proxy_password=(
            payload.proxy.password.get_secret_value()
            if payload.proxy.password is not None
            else None
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


@router.get("/sites/{site_id}/detail", response_model=SiteDetailResponse)
async def get_site_detail(
    site_id: str,
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(CONFIG_READ_ACCESS)],
) -> SiteDetailResponse:
    return await _site_detail_response(request, site_id)


@router.post("/sites/{site_id}/sync", response_model=SiteDetailResponse)
async def sync_site_user_profile(
    site_id: str,
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(CONFIG_WRITE_ACCESS)],
) -> SiteDetailResponse:
    await site_service(request).sync_user_profile(site_id)
    return await _site_detail_response(request, site_id)


@router.get("/sites/{site_id}/sync-history", response_model=SiteSyncHistoryResponse)
async def get_site_sync_history(
    site_id: str,
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(CONFIG_READ_ACCESS)],
) -> SiteSyncHistoryResponse:
    service = site_service(request)
    service.get(site_id)
    return SiteSyncHistoryResponse(
        items=[
            SiteSyncHistoryItemResponse.model_validate(item)
            for item in service.list_user_profile_sync_history(site_id)
        ]
    )


@router.get("/sites/{site_id}/seeding", response_model=SiteSeedingResponse)
async def get_site_seeding(
    site_id: str,
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(CONFIG_READ_ACCESS)],
) -> SiteSeedingResponse:
    site_service(request).get(site_id)
    return SiteSeedingResponse(supported=False, items=[], next_cursor=None)


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
    if payload.clear_download_cookie and payload.download_cookie is not None:
        raise ApplicationError(
            code="SITE_CREDENTIAL_INVALID",
            status=422,
            title="下载凭证操作冲突",
            detail="download_cookie 与 clear_download_cookie 不能同时使用",
        )
    action: Literal["KEEP", "SET", "CLEAR"] = "KEEP"
    if payload.clear_credential:
        action = "CLEAR"
    elif "credential" in payload.model_fields_set and payload.credential is not None:
        action = "SET"
    proxy_password_action, proxy_password = _proxy_password_action(payload.proxy)
    updated = site_service(request).update(
        site_id,
        expected_version=_expected_version(if_match),
        update_request=SiteUpdate(
            name=payload.name,
            type=payload.type,
            credential_action=action,
            credential_kind=payload.credential.kind if payload.credential is not None else None,
            credential=(
                payload.credential.value.get_secret_value()
                if payload.credential is not None
                else None
            ),
            download_cookie_action=(
                "CLEAR"
                if payload.clear_download_cookie
                else "SET"
                if payload.download_cookie is not None
                else "KEEP"
            ),
            download_cookie=(
                payload.download_cookie.get_secret_value()
                if payload.download_cookie is not None
                else None
            ),
            runtime_config=_runtime_patch(payload),
            proxy_password_action=proxy_password_action,
            proxy_password=proxy_password,
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
    record = site_service(request).set_enabled(
        site_id,
        expected_version=_expected_version(if_match),
        enabled=payload.action == "enable",
    )
    return _json_with_etag(record)
