from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from backend.app.domain.unpack import UnpackExecutionStatus, UnpackItemStatus
from backend.app.infrastructure.persistence.models import (
    UnpackExecution,
    UnpackExecutionItem,
    utc_now,
)

PIPELINE_ACTIVE_EXECUTION_STATUSES = (
    UnpackExecutionStatus.CONTENT_VERIFYING.value,
    UnpackExecutionStatus.EXECUTING.value,
    UnpackExecutionStatus.CLIENT_VERIFYING.value,
)


def unpack_execution_has_pending_match_work() -> ColumnElement[bool]:
    return (
        select(UnpackExecutionItem.id)
        .where(UnpackExecutionItem.execution_id == UnpackExecution.id)
        .where(
            UnpackExecutionItem.status.in_(
                (
                    UnpackItemStatus.MATCH_PENDING.value,
                    UnpackItemStatus.MATCHING.value,
                )
            )
        )
        .exists()
    )


def unpack_execution_has_unresolved_review_work() -> ColumnElement[bool]:
    return (
        select(UnpackExecutionItem.id)
        .where(UnpackExecutionItem.execution_id == UnpackExecution.id)
        .where(UnpackExecutionItem.status == UnpackItemStatus.REVIEW_REQUIRED.value)
        .where(UnpackExecutionItem.selected_candidate_id.is_(None))
        .exists()
    )


def refresh_unpack_execution_state(session: Session, execution: UnpackExecution) -> None:
    """Refresh counters and derive the parent phase from the earliest unfinished item phase."""
    counts = {status: 0 for status in UnpackItemStatus}
    rows = session.execute(
        select(UnpackExecutionItem.status, func.count(UnpackExecutionItem.id))
        .where(UnpackExecutionItem.execution_id == execution.id)
        .group_by(UnpackExecutionItem.status)
    ).all()
    for status_value, amount in rows:
        counts[UnpackItemStatus(status_value)] = int(amount)

    execution.matched_auto_count = counts[UnpackItemStatus.MATCHED_AUTO]
    execution.review_count = counts[UnpackItemStatus.REVIEW_REQUIRED]
    execution.content_verified_count = counts[UnpackItemStatus.CONTENT_VERIFIED]
    execution.content_mismatch_count = counts[UnpackItemStatus.CONTENT_MISMATCH]
    execution.timeout_count = counts[UnpackItemStatus.MATCH_TIMEOUT]
    execution.error_count = (
        counts[UnpackItemStatus.MATCH_ERROR]
        + counts[UnpackItemStatus.CONTENT_VERIFY_ERROR]
        + counts[UnpackItemStatus.EXECUTION_ERROR]
    )
    execution.completed_count = counts[UnpackItemStatus.COMPLETED]

    if execution.status in {
        UnpackExecutionStatus.PAUSED.value,
        UnpackExecutionStatus.CANCELLED.value,
    }:
        return

    matching_active = any(
        counts[status]
        for status in (
            UnpackItemStatus.MATCH_PENDING,
            UnpackItemStatus.MATCHING,
        )
    )
    content_active = any(
        counts[status]
        for status in (
            UnpackItemStatus.MATCHED_AUTO,
            UnpackItemStatus.MATCHED_MANUAL,
            UnpackItemStatus.TORRENT_FETCHING,
            UnpackItemStatus.AUXILIARY_FETCHING,
            UnpackItemStatus.CONTENT_VERIFYING,
            UnpackItemStatus.CONTENT_VERIFIED,
        )
    )
    execution_active = any(
        counts[status] for status in (UnpackItemStatus.PLAN_PENDING, UnpackItemStatus.EXECUTING)
    )
    client_active = counts[UnpackItemStatus.CLIENT_VERIFYING] > 0
    failure_count = sum(
        counts[status]
        for status in (
            UnpackItemStatus.NO_MATCH,
            UnpackItemStatus.MATCH_TIMEOUT,
            UnpackItemStatus.MATCH_ERROR,
            UnpackItemStatus.CONTENT_VERIFY_ERROR,
            UnpackItemStatus.CONTENT_MISMATCH,
            UnpackItemStatus.EXECUTION_ERROR,
        )
    )
    matching_failure_count = sum(
        counts[status]
        for status in (
            UnpackItemStatus.NO_MATCH,
            UnpackItemStatus.MATCH_TIMEOUT,
            UnpackItemStatus.MATCH_ERROR,
        )
    )

    if not execution.discovery_complete:
        next_status = UnpackExecutionStatus.DISCOVERING
    elif matching_active:
        next_status = UnpackExecutionStatus.MATCHING
    elif counts[UnpackItemStatus.REVIEW_REQUIRED]:
        next_status = UnpackExecutionStatus.REVIEW_REQUIRED
    elif content_active:
        next_status = UnpackExecutionStatus.CONTENT_VERIFYING
    elif execution_active:
        next_status = UnpackExecutionStatus.EXECUTING
    elif client_active:
        next_status = UnpackExecutionStatus.CLIENT_VERIFYING
    elif execution.completed_count == execution.total_count:
        next_status = UnpackExecutionStatus.COMPLETED
    elif failure_count:
        # FAILED is reserved for executions where every child failed during
        # matching. Failures after a candidate was selected are completed
        # execution results with errors, even when no child completed.
        next_status = (
            UnpackExecutionStatus.FAILED
            if matching_failure_count == execution.total_count
            else UnpackExecutionStatus.COMPLETED_WITH_ERRORS
        )
    else:
        next_status = UnpackExecutionStatus(execution.status)

    now = utc_now()
    execution.status = next_status.value
    execution.finished_at = (
        now
        if next_status
        in {
            UnpackExecutionStatus.COMPLETED,
            UnpackExecutionStatus.COMPLETED_WITH_ERRORS,
            UnpackExecutionStatus.FAILED,
        }
        else None
    )
    execution.updated_at = now
    execution.version += 1
