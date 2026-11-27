"""监控中心 · 辅种覆盖率报告接口。"""

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
from backend.app.application.media_coverage import MediaCoverageService

router = APIRouter(prefix="/monitor/coverage", tags=["monitor"])
READ_ACCESS = require_admin_principal
WRITE_ACCESS = require_admin_csrf_principal


def _service(request: Request) -> MediaCoverageService:
    return cast(MediaCoverageService, request.app.state.media_coverage_service)


class CoverageRunRequest(BaseModel):
    selection_token: str = Field(min_length=1, max_length=8192)


class CoverageScopeRequest(BaseModel):
    selection_token: str = Field(min_length=1, max_length=8192)
    scope_label: str = Field(default="", max_length=255)


class CoverageScopeResponse(BaseModel):
    selection_token: str
    scope_label: str
    updated_at: datetime


class CoverageRunAcceptedResponse(BaseModel):
    run_id: str
    accepted: bool


class CoverageRunResponse(BaseModel):
    id: str
    scope_label: str
    status: str
    phase: str
    started_at: datetime
    finished_at: datetime | None
    movie_count: int
    site_count: int
    watched_count: int
    identity_resolved_count: int
    degraded_sites: list[str]
    error_code: str | None


class CoverageSummaryResponse(BaseModel):
    generated_at: datetime
    last_run: CoverageRunResponse | None
    total_movies: int
    buckets: dict[str, int]
    known_site_count: int
    watched_torrent_count: int
    low_confidence_count: int


class CoverageMovieResponse(BaseModel):
    movie_key: str
    display_title: str
    year: int | None
    resolution: str | None
    site_keys: list[str]
    coverage_count: int
    bucket: str
    match_level: str
    confidence: str
    suggestion: str
    relative_path_hint: str | None


class CoverageMovieListResponse(BaseModel):
    items: list[CoverageMovieResponse]
    total: int
    page: int
    page_size: int


@router.get("/scope", response_model=CoverageScopeResponse | None)
def get_scope(
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(READ_ACCESS)],
) -> dict[str, Any] | None:
    scope = _service(request).get_scope()
    return asdict(scope) if scope is not None else None


@router.put("/scope", response_model=CoverageScopeResponse)
def save_scope(
    payload: CoverageScopeRequest,
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(WRITE_ACCESS)],
) -> dict[str, Any]:
    return asdict(_service(request).save_scope(payload.selection_token, payload.scope_label))


@router.post(
    "/runs", status_code=status.HTTP_202_ACCEPTED, response_model=CoverageRunAcceptedResponse
)
async def create_run(
    payload: CoverageRunRequest,
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(WRITE_ACCESS)],
) -> dict[str, Any]:
    run_id = await _service(request).create_run(payload.selection_token)
    return {"run_id": run_id, "accepted": True}


@router.get("/runs", response_model=list[CoverageRunResponse])
def list_runs(
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(READ_ACCESS)],
    limit: int = 20,
) -> list[dict[str, Any]]:
    return [asdict(item) for item in _service(request).runs(limit)]


@router.get("/summary", response_model=CoverageSummaryResponse)
def get_summary(
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(READ_ACCESS)],
) -> dict[str, Any]:
    return asdict(_service(request).summary())


@router.get("/movies", response_model=CoverageMovieListResponse)
def list_movies(
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(READ_ACCESS)],
    bucket: Literal["ZERO", "ONE", "TWO", "THREE_PLUS", "UNKNOWN"] | None = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
    resolution: Annotated[str | None, Query(max_length=16)] = None,
    page: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    result = _service(request).list_movies(
        bucket=bucket, query=q, resolution=resolution, page=page, page_size=page_size
    )
    return {
        "items": [asdict(item) for item in result.items],
        "total": result.total,
        "page": result.page,
        "page_size": result.page_size,
    }


@router.get("/export")
def export_movies(
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(READ_ACCESS)],
    bucket: Literal["ZERO", "ONE", "TWO", "THREE_PLUS", "UNKNOWN"] | None = None,
) -> Response:
    content = _service(request).export_csv(bucket)
    suffix = bucket or "all"
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="coverage-{suffix}.csv"'},
    )
