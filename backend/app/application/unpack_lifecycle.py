from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.domain.unpack import (
    TERMINAL_EXECUTION_STATUSES,
    UnpackExecutionStatus,
    UnpackItemStatus,
    execution_transition_allowed,
)
from backend.app.infrastructure.persistence.database import begin_immediate_write
from backend.app.infrastructure.persistence.models import (
    UnpackExecution,
    UnpackExecutionItem,
    utc_now,
)

PIPELINE_ACTIVE_EXECUTION_STATUSES = (
    # 流水线推进：匹配尚未全部结束时，已确定候选的影片项也可以继续校验/落位/辅种。
    UnpackExecutionStatus.MATCHING.value,
    UnpackExecutionStatus.CONTENT_VERIFYING.value,
    UnpackExecutionStatus.EXECUTING.value,
    UnpackExecutionStatus.CLIENT_VERIFYING.value,
)


def refresh_stale_review_executions(session_factory: sessionmaker[Session]) -> int:
    """启动时重算停留在 REVIEW_REQUIRED 的执行。

    旧版本把“有未决审核”当作整批阻塞，执行状态会被固定成 ``REVIEW_REQUIRED``；
    该状态不在活跃集合里，驱动永远不会再调度它。新的推导把待审核降到“只剩它时
    才生效”，因此这里做一次性归一化，避免升级后旧执行卡死。
    """

    with session_factory() as session:
        begin_immediate_write(session)
        executions = session.scalars(
            select(UnpackExecution).where(
                UnpackExecution.status == UnpackExecutionStatus.REVIEW_REQUIRED.value
            )
        ).all()
        for execution in executions:
            refresh_unpack_execution_state(session, execution)
        session.commit()
        return len(executions)


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
    elif content_active:
        next_status = UnpackExecutionStatus.CONTENT_VERIFYING
    elif execution_active:
        next_status = UnpackExecutionStatus.EXECUTING
    elif client_active:
        next_status = UnpackExecutionStatus.CLIENT_VERIFYING
    elif counts[UnpackItemStatus.REVIEW_REQUIRED]:
        # 待审核只阻塞自己：只要还有其它影片可推进，执行状态就保持活跃阶段；
        # 只剩待审核时才落到 REVIEW_REQUIRED（因此必须排在“已完成”之前）。
        next_status = UnpackExecutionStatus.REVIEW_REQUIRED
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

    # 让领域状态机成为唯一权威：终态默认不复活，除非显式重开。
    current_status = UnpackExecutionStatus(execution.status)
    if not execution_transition_allowed(
        current_status, next_status, allow_restart=current_status in TERMINAL_EXECUTION_STATUSES
    ):
        raise ValueError(f"非法的执行状态跃迁：{current_status.value} -> {next_status.value}")
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
