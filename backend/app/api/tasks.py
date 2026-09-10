from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from backend.app.api.dependencies import (
    AccessPrincipal,
    require_admin_csrf_or_scope,
    require_admin_or_scope,
    task_analysis_service,
)
from backend.app.application.errors import ApplicationError
from backend.app.application.tasks import (
    ExecutionGateView,
    PreflightView,
    ReviewVerificationView,
    TaskCandidateView,
    TaskReviewView,
    TaskUnitView,
    TaskView,
)
from backend.app.domain.auth import ApiScope
from backend.app.domain.review import ManualReviewMapping
from backend.app.domain.task_state import TaskStatus

router = APIRouter(tags=["tasks"])
TASKS_READ_ACCESS = require_admin_or_scope(ApiScope.TASKS_READ)
TASKS_WRITE_ACCESS = require_admin_csrf_or_scope(ApiScope.TASKS_WRITE)


class TaskCreateRequest(BaseModel):
    task_type: str = Field(min_length=1, max_length=64)
    source_downloader_id: str = Field(min_length=1, max_length=36)
    source_hash: str = Field(min_length=1, max_length=128)
    normalized_unit_key: str = Field(min_length=1, max_length=512)


class TaskResponse(BaseModel):
    id: str
    type: str
    source_downloader_id: str
    source_hash: str
    normalized_unit_key: str
    status: TaskStatus
    error_code: str | None
    version: int
    created_at: datetime
    updated_at: datetime


class TaskListResponse(BaseModel):
    items: list[TaskResponse]


class TaskCreateResponse(BaseModel):
    item: TaskResponse
    created: bool


class TaskActionRequest(BaseModel):
    action: Literal["analyze"]
    source_root: str = Field(min_length=1, max_length=4096)


class TaskUnitResponse(BaseModel):
    id: str
    normalized_unit_key: str
    kind: str
    source_root: str
    source_relative_path: str
    length: int
    source_inventory_digest: str
    descriptor: dict[str, Any]
    discovered_at: datetime


class TaskUnitListResponse(BaseModel):
    items: list[TaskUnitResponse]


class TaskCandidateResponse(BaseModel):
    id: str
    snapshot_id: str
    normalized_unit_key: str
    site_id: str
    torrent_id: str
    display_name: str
    score: float
    rejected: bool
    selected_for_verification: bool
    verification_level: str | None
    metainfo_digest: str | None
    error_code: str | None
    evidence: dict[str, Any]
    created_at: datetime


class TaskCandidateListResponse(BaseModel):
    items: list[TaskCandidateResponse]


class PreflightResponse(BaseModel):
    id: str
    snapshot_digest: str
    current: bool
    stale_reasons: list[str]
    created_at: datetime
    payload: dict[str, Any]


class PreflightCurrentResponse(BaseModel):
    snapshot_digest: str
    current: bool
    stale_reasons: list[str]


class ManualReviewMappingRequest(BaseModel):
    torrent_path: str = Field(min_length=1, max_length=4096)
    source_relative_path: str = Field(min_length=1, max_length=4096)


class TaskReviewRequest(BaseModel):
    expected_version: int = Field(ge=0)
    approved_candidate_id: str | None = Field(default=None, max_length=36)
    rejected_candidate_ids: list[str] = Field(default_factory=list, max_length=500)
    manual_mappings: list[ManualReviewMappingRequest] = Field(default_factory=list, max_length=5000)
    note: str | None = Field(default=None, max_length=2000)


class ManualReviewMappingResponse(BaseModel):
    torrent_path: str
    source_relative_path: str


class TaskReviewResponse(BaseModel):
    id: str
    task_id: str
    task_unit_id: str
    preflight_snapshot_id: str
    approved_candidate_id: str | None
    rejected_candidate_ids: list[str]
    manual_mappings: list[ManualReviewMappingResponse]
    note: str | None
    requires_reverification: bool
    execution_allowed: bool
    actor_kind: str
    version: int
    created_at: datetime


class TaskReviewActionRequest(BaseModel):
    action: Literal["reverify"]


class ReviewVerificationResponse(BaseModel):
    id: str
    review_revision_id: str
    review_version: int
    candidate_id: str
    verification_digest: str
    verification_level: str
    metainfo_digest: str
    execution_allowed: bool
    created_at: datetime


class ExecutionGateResponse(BaseModel):
    id: str
    gate_digest: str
    eligible: bool
    current: bool
    client_check_required: bool
    verification_level: str | None
    verification_source: str | None
    blocked_reasons: list[str]
    preflight_stale_reasons: list[str]
    candidate_id: str | None
    metainfo_digest: str | None
    review_revision_id: str
    review_version: int
    side_effects_started: bool
    created_at: datetime


@router.get("/tasks", response_model=TaskListResponse)
async def list_tasks(
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(TASKS_READ_ACCESS)],
    status: TaskStatus | None = None,
    limit: int = 100,
) -> TaskListResponse:
    bounded_limit = max(1, min(limit, 500))
    items = task_analysis_service(request).list_tasks(status=status, limit=bounded_limit)
    return TaskListResponse(items=[_task_response(item) for item in items])


@router.post("/tasks", response_model=TaskCreateResponse)
async def create_task(
    request: Request,
    payload: TaskCreateRequest,
    _principal: Annotated[AccessPrincipal, Depends(TASKS_WRITE_ACCESS)],
) -> TaskCreateResponse:
    result = task_analysis_service(request).create_task(
        task_type=payload.task_type,
        source_downloader_id=payload.source_downloader_id,
        source_hash=payload.source_hash,
        normalized_unit_key=payload.normalized_unit_key,
    )
    return TaskCreateResponse(item=_task_response(result.task), created=result.created)


@router.get("/tasks/{task_id}", response_model=TaskResponse)
async def get_task(
    task_id: str,
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(TASKS_READ_ACCESS)],
) -> TaskResponse:
    return _task_response(task_analysis_service(request).get_task(task_id))


@router.get("/tasks/{task_id}/units", response_model=TaskUnitListResponse)
async def list_task_units(
    task_id: str,
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(TASKS_READ_ACCESS)],
) -> TaskUnitListResponse:
    return TaskUnitListResponse(
        items=[_unit_response(item) for item in task_analysis_service(request).list_units(task_id)]
    )


@router.get("/tasks/{task_id}/candidates", response_model=TaskCandidateListResponse)
async def list_task_candidates(
    task_id: str,
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(TASKS_READ_ACCESS)],
) -> TaskCandidateListResponse:
    return TaskCandidateListResponse(
        items=[
            _candidate_response(item)
            for item in task_analysis_service(request).list_candidates(task_id)
        ]
    )


@router.get("/tasks/{task_id}/preflight", response_model=PreflightResponse)
async def get_task_preflight(
    task_id: str,
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(TASKS_READ_ACCESS)],
) -> PreflightResponse:
    return _preflight_view_response(task_analysis_service(request).latest_preflight(task_id))


@router.get("/tasks/{task_id}/preflight/current", response_model=PreflightCurrentResponse)
async def get_task_preflight_current(
    task_id: str,
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(TASKS_READ_ACCESS)],
) -> PreflightCurrentResponse:
    preflight = task_analysis_service(request).latest_preflight(task_id)
    return PreflightCurrentResponse(
        snapshot_digest=preflight.snapshot_digest,
        current=preflight.current,
        stale_reasons=list(preflight.stale_reasons),
    )


@router.post("/tasks/{task_id}/actions", response_model=PreflightResponse)
async def task_action(
    task_id: str,
    request: Request,
    payload: TaskActionRequest,
    _principal: Annotated[AccessPrincipal, Depends(TASKS_WRITE_ACCESS)],
) -> PreflightResponse:
    service = task_analysis_service(request)
    await service.analyze(
        task_id,
        source_root=payload.source_root,
    )
    return _preflight_view_response(service.latest_preflight(task_id))


@router.get("/task-units/{unit_id}/decision", response_model=TaskReviewResponse)
async def get_task_unit_decision(
    unit_id: str,
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(TASKS_READ_ACCESS)],
) -> TaskReviewResponse:
    return _review_response(task_analysis_service(request).get_review(unit_id))


@router.post("/task-units/{unit_id}/decision", response_model=TaskReviewResponse)
async def submit_task_unit_decision(
    unit_id: str,
    request: Request,
    payload: TaskReviewRequest,
    principal: Annotated[AccessPrincipal, Depends(TASKS_WRITE_ACCESS)],
) -> TaskReviewResponse:
    try:
        mappings = tuple(
            ManualReviewMapping(item.torrent_path, item.source_relative_path)
            for item in payload.manual_mappings
        )
    except ValueError as exc:
        raise ApplicationError(
            code="REVIEW_INPUT_INVALID",
            status=422,
            title="审核输入无效",
            detail=str(exc),
        ) from exc
    result = task_analysis_service(request).submit_review(
        unit_id,
        expected_version=payload.expected_version,
        approved_candidate_id=payload.approved_candidate_id,
        rejected_candidate_ids=tuple(payload.rejected_candidate_ids),
        manual_mappings=mappings,
        note=payload.note,
        actor_kind=principal.kind,
        actor_id=principal.subject_id,
    )
    return _review_response(result)


@router.get(
    "/task-units/{unit_id}/decision/verification",
    response_model=ReviewVerificationResponse,
)
async def get_task_unit_review_verification(
    unit_id: str,
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(TASKS_READ_ACCESS)],
) -> ReviewVerificationResponse:
    return _review_verification_response(
        task_analysis_service(request).get_review_verification(unit_id)
    )


@router.post(
    "/task-units/{unit_id}/decision/actions",
    response_model=ReviewVerificationResponse,
)
async def task_unit_review_action(
    unit_id: str,
    request: Request,
    payload: TaskReviewActionRequest,
    _principal: Annotated[AccessPrincipal, Depends(TASKS_WRITE_ACCESS)],
) -> ReviewVerificationResponse:
    if payload.action == "reverify":
        return _review_verification_response(
            await task_analysis_service(request).reverify_review(unit_id)
        )
    raise AssertionError("未覆盖的审核动作")


@router.get(
    "/task-units/{unit_id}/execution-gate",
    response_model=ExecutionGateResponse,
)
async def get_task_unit_execution_gate(
    unit_id: str,
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(TASKS_READ_ACCESS)],
) -> ExecutionGateResponse:
    return _execution_gate_response(task_analysis_service(request).get_execution_gate(unit_id))


@router.post(
    "/task-units/{unit_id}/execution-gate",
    response_model=ExecutionGateResponse,
)
async def refresh_task_unit_execution_gate(
    unit_id: str,
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(TASKS_WRITE_ACCESS)],
) -> ExecutionGateResponse:
    return _execution_gate_response(task_analysis_service(request).refresh_execution_gate(unit_id))


def _task_response(item: TaskView) -> TaskResponse:
    return TaskResponse(
        id=item.id,
        type=item.type,
        source_downloader_id=item.source_downloader_id,
        source_hash=item.source_hash,
        normalized_unit_key=item.normalized_unit_key,
        status=TaskStatus(item.status),
        error_code=item.error_code,
        version=item.version,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


def _unit_response(item: TaskUnitView) -> TaskUnitResponse:
    return TaskUnitResponse(
        id=item.id,
        normalized_unit_key=item.normalized_unit_key,
        kind=item.kind,
        source_root=item.source_root,
        source_relative_path=item.source_relative_path,
        length=item.length,
        source_inventory_digest=item.source_inventory_digest,
        descriptor=item.descriptor,
        discovered_at=item.discovered_at,
    )


def _candidate_response(item: TaskCandidateView) -> TaskCandidateResponse:
    return TaskCandidateResponse(
        id=item.id,
        snapshot_id=item.snapshot_id,
        normalized_unit_key=item.normalized_unit_key,
        site_id=item.site_id,
        torrent_id=item.torrent_id,
        display_name=item.display_name,
        score=item.score,
        rejected=item.rejected,
        selected_for_verification=item.selected_for_verification,
        verification_level=item.verification_level,
        metainfo_digest=item.metainfo_digest,
        error_code=item.error_code,
        evidence=item.evidence,
        created_at=item.created_at,
    )


def _preflight_view_response(item: PreflightView) -> PreflightResponse:
    return PreflightResponse(
        id=item.id,
        snapshot_digest=item.snapshot_digest,
        current=item.current,
        stale_reasons=list(item.stale_reasons),
        created_at=item.created_at,
        payload=item.payload,
    )


def _review_response(item: TaskReviewView) -> TaskReviewResponse:
    return TaskReviewResponse(
        id=item.id,
        task_id=item.task_id,
        task_unit_id=item.task_unit_id,
        preflight_snapshot_id=item.preflight_snapshot_id,
        approved_candidate_id=item.approved_candidate_id,
        rejected_candidate_ids=list(item.rejected_candidate_ids),
        manual_mappings=[
            ManualReviewMappingResponse(
                torrent_path=mapping.torrent_path,
                source_relative_path=mapping.source_relative_path,
            )
            for mapping in item.manual_mappings
        ],
        note=item.note,
        requires_reverification=item.requires_reverification,
        execution_allowed=item.execution_allowed,
        actor_kind=item.actor_kind,
        version=item.version,
        created_at=item.created_at,
    )


def _review_verification_response(item: ReviewVerificationView) -> ReviewVerificationResponse:
    return ReviewVerificationResponse(
        id=item.id,
        review_revision_id=item.review_revision_id,
        review_version=item.review_version,
        candidate_id=item.candidate_id,
        verification_digest=item.verification_digest,
        verification_level=item.verification_level,
        metainfo_digest=item.metainfo_digest,
        execution_allowed=item.execution_allowed,
        created_at=item.created_at,
    )


def _execution_gate_response(item: ExecutionGateView) -> ExecutionGateResponse:
    return ExecutionGateResponse(
        id=item.id,
        gate_digest=item.gate_digest,
        eligible=item.eligible,
        current=item.current,
        client_check_required=item.client_check_required,
        verification_level=item.verification_level,
        verification_source=item.verification_source,
        blocked_reasons=list(item.blocked_reasons),
        preflight_stale_reasons=list(item.preflight_stale_reasons),
        candidate_id=item.candidate_id,
        metainfo_digest=item.metainfo_digest,
        review_revision_id=item.review_revision_id,
        review_version=item.review_version,
        side_effects_started=item.side_effects_started,
        created_at=item.created_at,
    )
