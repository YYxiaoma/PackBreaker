"""监控中心 · 做种健康巡查接口。

读接口只返回数据库快照，不触发任何外部请求；唯一的写动作是「立即巡查」，
它复用后台调度器，避免与定时任务并发跑两遍。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import BaseModel, Field

from backend.app.api.dependencies import (
    AccessPrincipal,
    require_admin_csrf_principal,
    require_admin_principal,
)
from backend.app.application.seeding_health import SeedingHealthService
from backend.app.application.seeding_health_driver import SeedingHealthDriver
from backend.app.domain.seeding_health import SeedingRunTrigger

router = APIRouter(prefix="/monitor/seeding-health", tags=["monitor"])
MONITOR_READ = require_admin_principal
MONITOR_WRITE = require_admin_csrf_principal


def _service(request: Request) -> SeedingHealthService:
    return cast(SeedingHealthService, request.app.state.seeding_health_service)


class SeedingRunResponse(BaseModel):
    id: str
    trigger: str
    status: str
    started_at: datetime
    finished_at: datetime | None
    scanned_watches: int
    created_issues: int
    resolved_issues: int
    degraded_sites: list[str]
    error_summary: str | None


class SeedingHealthSummaryResponse(BaseModel):
    watched: int
    healthy: int
    abnormal: int
    hnr_risk: int
    pending: int
    recovered: int
    by_kind: dict[str, int]
    last_run: SeedingRunResponse | None
    next_run_at: datetime | None
    stale: bool
    interval_hours: int
    coverage: dict[str, int]


class SeedingIssueRowResponse(BaseModel):
    id: str
    kind: str
    kind_label: str
    severity: str
    risk_score: int
    status: str
    suggestion: str
    detect_count: int
    first_detected_at: datetime
    last_detected_at: datetime
    torrent_name: str
    torrent_hash: str
    site_key: str | None
    downloader_id: str | None = None
    size_bytes: int
    consecutive_misses: int
    affected_torrents: int


class SeedingIssueListResponse(BaseModel):
    items: list[SeedingIssueRowResponse]
    total: int
    page: int
    page_size: int


class SeedingIssueDetailResponse(SeedingIssueRowResponse):
    evidence: dict[str, Any]
    hnr_remaining_hours: float | None
    resolved_at: datetime | None
    resolved_reason: str | None
    save_path: str
    content_path: str | None
    identity_source: str | None
    identity_locked: bool
    seeding_time_seconds: int | None


class SeedingHealthSettingsResponse(BaseModel):
    site_probe_enabled: bool


class SeedingHealthSettingsRequest(BaseModel):
    site_probe_enabled: bool


class RepairPreviewRequest(BaseModel):
    action: str
    downloader_id: str | None = None
    save_path: str | None = None


class RepairExecuteRequest(BaseModel):
    action: str
    expected_payload_sha256: str
    downloader_id: str | None = None
    save_path: str | None = None


class RepairExecuteResponse(BaseModel):
    journal_id: str
    status: str
    torrent_hash: str | None = None
    already_recorded: bool


class SeedingPolicyResponse(BaseModel):
    site_key: str
    min_seeding_hours: int | None
    requires_hr: bool
    source: str


class IssueActionRequest(BaseModel):
    action: Literal["ACK", "IGNORE", "RECHECK", "RETRIEVE_TORRENT", "RESEED"]


class IssueActionResultResponse(BaseModel):
    id: str
    status: str


class SeedingPolicyRequest(BaseModel):
    min_seeding_hours: int | None = Field(default=None, ge=0, le=8760)


class SeedingRunAcceptedResponse(BaseModel):
    run_id: str | None
    accepted: bool


@router.get("/summary", response_model=SeedingHealthSummaryResponse)
async def get_summary(
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(MONITOR_READ)],
) -> dict[str, Any]:
    return _service(request).summary()


@router.get("/issues", response_model=SeedingIssueListResponse)
async def list_issues(
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(MONITOR_READ)],
    kind: str | None = None,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    severity: str | None = None,
    site_key: str | None = None,
    downloader_id: str | None = None,
    q: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    return _service(request).list_issues(
        kind=kind,
        status=status_filter,
        severity=severity,
        site_key=site_key,
        downloader_id=downloader_id,
        search=q,
        page=page,
        page_size=page_size,
    )


@router.get("/issues/{issue_id}", response_model=SeedingIssueDetailResponse)
async def get_issue(
    issue_id: str,
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(MONITOR_READ)],
) -> dict[str, Any]:
    return _service(request).issue_detail(issue_id)


@router.post("/issues/{issue_id}/actions", response_model=IssueActionResultResponse)
async def act_on_issue(
    issue_id: str,
    payload: IssueActionRequest,
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(MONITOR_WRITE)],
) -> dict[str, Any]:
    return await _service(request).act(issue_id, payload.action)


@router.post("/issues/{issue_id}/repair-preview", response_model=dict[str, Any])
async def preview_issue_repair(
    issue_id: str,
    payload: RepairPreviewRequest,
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(MONITOR_READ)],
) -> dict[str, Any]:
    """只读预览：拉种、解析、返回摘要与 payload 摘要值，不产生任何外部写操作。"""
    return await _service(request).preview_repair(
        issue_id,
        payload.action,
        downloader_id=payload.downloader_id,
        save_path=payload.save_path,
    )


@router.post("/issues/{issue_id}/repair", response_model=RepairExecuteResponse)
async def execute_issue_repair(
    issue_id: str,
    payload: RepairExecuteRequest,
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(MONITOR_WRITE)],
) -> dict[str, Any]:
    """确认执行：校验摘要值后记 journal intent，再以暂停状态加种并对账。"""
    return await _service(request).execute_repair(
        issue_id,
        payload.action,
        expected_payload_sha256=payload.expected_payload_sha256,
        downloader_id=payload.downloader_id,
        save_path=payload.save_path,
    )


@router.post(
    "/runs", status_code=status.HTTP_202_ACCEPTED, response_model=SeedingRunAcceptedResponse
)
async def trigger_run(
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(MONITOR_WRITE)],
) -> dict[str, Any]:
    driver = cast(SeedingHealthDriver, request.app.state.seeding_health_driver)
    run_id = driver.start_once(SeedingRunTrigger.MANUAL)
    return {"run_id": run_id, "accepted": True}


@router.get("/runs", response_model=list[SeedingRunResponse])
async def list_runs(
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(MONITOR_READ)],
    limit: int = 30,
) -> list[dict[str, Any]]:
    return _service(request).list_runs(limit=max(1, min(limit, 200)))


@router.get("/runs/{run_id}", response_model=SeedingRunResponse)
async def get_run(
    run_id: str,
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(MONITOR_READ)],
) -> dict[str, Any]:
    return _service(request).get_run(run_id)


@router.get("/settings", response_model=SeedingHealthSettingsResponse)
async def get_settings(
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(MONITOR_READ)],
) -> dict[str, Any]:
    return _service(request).get_settings()


@router.put("/settings", response_model=SeedingHealthSettingsResponse)
async def set_settings(
    payload: SeedingHealthSettingsRequest,
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(MONITOR_WRITE)],
) -> dict[str, Any]:
    return _service(request).set_settings(site_probe_enabled=payload.site_probe_enabled)


@router.get("/policies", response_model=list[SeedingPolicyResponse])
async def list_policies(
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(MONITOR_READ)],
) -> list[dict[str, Any]]:
    return _service(request).list_policies()


@router.put("/policies/{site_key}", response_model=SeedingPolicyResponse)
async def set_policy(
    site_key: str,
    payload: SeedingPolicyRequest,
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(MONITOR_WRITE)],
) -> dict[str, Any]:
    return _service(request).set_policy(site_key, min_seeding_hours=payload.min_seeding_hours)
