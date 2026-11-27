"""监控中心 · 站点资产趋势接口（只读）。"""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel

from backend.app.api.dependencies import AccessPrincipal, require_admin_principal
from backend.app.application.site_assets import SiteAssetsService

router = APIRouter(prefix="/monitor/site-assets", tags=["monitor"])
READ_ACCESS = require_admin_principal


def _service(request: Request) -> SiteAssetsService:
    return cast(SiteAssetsService, request.app.state.site_assets_service)


class SiteAssetRatioResponse(BaseModel):
    site_id: str
    site_name: str
    site_type: str
    ratio: float


class SiteAssetSummaryResponse(BaseModel):
    window_days: int
    generated_at: datetime
    bonus_total: float | None
    seeding_points_total: float | None
    bonus_per_hour_total: float | None
    seeding_count_total: int | None
    site_count: int
    changes: dict[str, float | None]
    incomplete_sites: list[str]
    ratio_distribution: list[SiteAssetRatioResponse]


class SiteAssetPointResponse(BaseModel):
    stat_date: date
    ratio: float | None
    bonus: float | None
    seeding_points: float | None
    bonus_per_hour: float | None
    seeding_count: int | None
    site_count: int


class SiteAssetSiteResponse(BaseModel):
    site_id: str
    site_name: str
    site_type: str
    ratio: float | None
    bonus: float | None
    seeding_points: float | None
    bonus_per_hour: float | None
    seeding_count: int | None
    ratio_change: float | None
    fetched_at: datetime


class SiteAssetSiteListResponse(BaseModel):
    items: list[SiteAssetSiteResponse]
    total: int
    page: int
    page_size: int


def _days(days: int) -> int:
    return min(max(days, 1), 365)


@router.get("/summary", response_model=SiteAssetSummaryResponse)
def get_summary(
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(READ_ACCESS)],
    days: int = 30,
) -> dict[str, Any]:
    return asdict(_service(request).summary(_days(days)))


@router.get("/trend", response_model=list[SiteAssetPointResponse])
def get_trend(
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(READ_ACCESS)],
    days: int = 30,
) -> list[dict[str, Any]]:
    return [asdict(item) for item in _service(request).trend(_days(days))]


@router.get("/sites", response_model=SiteAssetSiteListResponse)
def list_sites(
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(READ_ACCESS)],
    days: int = 30,
    q: Annotated[str | None, Query(max_length=120)] = None,
    page: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    result = _service(request).list_sites(_days(days), query=q, page=page, page_size=page_size)
    return {
        "items": [asdict(item) for item in result.items],
        "total": result.total,
        "page": result.page,
        "page_size": result.page_size,
    }


@router.get("/export")
def export_sites(
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(READ_ACCESS)],
    days: int = 30,
) -> Response:
    content = _service(request).export_csv(_days(days))
    filename = f"site-assets-{_days(days)}d.csv"
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
