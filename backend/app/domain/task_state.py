from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from backend.app.domain.errors import DomainViolation, ErrorCode
from backend.app.domain.verification import (
    DownloaderKind,
    VerificationLevel,
    decide_skip_checking,
)


class TaskStatus(StrEnum):
    PENDING = "PENDING"
    ANALYZING = "ANALYZING"
    SEARCHING = "SEARCHING"
    MATCHING = "MATCHING"
    VERIFYING = "VERIFYING"
    PREFLIGHT = "PREFLIGHT"
    AWAITING_CONFIRMATION = "AWAITING_CONFIRMATION"
    LINKING = "LINKING"
    ADDING = "ADDING"
    CLIENT_VERIFYING = "CLIENT_VERIFYING"
    SEEDING = "SEEDING"
    DONE = "DONE"
    PAUSED = "PAUSED"
    RETRY = "RETRY"
    FAILED = "FAILED"
    CANCELLING = "CANCELLING"
    ROLLING_BACK = "ROLLING_BACK"
    CANCELLED = "CANCELLED"


TERMINAL_STATUSES = frozenset({TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED})

_ALLOWED_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.PENDING: frozenset(
        {TaskStatus.ANALYZING, TaskStatus.PAUSED, TaskStatus.CANCELLING, TaskStatus.FAILED}
    ),
    TaskStatus.ANALYZING: frozenset(
        {
            TaskStatus.SEARCHING,
            TaskStatus.PAUSED,
            TaskStatus.RETRY,
            TaskStatus.CANCELLING,
            TaskStatus.FAILED,
        }
    ),
    TaskStatus.SEARCHING: frozenset(
        {
            TaskStatus.MATCHING,
            TaskStatus.PAUSED,
            TaskStatus.RETRY,
            TaskStatus.CANCELLING,
            TaskStatus.FAILED,
        }
    ),
    TaskStatus.MATCHING: frozenset(
        {
            TaskStatus.VERIFYING,
            TaskStatus.AWAITING_CONFIRMATION,
            TaskStatus.RETRY,
            TaskStatus.CANCELLING,
            TaskStatus.FAILED,
        }
    ),
    TaskStatus.VERIFYING: frozenset(
        {
            TaskStatus.PREFLIGHT,
            TaskStatus.AWAITING_CONFIRMATION,
            TaskStatus.RETRY,
            TaskStatus.CANCELLING,
            TaskStatus.FAILED,
        }
    ),
    TaskStatus.PREFLIGHT: frozenset(
        {
            TaskStatus.LINKING,
            TaskStatus.AWAITING_CONFIRMATION,
            TaskStatus.RETRY,
            TaskStatus.CANCELLING,
            TaskStatus.FAILED,
        }
    ),
    TaskStatus.AWAITING_CONFIRMATION: frozenset(
        {TaskStatus.VERIFYING, TaskStatus.LINKING, TaskStatus.CANCELLING, TaskStatus.FAILED}
    ),
    TaskStatus.LINKING: frozenset({TaskStatus.ADDING, TaskStatus.ROLLING_BACK}),
    TaskStatus.ADDING: frozenset({TaskStatus.CLIENT_VERIFYING, TaskStatus.ROLLING_BACK}),
    TaskStatus.CLIENT_VERIFYING: frozenset(
        {TaskStatus.SEEDING, TaskStatus.RETRY, TaskStatus.ROLLING_BACK}
    ),
    TaskStatus.SEEDING: frozenset({TaskStatus.DONE, TaskStatus.CANCELLING}),
    TaskStatus.PAUSED: frozenset(
        {
            TaskStatus.ANALYZING,
            TaskStatus.CANCELLING,
            TaskStatus.CANCELLED,
            TaskStatus.FAILED,
        }
    ),
    TaskStatus.RETRY: frozenset({TaskStatus.ANALYZING, TaskStatus.CANCELLING, TaskStatus.FAILED}),
    TaskStatus.CANCELLING: frozenset(
        {TaskStatus.ROLLING_BACK, TaskStatus.CANCELLED, TaskStatus.FAILED}
    ),
    TaskStatus.ROLLING_BACK: frozenset({TaskStatus.CANCELLED, TaskStatus.FAILED}),
    TaskStatus.DONE: frozenset(),
    TaskStatus.FAILED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class TaskTransition:
    from_status: TaskStatus
    to_status: TaskStatus
    reason: str
    occurred_at: datetime


def can_transition(from_status: TaskStatus, to_status: TaskStatus) -> bool:
    return to_status in _ALLOWED_TRANSITIONS[from_status]


def transition(
    from_status: TaskStatus,
    to_status: TaskStatus,
    *,
    reason: str,
    occurred_at: datetime | None = None,
) -> TaskTransition:
    if not can_transition(from_status, to_status):
        raise DomainViolation(
            ErrorCode.INVALID_STATE_TRANSITION,
            f"任务状态不允许从 {from_status} 转换到 {to_status}",
        )
    return TaskTransition(
        from_status=from_status,
        to_status=to_status,
        reason=reason,
        occurred_at=occurred_at or datetime.now(UTC),
    )


def transition_after_add(
    *,
    downloader: DownloaderKind,
    verification_level: VerificationLevel,
    skip_checking_enabled: bool,
    preflight_current: bool,
    reason: str,
    occurred_at: datetime | None = None,
) -> TaskTransition:
    """下载器添加完成后的唯一前进入口，避免绕过校验安全门。"""

    decision = decide_skip_checking(
        downloader=downloader,
        verification_level=verification_level,
        explicitly_enabled=skip_checking_enabled,
        preflight_current=preflight_current,
    )
    to_status = TaskStatus.SEEDING if decision.allowed else TaskStatus.CLIENT_VERIFYING
    return TaskTransition(
        from_status=TaskStatus.ADDING,
        to_status=to_status,
        reason=reason,
        occurred_at=occurred_at or datetime.now(UTC),
    )
