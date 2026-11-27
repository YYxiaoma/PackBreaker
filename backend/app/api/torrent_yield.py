"""监控中心 · 种子收益归因接口（只用实测值）。"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, Depends, Query, Request, Response, status
from pydantic import BaseModel, Field

from backend.app.api.dependencies import (
    AccessPrincipal,
    require_admin_csrf_principal,
    require_admin_principal,
)
from backend.app.application.torrent_yield import TorrentYieldService

router = APIRouter(prefix="/monitor/yield", tags=["monitor"])
READ_ACCESS = require_admin_principal
WRITE_ACCESS = require_admin_csrf_principal


def _service(request: Request) -> TorrentYieldService:
    return cast(TorrentYieldService, request.app.state.torrent_yield_service)


class YieldSummaryResponse(BaseModel):
    window_days: int
    retention_days: int
    generated_at: datetime
    total_upload_bytes: int
    total_bonus_contribution: float | None
    total_bonus_per_hour_contribution: float | None
    zero_upload_count: int
    observed_torrents: int
    contribution_available: bool


class YieldTorrentResponse(BaseModel):
    key: str
    downloader_id: str
    torrent_hash: str
    title: str
    site_key: str | None
    remote_torrent_id: str | None
    size_bytes: int | None
    upload_bytes: int
    bonus_contribution: float | None
    bonus_per_hour_contribution: float | None
    contribution_available: bool
    seeding_seconds: int
    zero_upload: bool
    status: str
    reclaimable_bytes: int


class YieldTorrentListResponse(BaseModel):
    items: list[YieldTorrentResponse]
    total: int
    page: int
    page_size: int


class YieldRefreshResponse(BaseModel):
    accepted: bool
    reason: str | None
    scanned: int
    recorded: int
    skipped: int
    errors: list[str]


class CleanupPreviewRequest(BaseModel):
    keys: list[str] = Field(default_factory=list, max_length=500)


class CleanupPreviewResponse(BaseModel):
    reclaimable_bytes: int
    bonus_loss: float | None
    bonus_loss_available: bool
    matched_files: int
    unmatched_torrents: list[str]
    warnings: list[str]


def _days(days: int) -> int:
    return min(max(days, 1), 365)


@router.get("/summary", response_model=YieldSummaryResponse)
def get_summary(
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(READ_ACCESS)],
    days: int = 30,
) -> dict[str, Any]:
    return asdict(_service(request).summary(_days(days)))


@router.get("/torrents", response_model=YieldTorrentListResponse)
def list_torrents(
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(READ_ACCESS)],
    days: int = 30,
    sort: Literal["upload", "bonus", "hourly"] = "upload",
    order: Literal["asc", "desc"] = "desc",
    zero_only: bool = False,
    q: Annotated[str | None, Query(max_length=200)] = None,
    site: Annotated[str | None, Query(max_length=64)] = None,
    page: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    result = _service(request).list_torrents(
        _days(days),
        sort=sort,
        order=order,
        zero_only=zero_only,
        query=q,
        site_key=site,
        page=page,
        page_size=page_size,
    )
    return {
        "items": [asdict(item) for item in result.items],
        "total": result.total,
        "page": result.page,
        "page_size": result.page_size,
    }


@router.post("/refresh", status_code=status.HTTP_202_ACCEPTED, response_model=YieldRefreshResponse)
async def refresh(
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(WRITE_ACCESS)],
) -> dict[str, Any]:
    return await _service(request).capture_once()


@router.post("/cleanup-preview", response_model=CleanupPreviewResponse)
def cleanup_preview(
    payload: CleanupPreviewRequest,
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(READ_ACCESS)],
) -> dict[str, Any]:
    return asdict(_service(request).cleanup_preview(payload.keys))


@router.post("/cleanup-export")
def cleanup_export(
    payload: CleanupPreviewRequest,
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(READ_ACCESS)],
) -> Response:
    # 用 POST 不是为了写数据，而是 keys 拼进 query string 会顶到 8 KB 上限（约 100 个 key），
    # 而预览允许选 500 个，导出会直接 422。改成请求体后与预览共用同一份上限。
    content = _service(request).cleanup_export(payload.keys)
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="torrent-yield-cleanup.csv"'},
    )
