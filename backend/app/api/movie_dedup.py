import asyncio
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from backend.app.api.dependencies import (
    AccessPrincipal,
    movie_dedup_service,
    require_admin_csrf_principal,
    require_admin_principal,
)
from backend.app.application.movie_dedup import (
    MovieDedupJobCreate,
    MovieDedupJobView,
    MovieDedupPairView,
)
from backend.app.domain.movie_dedup import (
    MovieDedupCrossFilesystemPolicy,
    MovieDedupMode,
)
from backend.app.domain.task_definition import DEFAULT_VIDEO_EXTENSIONS

router = APIRouter(tags=["movie-dedup"])
READ_ACCESS = require_admin_principal
WRITE_ACCESS = require_admin_csrf_principal


class MovieDedupPrecheckRequest(BaseModel):
    source_root: str = Field(min_length=1, max_length=4096)
    target_root: str = Field(min_length=1, max_length=4096)
    mode: MovieDedupMode = MovieDedupMode.AUTO
    cross_filesystem_policy: MovieDedupCrossFilesystemPolicy = MovieDedupCrossFilesystemPolicy.STOP


class MovieDedupPrecheckResponse(BaseModel):
    source_root: str
    target_root: str
    source_device: int
    target_device: int
    same_filesystem: bool
    resolved_action: str
    blocked_reasons: list[str]


class MovieDedupJobCreateRequest(MovieDedupPrecheckRequest):
    name: str = Field(min_length=1, max_length=120)
    include_subdirectories: bool = True
    min_size_bytes: int = Field(default=0, ge=0)
    video_extensions: list[str] = Field(
        default_factory=lambda: list(DEFAULT_VIDEO_EXTENSIONS), min_length=1, max_length=64
    )


class MovieDedupJobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    source_root: str
    target_root: str
    mode: str
    cross_filesystem_policy: str
    include_subdirectories: bool
    min_size_bytes: int
    video_extensions: list[str]
    status: str
    phase: str
    source_scan_cursor: str | None
    target_scan_cursor: str | None
    source_file_count: int
    target_file_count: int
    candidate_count: int
    verified_count: int
    deduplicated_count: int
    failed_count: int
    logical_duplicate_bytes: int
    estimated_reclaimable_bytes: int
    version: int
    error_code: str | None
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None


class MovieDedupJobListResponse(BaseModel):
    items: list[MovieDedupJobResponse]


class MovieDedupPairResponse(BaseModel):
    id: str
    source_relative_path: str
    target_relative_path: str
    source_media_metadata: dict[str, object]
    target_media_metadata: dict[str, object]
    source_size_bytes: int
    target_size_bytes: int
    source_device: int
    target_device: int
    source_inode: int
    target_inode: int
    source_link_count: int
    target_link_count: int
    source_mtime_ns: str
    target_mtime_ns: str
    source_sha256: str | None
    target_sha256: str | None
    metadata_match: bool
    size_match: bool
    quick_hash_match: bool
    full_hash_match: bool
    status: str
    resolved_action: str
    estimated_reclaimable_bytes: int
    error_code: str | None
    error_message: str | None


class MovieDedupPairListResponse(BaseModel):
    items: list[MovieDedupPairResponse]


class MovieDedupExecuteRequest(BaseModel):
    pair_ids: list[str] = Field(min_length=1, max_length=500)


def _job_response(view: MovieDedupJobView) -> MovieDedupJobResponse:
    return MovieDedupJobResponse(
        id=view.id,
        name=view.name,
        source_root=view.source_root,
        target_root=view.target_root,
        mode=view.mode.value,
        cross_filesystem_policy=view.cross_filesystem_policy.value,
        include_subdirectories=view.include_subdirectories,
        min_size_bytes=view.min_size_bytes,
        video_extensions=list(view.video_extensions),
        status=view.status.value,
        phase=view.phase.value,
        source_scan_cursor=view.source_scan_cursor,
        target_scan_cursor=view.target_scan_cursor,
        source_file_count=view.source_file_count,
        target_file_count=view.target_file_count,
        candidate_count=view.candidate_count,
        verified_count=view.verified_count,
        deduplicated_count=view.deduplicated_count,
        failed_count=view.failed_count,
        logical_duplicate_bytes=view.logical_duplicate_bytes,
        estimated_reclaimable_bytes=view.estimated_reclaimable_bytes,
        version=view.version,
        error_code=view.error_code,
        created_at=view.created_at,
        updated_at=view.updated_at,
        finished_at=view.finished_at,
    )


def _pair_response(view: MovieDedupPairView) -> MovieDedupPairResponse:
    return MovieDedupPairResponse(
        id=view.id,
        source_relative_path=view.source_relative_path,
        target_relative_path=view.target_relative_path,
        source_media_metadata=view.source_media_metadata,
        target_media_metadata=view.target_media_metadata,
        source_size_bytes=view.source_size_bytes,
        target_size_bytes=view.target_size_bytes,
        source_device=view.source_device,
        target_device=view.target_device,
        source_inode=view.source_inode,
        target_inode=view.target_inode,
        source_link_count=view.source_link_count,
        target_link_count=view.target_link_count,
        source_mtime_ns=view.source_mtime_ns,
        target_mtime_ns=view.target_mtime_ns,
        source_sha256=view.source_sha256,
        target_sha256=view.target_sha256,
        metadata_match=view.metadata_match,
        size_match=view.size_match,
        quick_hash_match=view.quick_hash_match,
        full_hash_match=view.full_hash_match,
        status=view.status.value,
        resolved_action=view.resolved_action.value,
        estimated_reclaimable_bytes=view.estimated_reclaimable_bytes,
        error_code=view.error_code,
        error_message=view.error_message,
    )


@router.post("/movie-dedup/precheck", response_model=MovieDedupPrecheckResponse)
async def precheck_movie_dedup(
    payload: MovieDedupPrecheckRequest,
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(READ_ACCESS)],
) -> MovieDedupPrecheckResponse:
    result = movie_dedup_service(request).precheck(
        source_root=payload.source_root,
        target_root=payload.target_root,
        mode=payload.mode,
        cross_filesystem_policy=payload.cross_filesystem_policy,
    )
    return MovieDedupPrecheckResponse(
        source_root=result.source_root,
        target_root=result.target_root,
        source_device=result.source_device,
        target_device=result.target_device,
        same_filesystem=result.same_filesystem,
        resolved_action=result.resolved_action.value,
        blocked_reasons=list(result.blocked_reasons),
    )


@router.post(
    "/movie-dedup/jobs",
    response_model=MovieDedupJobResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_movie_dedup_job(
    payload: MovieDedupJobCreateRequest,
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(WRITE_ACCESS)],
) -> MovieDedupJobResponse:
    view = movie_dedup_service(request).create(
        MovieDedupJobCreate(
            name=payload.name,
            source_root=payload.source_root,
            target_root=payload.target_root,
            mode=payload.mode,
            cross_filesystem_policy=payload.cross_filesystem_policy,
            include_subdirectories=payload.include_subdirectories,
            min_size_bytes=payload.min_size_bytes,
            video_extensions=tuple(payload.video_extensions),
        ),
        trace_id=str(getattr(request.state, "trace_id", "unknown")),
    )
    return _job_response(view)


@router.get("/movie-dedup/jobs", response_model=MovieDedupJobListResponse)
async def list_movie_dedup_jobs(
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(READ_ACCESS)],
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> MovieDedupJobListResponse:
    return MovieDedupJobListResponse(
        items=[_job_response(item) for item in movie_dedup_service(request).list(limit=limit)]
    )


@router.get("/movie-dedup/jobs/{job_id}", response_model=MovieDedupJobResponse)
async def get_movie_dedup_job(
    job_id: str,
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(READ_ACCESS)],
) -> MovieDedupJobResponse:
    return _job_response(movie_dedup_service(request).get(job_id))


@router.delete("/movie-dedup/jobs/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_movie_dedup_job(
    job_id: str,
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(WRITE_ACCESS)],
) -> None:
    movie_dedup_service(request).delete(job_id)


@router.post("/movie-dedup/jobs/{job_id}/start", response_model=MovieDedupJobResponse)
async def start_movie_dedup_job(
    job_id: str,
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(WRITE_ACCESS)],
) -> MovieDedupJobResponse:
    return _job_response(movie_dedup_service(request).start(job_id))


@router.get("/movie-dedup/jobs/{job_id}/pairs", response_model=MovieDedupPairListResponse)
async def list_movie_dedup_pairs(
    job_id: str,
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(READ_ACCESS)],
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> MovieDedupPairListResponse:
    return MovieDedupPairListResponse(
        items=[
            _pair_response(item)
            for item in movie_dedup_service(request).list_pairs(
                job_id,
                limit=limit,
                offset=offset,
            )
        ]
    )


@router.post("/movie-dedup/jobs/{job_id}/execute", response_model=MovieDedupJobResponse)
async def execute_movie_dedup_pairs(
    job_id: str,
    payload: MovieDedupExecuteRequest,
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(WRITE_ACCESS)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> MovieDedupJobResponse:
    if idempotency_key is None:
        from backend.app.application.errors import ApplicationError

        raise ApplicationError(
            code="IDEMPOTENCY_KEY_REQUIRED",
            status=428,
            title="缺少幂等键",
            detail="影片去重执行请求必须携带 Idempotency-Key",
        )
    result = await asyncio.to_thread(
        movie_dedup_service(request).execute_pairs,
        job_id,
        pair_ids=tuple(payload.pair_ids),
        idempotency_key=idempotency_key,
    )
    return _job_response(result)
