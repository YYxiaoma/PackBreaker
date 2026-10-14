"""Read-only overview aggregates. No downloader, filesystem, or site probes."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.domain.unpack_execution_plan import unpack_execution_plan_from_payload
from backend.app.infrastructure.persistence.models import (
    MovieDedupFileInventory,
    MovieDedupJob,
    MovieDedupPair,
    UnpackExecutionItem,
)

_ERROR_ITEMS = frozenset({"MATCH_TIMEOUT", "MATCH_ERROR", "CONTENT_MISMATCH", "EXECUTION_ERROR"})
_IDLE_ITEMS = frozenset({"COMPLETED", "CANCELLED", "NO_MATCH", "REVIEW_REQUIRED"}) | _ERROR_ITEMS
_PENDING_JOBS = frozenset({"PENDING", "RUNNING", "REVIEW_REQUIRED", "RECOVERY_REQUIRED"})
_PENDING_PAIRS = frozenset({"VERIFIED_DUPLICATE", "REVIEW_REQUIRED", "READY"})


def overview_statistics(session: Session) -> dict[str, int | None]:
    """Count current item outcomes; retries update an item instead of adding successes.

    Bytes are deduplicated by frozen torrent/downloader/save-path identity.
    Dedup targets use their original device/inode/ctime across scans and pairs.
    Reclaimed bytes are estimates from completed operations, never free-space deltas.
    """
    counts = {
        status: int(count)
        for status, count in session.execute(
            select(UnpackExecutionItem.status, func.count()).group_by(UnpackExecutionItem.status)
        )
    }
    metrics: dict[str, int | None] = {
        "unpack_total": sum(counts.values()),
        "unpack_success": counts.get("COMPLETED", 0),
        "unpack_failed": sum(counts.get(status, 0) for status in _ERROR_ITEMS),
        "unpack_review": counts.get("REVIEW_REQUIRED", 0),
        "unpack_running": sum(
            count for status, count in counts.items() if status not in _IDLE_ITEMS
        ),
        "unpack_no_match": counts.get("NO_MATCH", 0),
        "unpack_cancelled": counts.get("CANCELLED", 0),
    }
    volumes: dict[tuple[str, str, str], int] = {}
    unknown = 0
    for item_id, digest, payload in session.execute(
        select(
            UnpackExecutionItem.id,
            UnpackExecutionItem.execution_plan_digest,
            UnpackExecutionItem.execution_plan,
        ).where(UnpackExecutionItem.status == "COMPLETED")
    ).yield_per(200):
        try:
            plan = unpack_execution_plan_from_payload(payload)
            if not plan.ready or plan.item_id != item_id or plan.plan_digest != digest:
                raise ValueError("Completed item plan identity mismatch")
        except (KeyError, TypeError, ValueError):
            unknown += 1
            continue
        identity = (
            plan.target_downloader_id,
            plan.metainfo_digest,
            plan.target_remote_save_path,
        )
        volumes[identity] = max(volumes.get(identity, 0), sum(a.length for a in plan.actions))
    metrics.update(
        unpack_seeded_bytes=sum(volumes.values()),
        unpack_seeded_torrents=len(volumes),
        unpack_volume_unknown=unknown,
    )

    jobs = {
        status: int(count)
        for status, count in session.execute(
            select(MovieDedupJob.status, func.count()).group_by(MovieDedupJob.status)
        )
    }
    scanned = session.scalar(select(func.count()).select_from(MovieDedupFileInventory)) or 0
    # Each target can match several sources and appear in several scan jobs.
    completed: dict[tuple[int, int, int], int] = {}
    pending: dict[tuple[int, int, int], int] = {}
    verified: set[tuple[int, int, int]] = set()
    failed: set[tuple[int, int, int]] = set()
    rows = session.execute(
        select(
            MovieDedupFileInventory.device,
            MovieDedupFileInventory.inode,
            MovieDedupFileInventory.ctime_ns,
            MovieDedupPair.status,
            MovieDedupPair.full_hash_match,
            MovieDedupPair.estimated_reclaimable_bytes,
            MovieDedupJob.status,
        )
        .select_from(MovieDedupPair)
        .join(MovieDedupFileInventory, MovieDedupFileInventory.id == MovieDedupPair.target_file_id)
        .join(MovieDedupJob, MovieDedupJob.id == MovieDedupPair.job_id)
    ).yield_per(500)
    for device, inode, ctime_ns, status, full_match, reclaimable, job_status in rows:
        key = (device, inode, ctime_ns)
        if full_match:
            verified.add(key)
        if status == "COMPLETED":
            completed[key] = max(completed.get(key, 0), reclaimable)
        elif status in _PENDING_PAIRS and full_match and job_status in _PENDING_JOBS:
            pending[key] = max(pending.get(key, 0), reclaimable)
        elif status == "FAILED":
            failed.add(key)
    metrics.update(
        dedup_jobs=sum(jobs.values()),
        dedup_jobs_completed=jobs.get("COMPLETED", 0),
        dedup_jobs_running=jobs.get("RUNNING", 0),
        dedup_jobs_failed=jobs.get("FAILED", 0) + jobs.get("PARTIAL_FAILED", 0),
        dedup_jobs_pending=sum(jobs.get(s, 0) for s in _PENDING_JOBS if s != "RUNNING"),
        dedup_jobs_cancelled=jobs.get("CANCELLED", 0),
        dedup_scan_records=int(scanned),
        dedup_verified_targets=len(verified),
        dedup_completed_targets=len(completed),
        dedup_failed_targets=len(failed - completed.keys()),
        dedup_reclaimed_estimate_bytes=sum(completed.values()),
        dedup_pending_estimate_bytes=sum(v for k, v in pending.items() if k not in completed),
        dedup_actual_reclaimed_bytes=None,
    )
    return metrics
