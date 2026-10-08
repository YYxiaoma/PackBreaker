from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from backend.app.api.dependencies import (
    AccessPrincipal,
    require_admin_csrf_principal,
    require_admin_principal,
    unpack_definition_service,
    unpack_execution_query_service,
    unpack_item_action_service,
    unpack_source_scan_service,
)
from backend.app.application.errors import ApplicationError
from backend.app.application.unpack_definitions import (
    UnpackDefinitionCreate,
    UnpackDefinitionView,
)
from backend.app.application.unpack_executions import (
    UnpackExecutionItemView,
    UnpackExecutionView,
    UnpackMatchCandidateList,
    UnpackMatchCandidateView,
)
from backend.app.application.unpack_source_scans import (
    UnpackSourceScanItemView,
    UnpackSourceScanView,
    UnpackTreeEntryView,
)
from backend.app.domain.unpack import (
    UnpackDefinitionStatus,
    UnpackExecutionScopeKind,
    UnpackExecutionStatus,
    UnpackItemStatus,
    UnpackReviewDecision,
    UnpackSourceKind,
    UnpackTriggerKind,
)

router = APIRouter(tags=["unpack-v2"])
READ_ACCESS = require_admin_principal
WRITE_ACCESS = require_admin_csrf_principal


class UnpackTreeEntryResponse(BaseModel):
    name: str
    display_path: str
    selection_token: str


class UnpackTreeRootsResponse(BaseModel):
    items: list[UnpackTreeEntryResponse]


class UnpackTreeResponse(BaseModel):
    display_path: str
    selection_token: str
    entries: list[UnpackTreeEntryResponse]


class UnpackSourceScanCreateRequest(BaseModel):
    selection_token: str = Field(min_length=1, max_length=8192)
    file_filter: dict[str, Any] = Field(default_factory=dict)


class UnpackSourceScanResponse(BaseModel):
    id: str
    directory_path: str
    discovered_count: int
    selected_count: int
    expires_at: datetime
    version: int


class UnpackSourceScanItemResponse(BaseModel):
    source_object_key: str
    relative_path: str
    filename: str
    extension: str
    resolution: str | None
    size_bytes: int
    selected: bool


class UnpackSourceScanItemsResponse(BaseModel):
    items: list[UnpackSourceScanItemResponse]
    next_cursor: str | None
    has_more: bool


class UnpackSourceScanSelectionRequest(BaseModel):
    source_object_keys: list[str] = Field(min_length=1, max_length=1000)
    selected: bool


class UnpackSourceScanSelectionSummaryResponse(BaseModel):
    discovered_count: int
    selected_count: int


class UnpackDefinitionCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    trigger_kind: UnpackTriggerKind
    source_kind: UnpackSourceKind
    execution_scope_kind: UnpackExecutionScopeKind = UnpackExecutionScopeKind.ALL_MATCHING_MEDIA
    source_config: dict[str, Any]
    file_filter: dict[str, Any] = Field(default_factory=dict)
    site_ids: list[str] = Field(min_length=1, max_length=64)
    output_config: dict[str, Any]
    retry_enabled: bool = True
    max_retries: int = Field(default=3, ge=0, le=10)
    auto_match_threshold_bps: int = Field(default=10_000, ge=0, le=10_000)
    cron_expression: str | None = Field(default=None, max_length=160)
    timezone: str | None = Field(default=None, max_length=64)
    source_scan_id: str | None = Field(default=None, max_length=36)


class UnpackDefinitionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    trigger_kind: UnpackTriggerKind
    status: UnpackDefinitionStatus
    source_kind: UnpackSourceKind
    execution_scope_kind: UnpackExecutionScopeKind
    source_config: dict[str, Any]
    file_filter: dict[str, Any]
    site_ids: list[str]
    output_config: dict[str, Any]
    retry_enabled: bool
    max_retries: int
    auto_match_threshold_bps: int
    cron_expression: str | None
    timezone: str | None
    next_run_at: datetime | None
    last_triggered_at: datetime | None
    selected_source_count: int
    version: int
    created_at: datetime
    updated_at: datetime


class UnpackDefinitionListResponse(BaseModel):
    items: list[UnpackDefinitionResponse]


class UnpackDefinitionActionRequest(BaseModel):
    action: Literal["run"]


class UnpackDefinitionActionResponse(BaseModel):
    definition: UnpackDefinitionResponse
    execution_id: str | None


class UnpackExecutionResponse(BaseModel):
    id: str
    definition_id: str
    trigger: str
    status: UnpackExecutionStatus
    discovery_complete: bool
    discovery_cursor: str | None
    total_count: int
    matched_auto_count: int
    review_count: int
    content_verified_count: int
    content_mismatch_count: int
    timeout_count: int
    error_count: int
    completed_count: int
    started_at: datetime
    finished_at: datetime | None
    version: int
    created_at: datetime
    updated_at: datetime


class UnpackExecutionListResponse(BaseModel):
    items: list[UnpackExecutionResponse]


class UnpackExecutionItemResponse(BaseModel):
    id: str
    execution_id: str
    source_object_key: str
    source_snapshot: dict[str, Any]
    media_identity: dict[str, Any]
    status: UnpackItemStatus
    selected_candidate_id: str | None
    match_origin: str | None
    candidate_generation: int
    retry_count: int
    content_verification_level: str | None
    torrent_metainfo_digest: str | None
    auxiliary_state: dict[str, Any] | None
    review_allowed: bool
    last_error_code: str | None
    last_error_message: str | None
    match_started_at: datetime | None
    match_finished_at: datetime | None
    version: int
    created_at: datetime
    updated_at: datetime


class UnpackExecutionItemListResponse(BaseModel):
    items: list[UnpackExecutionItemResponse]
    next_cursor: str | None
    has_more: bool


class UnpackMatchCandidateResponse(BaseModel):
    id: str
    item_id: str
    generation: int
    site_id: str
    candidate_key: str
    title: str
    size_bytes: int | None
    imdb_id: str | None
    douban_id: str | None
    seeders: int | None
    score_bps: int
    is_exact_match: bool
    evidence: dict[str, Any]
    verification_status: str
    verification_level: str | None
    verification_error_code: str | None
    created_at: datetime


class UnpackMatchCandidateListResponse(BaseModel):
    item_id: str
    generation: int
    item_version: int
    default_candidate_id: str | None
    candidates: list[UnpackMatchCandidateResponse]


class UnpackReviewRequest(BaseModel):
    decision: UnpackReviewDecision
    candidate_id: str | None = Field(default=None, max_length=36)
    generation: int = Field(ge=0)


class UnpackReviewResponse(BaseModel):
    decision_id: str
    item_id: str
    decision: UnpackReviewDecision
    candidate_id: str | None
    generation: int
    item_version: int
    item_status: UnpackItemStatus
    decided_at: datetime
    replayed: bool


class UnpackItemActionRequest(BaseModel):
    action: Literal["retry_match"]


class UnpackItemActionResponse(BaseModel):
    item_id: str
    generation: int
    retry_count: int
    item_version: int
    item_status: UnpackItemStatus


def _tree_entry_response(view: UnpackTreeEntryView) -> UnpackTreeEntryResponse:
    return UnpackTreeEntryResponse(
        name=view.name,
        display_path=view.display_path,
        selection_token=view.selection_token,
    )


def _source_scan_response(view: UnpackSourceScanView) -> UnpackSourceScanResponse:
    return UnpackSourceScanResponse(
        id=view.id,
        directory_path=view.directory_path,
        discovered_count=view.discovered_count,
        selected_count=view.selected_count,
        expires_at=view.expires_at,
        version=view.version,
    )


def _source_scan_item_response(view: UnpackSourceScanItemView) -> UnpackSourceScanItemResponse:
    return UnpackSourceScanItemResponse(
        source_object_key=view.source_object_key,
        relative_path=view.relative_path,
        filename=view.filename,
        extension=view.extension,
        resolution=view.resolution,
        size_bytes=view.size_bytes,
        selected=view.selected,
    )


def _definition_response(view: UnpackDefinitionView) -> UnpackDefinitionResponse:
    return UnpackDefinitionResponse(
        id=view.id,
        name=view.name,
        trigger_kind=view.trigger_kind,
        status=view.status,
        source_kind=view.source_kind,
        execution_scope_kind=view.execution_scope_kind,
        source_config=view.source_config,
        file_filter=view.file_filter,
        site_ids=list(view.site_ids),
        output_config=view.output_config,
        retry_enabled=view.retry_enabled,
        max_retries=view.max_retries,
        auto_match_threshold_bps=view.auto_match_threshold_bps,
        cron_expression=view.cron_expression,
        timezone=view.timezone,
        next_run_at=view.next_run_at,
        last_triggered_at=view.last_triggered_at,
        selected_source_count=view.selected_source_count,
        version=view.version,
        created_at=view.created_at,
        updated_at=view.updated_at,
    )


def _execution_response(view: UnpackExecutionView) -> UnpackExecutionResponse:
    return UnpackExecutionResponse(
        id=view.id,
        definition_id=view.definition_id,
        trigger=view.trigger,
        status=view.status,
        discovery_complete=view.discovery_complete,
        discovery_cursor=view.discovery_cursor,
        total_count=view.total_count,
        matched_auto_count=view.matched_auto_count,
        review_count=view.review_count,
        content_verified_count=view.content_verified_count,
        content_mismatch_count=view.content_mismatch_count,
        timeout_count=view.timeout_count,
        error_count=view.error_count,
        completed_count=view.completed_count,
        started_at=view.started_at,
        finished_at=view.finished_at,
        version=view.version,
        created_at=view.created_at,
        updated_at=view.updated_at,
    )


def _execution_item_response(view: UnpackExecutionItemView) -> UnpackExecutionItemResponse:
    return UnpackExecutionItemResponse(
        id=view.id,
        execution_id=view.execution_id,
        source_object_key=view.source_object_key,
        source_snapshot=view.source_snapshot,
        media_identity=view.media_identity,
        status=view.status,
        selected_candidate_id=view.selected_candidate_id,
        match_origin=view.match_origin,
        candidate_generation=view.candidate_generation,
        retry_count=view.retry_count,
        content_verification_level=view.content_verification_level,
        torrent_metainfo_digest=view.torrent_metainfo_digest,
        auxiliary_state=view.auxiliary_state,
        review_allowed=view.review_allowed,
        last_error_code=view.last_error_code,
        last_error_message=view.last_error_message,
        match_started_at=view.match_started_at,
        match_finished_at=view.match_finished_at,
        version=view.version,
        created_at=view.created_at,
        updated_at=view.updated_at,
    )


def _candidate_response(view: UnpackMatchCandidateView) -> UnpackMatchCandidateResponse:
    return UnpackMatchCandidateResponse(
        id=view.id,
        item_id=view.item_id,
        generation=view.generation,
        site_id=view.site_id,
        candidate_key=view.candidate_key,
        title=view.title,
        size_bytes=view.size_bytes,
        imdb_id=view.imdb_id,
        douban_id=view.douban_id,
        seeders=view.seeders,
        score_bps=view.score_bps,
        is_exact_match=view.is_exact_match,
        evidence=view.evidence,
        verification_status=view.verification_status,
        verification_level=view.verification_level,
        verification_error_code=view.verification_error_code,
        created_at=view.created_at,
    )


def _candidate_list_response(view: UnpackMatchCandidateList) -> UnpackMatchCandidateListResponse:
    return UnpackMatchCandidateListResponse(
        item_id=view.item_id,
        generation=view.generation,
        item_version=view.item_version,
        default_candidate_id=view.default_candidate_id,
        candidates=[_candidate_response(item) for item in view.candidates],
    )


def _required_item_version(value: str | None) -> int:
    if value is None:
        raise ApplicationError(
            code="UNPACK_ITEM_IF_MATCH_REQUIRED",
            status=428,
            title="缺少影片项版本",
            detail="写操作必须通过 If-Match 提交当前影片项版本",
        )
    normalized = value.strip()
    if normalized.startswith("W/"):
        normalized = normalized[2:].strip()
    if len(normalized) >= 2 and normalized[0] == '"' and normalized[-1] == '"':
        normalized = normalized[1:-1]
    try:
        version = int(normalized)
    except ValueError as exc:
        raise ApplicationError(
            code="UNPACK_ITEM_IF_MATCH_INVALID",
            status=422,
            title="影片项版本格式无效",
            detail="If-Match 必须是正整数版本号",
        ) from exc
    if version < 1:
        raise ApplicationError(
            code="UNPACK_ITEM_IF_MATCH_INVALID",
            status=422,
            title="影片项版本格式无效",
            detail="If-Match 必须是正整数版本号",
        )
    return version


@router.get("/files/tree/roots", response_model=UnpackTreeRootsResponse)
async def list_unpack_tree_roots(
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(READ_ACCESS)],
) -> UnpackTreeRootsResponse:
    return UnpackTreeRootsResponse(
        items=[
            _tree_entry_response(item)
            for item in unpack_source_scan_service(request).list_tree_roots()
        ]
    )


@router.get("/files/tree", response_model=UnpackTreeResponse)
async def browse_unpack_tree(
    request: Request,
    selection_token: Annotated[str, Query(min_length=1, max_length=8192)],
    _principal: Annotated[AccessPrincipal, Depends(READ_ACCESS)],
) -> UnpackTreeResponse:
    view = unpack_source_scan_service(request).browse_tree(selection_token)
    return UnpackTreeResponse(
        display_path=view.display_path,
        selection_token=view.selection_token,
        entries=[_tree_entry_response(item) for item in view.entries],
    )


@router.post(
    "/unpack/source-scans",
    response_model=UnpackSourceScanResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_unpack_source_scan(
    payload: UnpackSourceScanCreateRequest,
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(WRITE_ACCESS)],
) -> UnpackSourceScanResponse:
    return _source_scan_response(
        unpack_source_scan_service(request).create_scan(
            selection_token=payload.selection_token,
            file_filter=payload.file_filter,
        )
    )


@router.get(
    "/unpack/source-scans/{scan_id}",
    response_model=UnpackSourceScanResponse,
)
async def get_unpack_source_scan(
    scan_id: str,
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(READ_ACCESS)],
) -> UnpackSourceScanResponse:
    return _source_scan_response(unpack_source_scan_service(request).get_scan(scan_id))


@router.get(
    "/unpack/source-scans/{scan_id}/items",
    response_model=UnpackSourceScanItemsResponse,
)
async def list_unpack_source_scan_items(
    scan_id: str,
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(READ_ACCESS)],
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    q: Annotated[str | None, Query(max_length=512)] = None,
    extension: Annotated[str | None, Query(max_length=32)] = None,
    resolution: Annotated[str | None, Query(max_length=16)] = None,
    selected: bool | None = None,
) -> UnpackSourceScanItemsResponse:
    page = unpack_source_scan_service(request).list_items(
        scan_id,
        cursor=cursor,
        limit=limit,
        query=q,
        extension=extension,
        resolution=resolution,
        selected=selected,
    )
    return UnpackSourceScanItemsResponse(
        items=[_source_scan_item_response(item) for item in page.items],
        next_cursor=page.next_cursor,
        has_more=page.has_more,
    )


@router.put(
    "/unpack/source-scans/{scan_id}/selection",
    response_model=UnpackSourceScanSelectionSummaryResponse,
)
async def update_unpack_source_scan_selection(
    scan_id: str,
    payload: UnpackSourceScanSelectionRequest,
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(WRITE_ACCESS)],
) -> UnpackSourceScanSelectionSummaryResponse:
    summary = unpack_source_scan_service(request).update_selection(
        scan_id,
        source_object_keys=tuple(payload.source_object_keys),
        selected=payload.selected,
    )
    return UnpackSourceScanSelectionSummaryResponse(
        discovered_count=summary.discovered_count,
        selected_count=summary.selected_count,
    )


@router.get(
    "/unpack/source-scans/{scan_id}/selection-summary",
    response_model=UnpackSourceScanSelectionSummaryResponse,
)
async def get_unpack_source_scan_selection_summary(
    scan_id: str,
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(READ_ACCESS)],
) -> UnpackSourceScanSelectionSummaryResponse:
    summary = unpack_source_scan_service(request).selection_summary(scan_id)
    return UnpackSourceScanSelectionSummaryResponse(
        discovered_count=summary.discovered_count,
        selected_count=summary.selected_count,
    )


@router.post(
    "/unpack/definitions",
    response_model=UnpackDefinitionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_unpack_definition(
    payload: UnpackDefinitionCreateRequest,
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(WRITE_ACCESS)],
) -> UnpackDefinitionResponse:
    view = unpack_definition_service(request).create(
        UnpackDefinitionCreate(
            name=payload.name,
            trigger_kind=payload.trigger_kind,
            source_kind=payload.source_kind,
            execution_scope_kind=payload.execution_scope_kind,
            source_config=payload.source_config,
            file_filter=payload.file_filter,
            site_ids=tuple(payload.site_ids),
            output_config=payload.output_config,
            retry_enabled=payload.retry_enabled,
            max_retries=payload.max_retries,
            auto_match_threshold_bps=payload.auto_match_threshold_bps,
            cron_expression=payload.cron_expression,
            timezone=payload.timezone,
            source_scan_id=payload.source_scan_id,
        )
    )
    return _definition_response(view)


@router.get("/unpack/definitions", response_model=UnpackDefinitionListResponse)
async def list_unpack_definitions(
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(READ_ACCESS)],
) -> UnpackDefinitionListResponse:
    return UnpackDefinitionListResponse(
        items=[_definition_response(item) for item in unpack_definition_service(request).list()]
    )


@router.get(
    "/unpack/definitions/{definition_id}",
    response_model=UnpackDefinitionResponse,
)
async def get_unpack_definition(
    definition_id: str,
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(READ_ACCESS)],
) -> UnpackDefinitionResponse:
    return _definition_response(unpack_definition_service(request).get(definition_id))


@router.post(
    "/unpack/definitions/{definition_id}/actions",
    response_model=UnpackDefinitionActionResponse,
)
async def act_on_unpack_definition(
    definition_id: str,
    payload: UnpackDefinitionActionRequest,
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(WRITE_ACCESS)],
) -> UnpackDefinitionActionResponse:
    if payload.action != "run":
        raise AssertionError("Pydantic 已限制 action")
    result = unpack_definition_service(request).run(definition_id)
    return UnpackDefinitionActionResponse(
        definition=_definition_response(result.definition),
        execution_id=result.execution_id,
    )


@router.get("/unpack/executions", response_model=UnpackExecutionListResponse)
async def list_unpack_executions(
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(READ_ACCESS)],
    definition_id: str | None = None,
    execution_status: UnpackExecutionStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> UnpackExecutionListResponse:
    items = unpack_execution_query_service(request).list(
        definition_id=definition_id,
        status=execution_status,
        limit=limit,
    )
    return UnpackExecutionListResponse(items=[_execution_response(item) for item in items])


@router.get(
    "/unpack/executions/{execution_id}",
    response_model=UnpackExecutionResponse,
)
async def get_unpack_execution(
    execution_id: str,
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(READ_ACCESS)],
) -> UnpackExecutionResponse:
    return _execution_response(unpack_execution_query_service(request).get(execution_id))


@router.get(
    "/unpack/executions/{execution_id}/items",
    response_model=UnpackExecutionItemListResponse,
)
async def list_unpack_execution_items(
    execution_id: str,
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(READ_ACCESS)],
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    item_status: UnpackItemStatus | None = None,
    q: Annotated[str | None, Query(max_length=512)] = None,
) -> UnpackExecutionItemListResponse:
    page = unpack_execution_query_service(request).list_items(
        execution_id,
        cursor=cursor,
        limit=limit,
        status=item_status,
        query=q,
    )
    return UnpackExecutionItemListResponse(
        items=[_execution_item_response(item) for item in page.items],
        next_cursor=page.next_cursor,
        has_more=page.has_more,
    )


@router.get(
    "/unpack/items/{item_id}/candidates",
    response_model=UnpackMatchCandidateListResponse,
)
async def list_unpack_item_candidates(
    item_id: str,
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(READ_ACCESS)],
) -> UnpackMatchCandidateListResponse:
    return _candidate_list_response(
        unpack_execution_query_service(request).list_candidates(item_id)
    )


@router.put(
    "/unpack/items/{item_id}/review",
    response_model=UnpackReviewResponse,
)
async def review_unpack_item(
    item_id: str,
    payload: UnpackReviewRequest,
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(WRITE_ACCESS)],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> UnpackReviewResponse:
    if idempotency_key is None:
        raise ApplicationError(
            code="UNPACK_REVIEW_IDEMPOTENCY_KEY_REQUIRED",
            status=428,
            title="缺少审核幂等键",
            detail="人工审核必须通过 Idempotency-Key 提交稳定幂等键",
        )
    result = unpack_item_action_service(request).review(
        item_id,
        decision=payload.decision,
        candidate_id=payload.candidate_id,
        generation=payload.generation,
        expected_item_version=_required_item_version(if_match),
        idempotency_key=idempotency_key,
    )
    return UnpackReviewResponse(
        decision_id=result.decision_id,
        item_id=result.item_id,
        decision=result.decision,
        candidate_id=result.candidate_id,
        generation=result.generation,
        item_version=result.item_version,
        item_status=result.item_status,
        decided_at=result.decided_at,
        replayed=result.replayed,
    )


@router.post(
    "/unpack/items/{item_id}/actions",
    response_model=UnpackItemActionResponse,
)
async def act_on_unpack_item(
    item_id: str,
    payload: UnpackItemActionRequest,
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(WRITE_ACCESS)],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> UnpackItemActionResponse:
    if payload.action != "retry_match":
        raise AssertionError("Pydantic 已限制 action")
    result = unpack_item_action_service(request).retry_match(
        item_id,
        expected_item_version=_required_item_version(if_match),
    )
    return UnpackItemActionResponse(
        item_id=result.item_id,
        generation=result.generation,
        retry_count=result.retry_count,
        item_version=result.item_version,
        item_status=result.item_status,
    )
