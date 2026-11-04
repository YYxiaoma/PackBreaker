from __future__ import annotations

import base64
import binascii
from dataclasses import asdict
from datetime import datetime
from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, Depends, Header, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from backend.app.api.dependencies import (
    AccessPrincipal,
    require_admin_csrf_principal,
    require_admin_principal,
    site_service,
    unpack_definition_service,
    unpack_execution_query_service,
    unpack_item_action_service,
    unpack_manual_candidate_service,
    unpack_source_scan_service,
    unpack_template_service,
)
from backend.app.application.errors import ApplicationError
from backend.app.application.unpack_definitions import (
    UnpackDefinitionCreate,
    UnpackDefinitionView,
)
from backend.app.application.unpack_executions import (
    UnpackExecutionItemView,
    UnpackExecutionView,
    UnpackItemDiagnosticsView,
    UnpackMatchCandidateList,
    UnpackMatchCandidateView,
)
from backend.app.application.unpack_manual_torrents import MAX_MANUAL_TORRENT_BYTES
from backend.app.application.unpack_seeding import UnpackSeedingService
from backend.app.application.unpack_source_scans import (
    UnpackSourceScanItemView,
    UnpackSourceScanView,
    UnpackTreeEntryView,
)
from backend.app.application.unpack_templates import (
    UnpackTemplateImportJobView,
)
from backend.app.domain.unpack import (
    UnpackDefinitionStatus,
    UnpackExecutionScopeKind,
    UnpackExecutionStatus,
    UnpackItemStatus,
    UnpackReviewDecision,
    UnpackSourceKind,
    UnpackTaskKind,
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
    task_kind: UnpackTaskKind | None = None
    template_id: str | None = Field(default=None, max_length=36)
    template_version: int | None = Field(default=None, ge=1)
    source_kind: UnpackSourceKind
    execution_scope_kind: UnpackExecutionScopeKind = UnpackExecutionScopeKind.ALL_MATCHING_MEDIA
    source_config: dict[str, Any]
    file_filter: dict[str, Any] = Field(default_factory=dict)
    site_ids: list[str] = Field(min_length=1, max_length=64)
    output_config: dict[str, Any]
    retry_enabled: bool = True
    max_retries: int = Field(default=3, ge=0, le=10)
    auto_match_threshold_bps: int = Field(default=10_000, ge=0, le=10_000)
    auto_match_enabled: bool = True
    cron_expression: str | None = Field(default=None, max_length=160)
    timezone: str | None = Field(default=None, max_length=64)
    source_scan_id: str | None = Field(default=None, max_length=36)


class UnpackTemplateResponse(BaseModel):
    id: str
    name: str
    description: str | None
    current_version: int
    status: str
    expected_seed_count: int
    seed_count: int
    mapped_media_count: int
    site_keys: list[str]
    site_ids: list[str]
    content_digest: str
    source_provenance: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class UnpackTemplateListResponse(BaseModel):
    items: list[UnpackTemplateResponse]


class UnpackTemplateSeedMappingRequest(BaseModel):
    torrent_id: str = Field(min_length=1, max_length=255)
    collection_root_name: str = Field(min_length=1, max_length=255)
    relative_directory: str = Field(min_length=1, max_length=1024)
    relative_file_name: str | None = Field(default=None, max_length=255)
    media_title: str = Field(min_length=1, max_length=512)
    confidence_bps: int = Field(default=0, ge=0, le=10_000)
    source_execution_id: str | None = Field(default=None, max_length=36)


class UnpackTemplateImportRequest(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    template_id: str | None = Field(default=None, max_length=36)
    site_id: str = Field(min_length=1, max_length=36)
    torrent_ids: list[str] = Field(min_length=1, max_length=1000)
    source_label: str | None = Field(default=None, max_length=200)
    seed_mappings: list[UnpackTemplateSeedMappingRequest] = Field(
        default_factory=list, max_length=5000
    )


class UnpackTemplateImportJobResponse(BaseModel):
    id: str
    template_id: str
    template_version: int
    status: str
    total_count: int
    processed_count: int
    success_count: int
    failure_count: int
    errors: list[dict[str, Any]]
    created_at: datetime
    updated_at: datetime


class UnpackDefinitionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    trigger_kind: UnpackTriggerKind
    task_kind: UnpackTaskKind
    template_id: str | None
    template_version: int | None
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
    auto_match_enabled: bool
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


class UnpackRetryEligibleItemsRequest(BaseModel):
    action: Literal["retry_eligible_items"]
    item_statuses: list[UnpackItemStatus] = Field(min_length=1, max_length=8)


class UnpackRetryEligibleItemsResponse(BaseModel):
    execution_id: str
    retried_count: int
    skipped_unsafe_count: int


class UnpackExecutionResponse(BaseModel):
    id: str
    definition_id: str
    trigger: str
    status: UnpackExecutionStatus
    discovery_complete: bool
    discovery_cursor: str | None
    total_count: int
    matched_auto_count: int
    no_match_count: int
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
    highest_candidate_score_bps: int | None
    retry_count: int
    content_verification_level: str | None
    torrent_metainfo_digest: str | None
    auxiliary_state: dict[str, Any] | None
    review_allowed: bool
    has_external_operations: bool
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


class UnpackOperationDiagnosticResponse(BaseModel):
    operation_type: str
    status: str
    last_error_code: str | None
    downloader_id: str | None
    torrent_hash: str | None
    target_path: str | None
    last_observed_state: str | None
    recorded_file_type: str | None
    recorded_device: int | None
    recorded_inode: int | None
    recorded_size: int | None
    updated_at: datetime


class UnpackItemDiagnosticsResponse(BaseModel):
    item_id: str
    status: UnpackItemStatus
    item_version: int
    execution_stage: str | None
    last_error_code: str | None
    plan_status: Literal["NOT_CREATED", "READY", "BLOCKED", "INVALID"]
    plan_blocked_reasons: list[str]
    acceptance_read_only: bool
    last_error_message: str | None
    requires_manual_reconciliation: bool
    reconciliation_reasons: list[str]
    downloader_state_live_checked: bool
    filesystem_state_live_checked: bool
    operations_truncated: bool
    operations: list[UnpackOperationDiagnosticResponse]


class UnpackLiveClientReadResponse(BaseModel):
    status: Literal[
        "NO_ADD_EVIDENCE", "UNRESOLVED_ADD", "MATCHED", "MISSING", "OWNERSHIP_MISMATCH", "UNKNOWN"
    ]
    downloader_id: str | None = None
    client_kind: str | None = None
    torrent_hash: str | None = None
    current_state: str | None = None
    progress: float | None = None
    ownership_verified: bool = False
    save_path_matches: bool = False
    error_code: str | None = None


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


class ManualTorrentFileResponse(BaseModel):
    path: str
    size_bytes: int


class ManualCandidatePreviewResponse(BaseModel):
    site_id: str
    site_name: str
    source_kind: Literal["URL", "FILE"]
    source_title: str
    source_size_bytes: int
    candidate_key: str
    candidate_title: str
    torrent_name: str
    torrent_kind: str
    v1_info_hash: str | None
    v2_info_hash: str | None
    metainfo_digest: str
    payload_sha256: str
    total_size_bytes: int
    file_count: int
    files: list[ManualTorrentFileResponse]
    score_bps: int
    evidence: dict[str, Any]
    hard_conflicts: list[str]
    seeders: int | None
    imdb_id: str | None
    douban_id: str | None


class ManualCandidateAddResponse(BaseModel):
    candidate_id: str
    candidate_key: str
    generation: int
    item_version: int


class ManualCandidateInputRequest(BaseModel):
    site_id: str = Field(min_length=1, max_length=36)
    expected_generation: int = Field(ge=0)
    expected_item_version: int = Field(ge=1)
    source_url: str | None = Field(default=None, max_length=4096)
    torrent_file_base64: str | None = Field(
        default=None,
        max_length=((MAX_MANUAL_TORRENT_BYTES + 2) // 3) * 4,
    )
    torrent_file_name: str | None = Field(default=None, max_length=255)
    expected_metainfo_digest: str | None = Field(default=None, min_length=64, max_length=64)
    expected_payload_sha256: str | None = Field(default=None, min_length=64, max_length=64)


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
        task_kind=view.task_kind,
        template_id=view.template_id,
        template_version=view.template_version,
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
        auto_match_enabled=view.auto_match_enabled,
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
        no_match_count=view.no_match_count,
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
        highest_candidate_score_bps=view.highest_candidate_score_bps,
        retry_count=view.retry_count,
        content_verification_level=view.content_verification_level,
        torrent_metainfo_digest=view.torrent_metainfo_digest,
        auxiliary_state=view.auxiliary_state,
        review_allowed=view.review_allowed,
        has_external_operations=view.has_external_operations,
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


def _required_definition_version(value: str | None) -> int:
    if value is None:
        raise ApplicationError(
            code="UNPACK_VERSION_REQUIRED",
            status=428,
            title="缺少版本条件",
            detail="写操作必须通过 If-Match 提交当前记录版本",
        )
    raw = value.strip().strip('"')
    if not raw.isdigit() or int(raw) < 1:
        raise ApplicationError(
            code="UNPACK_VERSION_INVALID",
            status=422,
            title="版本条件无效",
            detail="If-Match 必须为正整数版本",
        )
    return int(raw)


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


@router.get("/unpack/templates", response_model=UnpackTemplateListResponse)
def list_unpack_templates(
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(READ_ACCESS)],
) -> UnpackTemplateListResponse:
    service = unpack_template_service(request)
    return UnpackTemplateListResponse(
        items=[
            UnpackTemplateResponse(
                id=item.id,
                name=item.name,
                description=item.description,
                current_version=item.current_version,
                status=item.status,
                expected_seed_count=item.expected_seed_count,
                seed_count=item.seed_count,
                mapped_media_count=item.mapped_media_count,
                site_keys=list(item.site_keys),
                site_ids=list(item.site_ids),
                content_digest=item.content_digest,
                source_provenance=item.source_provenance,
                created_at=item.created_at,
                updated_at=item.updated_at,
            )
            for item in service.list_templates(site_service(request))
        ]
    )


@router.post(
    "/unpack/templates/import",
    response_model=UnpackTemplateImportJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_unpack_template_import(
    payload: UnpackTemplateImportRequest,
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(WRITE_ACCESS)],
) -> UnpackTemplateImportJobResponse:
    job = unpack_template_service(request).create_import_job(
        site_service=site_service(request),
        site_config_id=payload.site_id,
        torrent_ids=tuple(payload.torrent_ids),
        name=payload.name,
        description=payload.description,
        template_id=payload.template_id,
        source_label=payload.source_label,
        seed_mappings=tuple(item.model_dump() for item in payload.seed_mappings),
    )
    return _template_import_job_response(job)


@router.get(
    "/unpack/templates/import-jobs/{job_id}",
    response_model=UnpackTemplateImportJobResponse,
)
def get_unpack_template_import_job(
    job_id: str,
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(READ_ACCESS)],
) -> UnpackTemplateImportJobResponse:
    return _template_import_job_response(unpack_template_service(request).get_import_job(job_id))


def _template_import_job_response(
    view: UnpackTemplateImportJobView,
) -> UnpackTemplateImportJobResponse:
    return UnpackTemplateImportJobResponse(
        id=view.id,
        template_id=view.template_id,
        template_version=view.template_version,
        status=view.status,
        total_count=view.total_count,
        processed_count=view.processed_count,
        success_count=view.success_count,
        failure_count=view.failure_count,
        errors=list(view.errors),
        created_at=view.created_at,
        updated_at=view.updated_at,
    )


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
    source_config = dict(payload.source_config)
    frozen_downloader_sources: tuple[dict[str, Any], ...] = ()
    if (
        payload.task_kind is UnpackTaskKind.TEMPLATE
        and payload.source_kind is UnpackSourceKind.DOWNLOADER
    ):
        source_config.pop("_frozen_downloader_sources", None)
        frozen_downloader_sources = tuple(
            await request.app.state.unpack_monitor_service.freeze_downloader_sources(source_config)
        )
        if not frozen_downloader_sources:
            raise ApplicationError(
                code="UNPACK_TEMPLATE_SOURCE_NOT_FOUND",
                status=422,
                title="模板拆包来源无效",
                detail="所选下载器中没有找到已完成且符合条件的种子，请重新选择来源种子",
            )
    view = unpack_definition_service(request).create(
        UnpackDefinitionCreate(
            name=payload.name,
            trigger_kind=payload.trigger_kind,
            task_kind=payload.task_kind,
            template_id=payload.template_id,
            template_version=payload.template_version,
            source_kind=payload.source_kind,
            execution_scope_kind=payload.execution_scope_kind,
            source_config=source_config,
            file_filter=payload.file_filter,
            site_ids=tuple(payload.site_ids),
            output_config=payload.output_config,
            retry_enabled=payload.retry_enabled,
            max_retries=payload.max_retries,
            auto_match_threshold_bps=payload.auto_match_threshold_bps,
            auto_match_enabled=payload.auto_match_enabled,
            cron_expression=payload.cron_expression,
            timezone=payload.timezone,
            source_scan_id=payload.source_scan_id,
            frozen_downloader_sources=frozen_downloader_sources,
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


@router.put(
    "/unpack/definitions/{definition_id}",
    response_model=UnpackDefinitionResponse,
)
async def update_unpack_definition(
    definition_id: str,
    payload: UnpackDefinitionCreateRequest,
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(WRITE_ACCESS)],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> UnpackDefinitionResponse:
    source_config = dict(payload.source_config)
    frozen_downloader_sources: tuple[dict[str, Any], ...] = ()
    if (
        payload.task_kind is UnpackTaskKind.TEMPLATE
        and payload.source_kind is UnpackSourceKind.DOWNLOADER
    ):
        source_config.pop("_frozen_downloader_sources", None)
        frozen_downloader_sources = tuple(
            await request.app.state.unpack_monitor_service.freeze_downloader_sources(source_config)
        )
        if not frozen_downloader_sources:
            raise ApplicationError(
                code="UNPACK_TEMPLATE_SOURCE_NOT_FOUND",
                status=422,
                title="模板拆包来源无效",
                detail="所选下载器中没有找到已完成且符合条件的种子，请重新选择来源种子",
            )
    view = unpack_definition_service(request).update(
        definition_id,
        UnpackDefinitionCreate(
            name=payload.name,
            trigger_kind=payload.trigger_kind,
            task_kind=payload.task_kind,
            template_id=payload.template_id,
            template_version=payload.template_version,
            source_kind=payload.source_kind,
            execution_scope_kind=payload.execution_scope_kind,
            source_config=source_config,
            file_filter=payload.file_filter,
            site_ids=tuple(payload.site_ids),
            output_config=payload.output_config,
            retry_enabled=payload.retry_enabled,
            max_retries=payload.max_retries,
            auto_match_threshold_bps=payload.auto_match_threshold_bps,
            auto_match_enabled=payload.auto_match_enabled,
            cron_expression=payload.cron_expression,
            timezone=payload.timezone,
            source_scan_id=payload.source_scan_id,
            frozen_downloader_sources=frozen_downloader_sources,
        ),
        expected_version=_required_definition_version(if_match),
    )
    return _definition_response(view)


@router.delete(
    "/unpack/definitions/{definition_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_unpack_definition(
    definition_id: str,
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(WRITE_ACCESS)],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> None:
    unpack_definition_service(request).delete(
        definition_id, expected_version=_required_definition_version(if_match)
    )


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


@router.post(
    "/unpack/executions/{execution_id}/actions",
    response_model=UnpackRetryEligibleItemsResponse,
)
async def act_on_unpack_execution(
    execution_id: str,
    payload: UnpackRetryEligibleItemsRequest,
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(WRITE_ACCESS)],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> UnpackRetryEligibleItemsResponse:
    if payload.action != "retry_eligible_items":
        raise AssertionError("Pydantic 已限制 action")
    result = unpack_item_action_service(request).retry_eligible_items(
        execution_id,
        expected_execution_version=_required_definition_version(if_match),
        selected_statuses=tuple(payload.item_statuses),
    )
    return UnpackRetryEligibleItemsResponse(
        execution_id=result.execution_id,
        retried_count=result.retried_count,
        skipped_unsafe_count=result.skipped_unsafe_count,
    )


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
    "/unpack/items/{item_id}/diagnostics/live",
    response_model=UnpackLiveClientReadResponse,
)
async def read_unpack_item_downloader_state(
    item_id: str,
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(READ_ACCESS)],
) -> UnpackLiveClientReadResponse:
    """One explicitly requested GET-only client probe; no automatic reconciliation."""
    seeding = cast(UnpackSeedingService, request.app.state.unpack_seeding_service)
    result = await seeding.inspect_client_readonly(item_id)
    return UnpackLiveClientReadResponse.model_validate(result, from_attributes=True)


@router.get(
    "/unpack/items/{item_id}/diagnostics",
    response_model=UnpackItemDiagnosticsResponse,
)
async def get_unpack_item_diagnostics(
    item_id: str,
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(READ_ACCESS)],
) -> UnpackItemDiagnosticsResponse:
    view: UnpackItemDiagnosticsView = unpack_execution_query_service(request).item_diagnostics(
        item_id
    )
    return UnpackItemDiagnosticsResponse(
        item_id=view.item_id,
        status=view.status,
        item_version=view.item_version,
        execution_stage=view.execution_stage,
        plan_status=cast(Literal["NOT_CREATED", "READY", "BLOCKED", "INVALID"], view.plan_status),
        plan_blocked_reasons=list(view.plan_blocked_reasons),
        acceptance_read_only=view.acceptance_read_only,
        last_error_code=view.last_error_code,
        last_error_message=view.last_error_message,
        requires_manual_reconciliation=view.requires_manual_reconciliation,
        reconciliation_reasons=list(view.reconciliation_reasons),
        downloader_state_live_checked=view.downloader_state_live_checked,
        filesystem_state_live_checked=view.filesystem_state_live_checked,
        operations_truncated=view.operations_truncated,
        operations=[
            UnpackOperationDiagnosticResponse.model_validate(o, from_attributes=True)
            for o in view.operations
        ],
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


@router.post(
    "/unpack/items/{item_id}/manual-candidate-preview",
    response_model=ManualCandidatePreviewResponse,
)
async def preview_manual_unpack_candidate(
    item_id: str,
    payload: ManualCandidateInputRequest,
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(WRITE_ACCESS)],
) -> ManualCandidatePreviewResponse:
    content = _decode_manual_torrent_upload(payload)
    result = await unpack_manual_candidate_service(request).preview(
        item_id,
        site_id=payload.site_id,
        expected_generation=payload.expected_generation,
        expected_item_version=payload.expected_item_version,
        source_url=payload.source_url,
        torrent_content=content,
    )
    return ManualCandidatePreviewResponse(**asdict(result))


@router.post(
    "/unpack/items/{item_id}/manual-candidates",
    response_model=ManualCandidateAddResponse,
    status_code=status.HTTP_201_CREATED,
)
async def add_manual_unpack_candidate(
    item_id: str,
    payload: ManualCandidateInputRequest,
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(WRITE_ACCESS)],
) -> ManualCandidateAddResponse:
    content = _decode_manual_torrent_upload(payload)
    if payload.expected_metainfo_digest is None or payload.expected_payload_sha256 is None:
        raise ApplicationError(
            code="UNPACK_MANUAL_CANDIDATE_DIGEST_REQUIRED",
            status=422,
            title="缺少种子预览摘要",
            detail="请先预览种子，并基于预览结果确认添加",
        )
    result = await unpack_manual_candidate_service(request).add(
        item_id,
        site_id=payload.site_id,
        expected_generation=payload.expected_generation,
        expected_item_version=payload.expected_item_version,
        expected_metainfo_digest=payload.expected_metainfo_digest,
        expected_payload_sha256=payload.expected_payload_sha256,
        source_url=payload.source_url,
        torrent_content=content,
    )
    return ManualCandidateAddResponse(**asdict(result))


def _decode_manual_torrent_upload(payload: ManualCandidateInputRequest) -> bytes | None:
    encoded = payload.torrent_file_base64
    if encoded is None:
        if payload.torrent_file_name is not None:
            raise ApplicationError(
                code="UNPACK_MANUAL_TORRENT_INVALID",
                status=422,
                title="种子文件无效",
                detail="未收到种子文件内容，请重新选择文件",
            )
        return None
    if not payload.torrent_file_name or not payload.torrent_file_name.casefold().endswith(
        ".torrent"
    ):
        raise ApplicationError(
            code="UNPACK_MANUAL_TORRENT_INVALID",
            status=422,
            title="种子文件无效",
            detail="请上传 .torrent 文件",
        )
    if len(encoded) > ((MAX_MANUAL_TORRENT_BYTES + 2) // 3) * 4:
        raise ApplicationError(
            code="UNPACK_MANUAL_TORRENT_TOO_LARGE",
            status=413,
            title="种子文件过大",
            detail="手动上传的 .torrent 文件不能超过 32 MiB",
        )
    try:
        content = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ApplicationError(
            code="UNPACK_MANUAL_TORRENT_INVALID",
            status=422,
            title="种子文件无效",
            detail="上传的种子文件编码无效，请重新选择文件",
        ) from exc
    if not content or len(content) > MAX_MANUAL_TORRENT_BYTES:
        raise ApplicationError(
            code="UNPACK_MANUAL_TORRENT_TOO_LARGE",
            status=413,
            title="种子文件无效或过大",
            detail="种子文件为空或超过 32 MiB 限制",
        )
    return content


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


@router.delete("/unpack/items/{item_id}", status_code=204)
async def delete_unpack_execution_item(
    item_id: str,
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(WRITE_ACCESS)],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> None:
    unpack_item_action_service(request).delete_item(
        item_id,
        expected_item_version=_required_item_version(if_match),
    )
