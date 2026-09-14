from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from backend.app.api.dependencies import (
    AccessPrincipal,
    history_scan_service,
    require_admin_csrf_or_scope,
    require_admin_or_scope,
)
from backend.app.application.errors import ApplicationError
from backend.app.application.history_scans import HistoryScanBatchResult, HistoryScanView
from backend.app.domain.auth import ApiScope
from backend.app.domain.history_scan import HistoryMediaKind, HistoryScanStatus

router = APIRouter(tags=["history-scans"])
TASKS_READ_ACCESS = require_admin_or_scope(ApiScope.TASKS_READ)
TASKS_WRITE_ACCESS = require_admin_csrf_or_scope(ApiScope.TASKS_WRITE)


class HistoryScanResponse(BaseModel):
    id: str
    root_path: str
    media_kind: HistoryMediaKind
    extensions: list[str]
    exclude_patterns: list[str]
    status: HistoryScanStatus
    generation: int
    cursor: str | None
    discovered_count: int
    new_count: int
    changed_count: int
    unchanged_count: int
    version: int
    last_started_at: datetime | None
    last_completed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class HistoryScanListResponse(BaseModel):
    items: list[HistoryScanResponse]


class HistoryScanCreateRequest(BaseModel):
    root_path: str = Field(min_length=1, max_length=2048)
    media_kind: HistoryMediaKind
    extensions: list[str] = Field(min_length=1, max_length=32)
    exclude_patterns: list[str] = Field(default_factory=list, max_length=64)


class HistoryScanActionRequest(BaseModel):
    action: Literal["start", "pause", "resume", "scan"]
    limit: int = Field(default=100, ge=1, le=1000)


class HistoryScanBatchResponse(BaseModel):
    scan: HistoryScanResponse
    processed_count: int
    has_more: bool


def _view(record: HistoryScanView) -> HistoryScanResponse:
    root_path = (
        "/data" if record.root_relative_path == "." else f"/data/{record.root_relative_path}"
    )
    return HistoryScanResponse(
        id=record.id,
        root_path=root_path,
        media_kind=record.media_kind,
        extensions=list(record.extensions),
        exclude_patterns=list(record.exclude_patterns),
        status=record.status,
        generation=record.generation,
        cursor=record.cursor,
        discovered_count=record.discovered_count,
        new_count=record.new_count,
        changed_count=record.changed_count,
        unchanged_count=record.unchanged_count,
        version=record.version,
        last_started_at=record.last_started_at,
        last_completed_at=record.last_completed_at,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _expected_version(value: str | None) -> int:
    if value is None:
        raise ApplicationError(
            code="IF_MATCH_REQUIRED",
            status=428,
            title="缺少版本前置条件",
            detail="推进历史扫描时必须提供 If-Match 版本",
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
            detail="历史扫描版本必须大于等于 1",
        )
    return parsed


def _json_with_etag(record: HistoryScanView, *, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        _view(record).model_dump(mode="json"),
        status_code=status_code,
        headers={"ETag": f'"{record.version}"'},
    )


@router.get("/history-scans", response_model=HistoryScanListResponse)
async def list_history_scans(
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(TASKS_READ_ACCESS)],
) -> HistoryScanListResponse:
    return HistoryScanListResponse(
        items=[_view(item) for item in history_scan_service(request).list_scans()]
    )


@router.post(
    "/history-scans",
    response_model=HistoryScanResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_history_scan(
    request: Request,
    payload: HistoryScanCreateRequest,
    _principal: Annotated[AccessPrincipal, Depends(TASKS_WRITE_ACCESS)],
) -> JSONResponse:
    created = history_scan_service(request).create(
        root_path=payload.root_path,
        media_kind=payload.media_kind,
        extensions=tuple(payload.extensions),
        exclude_patterns=tuple(payload.exclude_patterns),
    )
    return _json_with_etag(created, status_code=status.HTTP_201_CREATED)


@router.get("/history-scans/{scan_id}", response_model=HistoryScanResponse)
async def get_history_scan(
    scan_id: str,
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(TASKS_READ_ACCESS)],
) -> JSONResponse:
    return _json_with_etag(history_scan_service(request).get(scan_id))


@router.post(
    "/history-scans/{scan_id}/actions",
    response_model=HistoryScanResponse | HistoryScanBatchResponse,
)
async def history_scan_action(
    scan_id: str,
    request: Request,
    payload: HistoryScanActionRequest,
    _principal: Annotated[AccessPrincipal, Depends(TASKS_WRITE_ACCESS)],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> JSONResponse:
    service = history_scan_service(request)
    version = _expected_version(if_match)
    if payload.action == "start":
        return _json_with_etag(service.start(scan_id, expected_version=version))
    if payload.action == "pause":
        return _json_with_etag(service.pause(scan_id, expected_version=version))
    if payload.action == "resume":
        return _json_with_etag(service.resume(scan_id, expected_version=version))
    result: HistoryScanBatchResult = service.scan_batch(
        scan_id,
        expected_version=version,
        limit=payload.limit,
    )
    response = HistoryScanBatchResponse(
        scan=_view(result.scan),
        processed_count=result.processed_count,
        has_more=result.has_more,
    )
    return JSONResponse(
        response.model_dump(mode="json"),
        headers={"ETag": f'"{result.scan.version}"'},
    )
