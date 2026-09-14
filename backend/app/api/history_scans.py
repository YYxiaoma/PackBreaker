from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from backend.app.api.dependencies import (
    AccessPrincipal,
    history_scan_service,
    require_admin_csrf_or_scope,
    require_admin_or_scope,
    task_analysis_service,
)
from backend.app.application.errors import ApplicationError
from backend.app.application.history_scans import (
    HistoryMaterializationItemView,
    HistoryMaterializeResult,
    HistoryScanBatchResult,
    HistoryScanView,
    HistoryTaskResultView,
)
from backend.app.domain.auth import ApiScope
from backend.app.domain.history_scan import (
    HistoryMaterializationStatus,
    HistoryMediaKind,
    HistoryScanStatus,
)
from backend.app.domain.media_matching import EpisodeKind
from backend.app.domain.task_state import TaskStatus

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
    action: Literal["start", "pause", "resume", "cancel", "scan", "materialize"]
    limit: int = Field(default=100, ge=1, le=1000)


class HistoryScanBatchResponse(BaseModel):
    scan: HistoryScanResponse
    processed_count: int
    has_more: bool


class HistoryMaterializationItemResponse(BaseModel):
    materialization_id: str
    scan_file_id: str
    status: HistoryMaterializationStatus
    reason_code: str | None
    task_id: str | None
    task_created: bool
    source_root: str | None
    normalized_unit_key: str | None
    unit_kind: str | None


class HistoryScanMaterializeResponse(BaseModel):
    scan: HistoryScanResponse
    processed_count: int
    task_created_count: int
    task_reused_count: int
    skipped_count: int
    remaining_count: int
    items: list[HistoryMaterializationItemResponse]


class HistoryTaskResultResponse(BaseModel):
    materialization_id: str
    scan_file_id: str
    relative_path: str
    snapshot_digest: str
    materialization_status: HistoryMaterializationStatus
    reason_code: str | None
    task_id: str | None
    source_root: str | None
    normalized_unit_key: str | None
    unit_kind: str | None
    episode_kind: EpisodeKind | None
    episode_season: int | None
    episode_start: int | None
    episode_end: int | None
    episode_label: str | None
    episode_group_key: str | None
    episode_variant_key: str | None
    variant_count: int
    task_status: TaskStatus | None
    task_version: int | None
    task_error_code: str | None
    analysis_eligible: bool
    has_preflight: bool
    created_at: datetime


class HistoryTaskResultListResponse(BaseModel):
    items: list[HistoryTaskResultResponse]


class HistoryTaskBatchActionRequest(BaseModel):
    action: Literal["analyze"]
    task_ids: list[str] = Field(min_length=1, max_length=10)


class HistoryTaskBatchAnalyzeItemResponse(BaseModel):
    task_id: str
    source_root: str
    attempted: bool
    succeeded: bool
    task_status: TaskStatus
    error_code: str | None


class HistoryTaskBatchAnalyzeResponse(BaseModel):
    action: Literal["analyze"] = "analyze"
    attempted_count: int
    succeeded_count: int
    failed_count: int
    skipped_count: int
    items: list[HistoryTaskBatchAnalyzeItemResponse]


def _materialization_item_view(
    record: HistoryMaterializationItemView,
) -> HistoryMaterializationItemResponse:
    return HistoryMaterializationItemResponse(
        materialization_id=record.materialization_id,
        scan_file_id=record.scan_file_id,
        status=record.status,
        reason_code=record.reason_code,
        task_id=record.task_id,
        task_created=record.task_created,
        source_root=record.source_root,
        normalized_unit_key=record.normalized_unit_key,
        unit_kind=record.unit_kind,
    )


def _history_task_result_response(record: HistoryTaskResultView) -> HistoryTaskResultResponse:
    return HistoryTaskResultResponse(
        materialization_id=record.materialization_id,
        scan_file_id=record.scan_file_id,
        relative_path=record.relative_path,
        snapshot_digest=record.snapshot_digest,
        materialization_status=record.materialization_status,
        reason_code=record.reason_code,
        task_id=record.task_id,
        source_root=record.source_root,
        normalized_unit_key=record.normalized_unit_key,
        unit_kind=record.unit_kind,
        episode_kind=EpisodeKind(record.episode_kind) if record.episode_kind is not None else None,
        episode_season=record.episode_season,
        episode_start=record.episode_start,
        episode_end=record.episode_end,
        episode_label=record.episode_label,
        episode_group_key=record.episode_group_key,
        episode_variant_key=record.episode_variant_key,
        variant_count=record.variant_count,
        task_status=record.task_status,
        task_version=record.task_version,
        task_error_code=record.task_error_code,
        analysis_eligible=record.analysis_eligible,
        has_preflight=record.has_preflight,
        created_at=record.created_at,
    )


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


@router.get(
    "/history-scans/{scan_id}/tasks",
    response_model=HistoryTaskResultListResponse,
)
async def list_history_scan_tasks(
    scan_id: str,
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(TASKS_READ_ACCESS)],
    limit: Annotated[int, Query(ge=1, le=1000)] = 500,
    task_status: Annotated[TaskStatus | None, Query()] = None,
    materialization_status: Annotated[HistoryMaterializationStatus | None, Query()] = None,
    query: Annotated[str | None, Query(max_length=200)] = None,
) -> HistoryTaskResultListResponse:
    items = history_scan_service(request).list_task_results(
        scan_id,
        limit=limit,
        task_status=task_status,
        materialization_status=materialization_status,
        query=query,
    )
    return HistoryTaskResultListResponse(
        items=[_history_task_result_response(item) for item in items]
    )


@router.post(
    "/history-scans/{scan_id}/tasks/actions",
    response_model=HistoryTaskBatchAnalyzeResponse,
)
async def history_scan_task_action(
    scan_id: str,
    request: Request,
    payload: HistoryTaskBatchActionRequest,
    _principal: Annotated[AccessPrincipal, Depends(TASKS_WRITE_ACCESS)],
) -> HistoryTaskBatchAnalyzeResponse:
    targets = history_scan_service(request).analysis_targets(
        scan_id,
        task_ids=tuple(payload.task_ids),
    )
    service = task_analysis_service(request)
    items: list[HistoryTaskBatchAnalyzeItemResponse] = []
    attempted_count = 0
    succeeded_count = 0
    failed_count = 0
    skipped_count = 0
    eligible = {TaskStatus.PENDING, TaskStatus.RETRY, TaskStatus.PAUSED}
    for target in targets:
        before = service.get_task(target.task_id)
        before_status = TaskStatus(before.status)
        if before_status not in eligible:
            skipped_count += 1
            items.append(
                HistoryTaskBatchAnalyzeItemResponse(
                    task_id=target.task_id,
                    source_root=target.source_root,
                    attempted=False,
                    succeeded=False,
                    task_status=before_status,
                    error_code="TASK_STATE_NOT_ANALYZABLE",
                )
            )
            continue
        attempted_count += 1
        try:
            await service.analyze(target.task_id, source_root=target.source_root)
        except ApplicationError as exc:
            failed_count += 1
            after = service.get_task(target.task_id)
            items.append(
                HistoryTaskBatchAnalyzeItemResponse(
                    task_id=target.task_id,
                    source_root=target.source_root,
                    attempted=True,
                    succeeded=False,
                    task_status=TaskStatus(after.status),
                    error_code=exc.code,
                )
            )
            continue
        succeeded_count += 1
        after = service.get_task(target.task_id)
        items.append(
            HistoryTaskBatchAnalyzeItemResponse(
                task_id=target.task_id,
                source_root=target.source_root,
                attempted=True,
                succeeded=True,
                task_status=TaskStatus(after.status),
                error_code=None,
            )
        )
    return HistoryTaskBatchAnalyzeResponse(
        attempted_count=attempted_count,
        succeeded_count=succeeded_count,
        failed_count=failed_count,
        skipped_count=skipped_count,
        items=items,
    )


@router.post(
    "/history-scans/{scan_id}/actions",
    response_model=HistoryScanResponse | HistoryScanBatchResponse | HistoryScanMaterializeResponse,
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
    if payload.action == "cancel":
        return _json_with_etag(service.cancel(scan_id, expected_version=version))
    if payload.action == "materialize":
        materialized: HistoryMaterializeResult = service.materialize(
            scan_id,
            expected_version=version,
            limit=payload.limit,
        )
        materialize_response = HistoryScanMaterializeResponse(
            scan=_view(materialized.scan),
            processed_count=materialized.processed_count,
            task_created_count=materialized.task_created_count,
            task_reused_count=materialized.task_reused_count,
            skipped_count=materialized.skipped_count,
            remaining_count=materialized.remaining_count,
            items=[_materialization_item_view(item) for item in materialized.items],
        )
        return JSONResponse(
            materialize_response.model_dump(mode="json"),
            headers={"ETag": f'"{materialized.scan.version}"'},
        )
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
