from __future__ import annotations

from enum import StrEnum


class UnpackTriggerKind(StrEnum):
    MANUAL = "MANUAL"
    MONITOR = "MONITOR"


class UnpackDefinitionStatus(StrEnum):
    PENDING_EXECUTION = "PENDING_EXECUTION"
    ENABLED = "ENABLED"
    PAUSED = "PAUSED"
    ERROR = "ERROR"


class UnpackSourceKind(StrEnum):
    DIRECTORY = "DIRECTORY"
    DOWNLOADER = "DOWNLOADER"


class UnpackExecutionScopeKind(StrEnum):
    ALL_MATCHING_MEDIA = "ALL_MATCHING_MEDIA"
    SELECTED_MEDIA = "SELECTED_MEDIA"


class UnpackExecutionTrigger(StrEnum):
    MANUAL = "MANUAL"
    SCHEDULE = "SCHEDULE"
    MONITOR_EVENT = "MONITOR_EVENT"


class UnpackExecutionStatus(StrEnum):
    DISCOVERING = "DISCOVERING"
    MATCHING = "MATCHING"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    CONTENT_VERIFYING = "CONTENT_VERIFYING"
    EXECUTING = "EXECUTING"
    CLIENT_VERIFYING = "CLIENT_VERIFYING"
    COMPLETED = "COMPLETED"
    COMPLETED_WITH_ERRORS = "COMPLETED_WITH_ERRORS"
    PAUSED = "PAUSED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class UnpackItemStatus(StrEnum):
    DISCOVERED = "DISCOVERED"
    MATCH_PENDING = "MATCH_PENDING"
    MATCHING = "MATCHING"
    MATCHED_AUTO = "MATCHED_AUTO"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    MATCHED_MANUAL = "MATCHED_MANUAL"
    NO_MATCH = "NO_MATCH"
    MATCH_TIMEOUT = "MATCH_TIMEOUT"
    MATCH_ERROR = "MATCH_ERROR"
    TORRENT_FETCHING = "TORRENT_FETCHING"
    AUXILIARY_FETCHING = "AUXILIARY_FETCHING"
    CONTENT_VERIFYING = "CONTENT_VERIFYING"
    CONTENT_VERIFIED = "CONTENT_VERIFIED"
    CONTENT_MISMATCH = "CONTENT_MISMATCH"
    PLAN_PENDING = "PLAN_PENDING"
    EXECUTING = "EXECUTING"
    CLIENT_VERIFYING = "CLIENT_VERIFYING"
    COMPLETED = "COMPLETED"
    EXECUTION_ERROR = "EXECUTION_ERROR"
    CANCELLED = "CANCELLED"


class UnpackCandidateVerificationStatus(StrEnum):
    NOT_CHECKED = "NOT_CHECKED"
    VERIFYING = "VERIFYING"
    VERIFIED = "VERIFIED"
    MISMATCH = "MISMATCH"
    UNAVAILABLE = "UNAVAILABLE"


class UnpackContentVerificationLevel(StrEnum):
    FULL_VERIFIED = "FULL_VERIFIED"
    CLIENT_CHECK_REQUIRED = "CLIENT_CHECK_REQUIRED"
    BLOCKED = "BLOCKED"


class UnpackReviewDecision(StrEnum):
    APPROVE = "APPROVE"
    NO_MATCH = "NO_MATCH"


TERMINAL_EXECUTION_STATUSES = frozenset(
    {
        UnpackExecutionStatus.COMPLETED,
        UnpackExecutionStatus.COMPLETED_WITH_ERRORS,
        UnpackExecutionStatus.FAILED,
        UnpackExecutionStatus.CANCELLED,
    }
)

TERMINAL_ITEM_STATUSES = frozenset(
    {
        UnpackItemStatus.NO_MATCH,
        UnpackItemStatus.COMPLETED,
        UnpackItemStatus.EXECUTION_ERROR,
        UnpackItemStatus.CANCELLED,
    }
)

REVIEWABLE_ITEM_STATUSES = frozenset(
    {
        UnpackItemStatus.REVIEW_REQUIRED,
        UnpackItemStatus.MATCHED_AUTO,
        UnpackItemStatus.TORRENT_FETCHING,
        UnpackItemStatus.CONTENT_VERIFYING,
        UnpackItemStatus.CONTENT_VERIFIED,
        UnpackItemStatus.PLAN_PENDING,
    }
)


_EXECUTION_TRANSITIONS: dict[UnpackExecutionStatus, frozenset[UnpackExecutionStatus]] = {
    UnpackExecutionStatus.DISCOVERING: frozenset(
        {
            UnpackExecutionStatus.MATCHING,
            UnpackExecutionStatus.PAUSED,
            UnpackExecutionStatus.FAILED,
            UnpackExecutionStatus.CANCELLED,
        }
    ),
    UnpackExecutionStatus.MATCHING: frozenset(
        {
            UnpackExecutionStatus.REVIEW_REQUIRED,
            UnpackExecutionStatus.CONTENT_VERIFYING,
            UnpackExecutionStatus.EXECUTING,
            UnpackExecutionStatus.COMPLETED,
            UnpackExecutionStatus.COMPLETED_WITH_ERRORS,
            UnpackExecutionStatus.PAUSED,
            UnpackExecutionStatus.FAILED,
            UnpackExecutionStatus.CANCELLED,
        }
    ),
    UnpackExecutionStatus.REVIEW_REQUIRED: frozenset(
        {
            UnpackExecutionStatus.MATCHING,
            UnpackExecutionStatus.CONTENT_VERIFYING,
            UnpackExecutionStatus.EXECUTING,
            UnpackExecutionStatus.COMPLETED,
            UnpackExecutionStatus.COMPLETED_WITH_ERRORS,
            UnpackExecutionStatus.PAUSED,
            UnpackExecutionStatus.FAILED,
            UnpackExecutionStatus.CANCELLED,
        }
    ),
    UnpackExecutionStatus.CONTENT_VERIFYING: frozenset(
        {
            UnpackExecutionStatus.REVIEW_REQUIRED,
            UnpackExecutionStatus.EXECUTING,
            UnpackExecutionStatus.COMPLETED_WITH_ERRORS,
            UnpackExecutionStatus.PAUSED,
            UnpackExecutionStatus.FAILED,
            UnpackExecutionStatus.CANCELLED,
        }
    ),
    UnpackExecutionStatus.EXECUTING: frozenset(
        {
            UnpackExecutionStatus.CLIENT_VERIFYING,
            UnpackExecutionStatus.REVIEW_REQUIRED,
            UnpackExecutionStatus.COMPLETED,
            UnpackExecutionStatus.COMPLETED_WITH_ERRORS,
            UnpackExecutionStatus.PAUSED,
            UnpackExecutionStatus.FAILED,
            UnpackExecutionStatus.CANCELLED,
        }
    ),
    UnpackExecutionStatus.CLIENT_VERIFYING: frozenset(
        {
            UnpackExecutionStatus.COMPLETED,
            UnpackExecutionStatus.COMPLETED_WITH_ERRORS,
            UnpackExecutionStatus.FAILED,
            UnpackExecutionStatus.CANCELLED,
        }
    ),
    UnpackExecutionStatus.PAUSED: frozenset(
        {
            UnpackExecutionStatus.DISCOVERING,
            UnpackExecutionStatus.MATCHING,
            UnpackExecutionStatus.REVIEW_REQUIRED,
            UnpackExecutionStatus.CONTENT_VERIFYING,
            UnpackExecutionStatus.EXECUTING,
            UnpackExecutionStatus.CANCELLED,
        }
    ),
}


_ITEM_TRANSITIONS: dict[UnpackItemStatus, frozenset[UnpackItemStatus]] = {
    UnpackItemStatus.DISCOVERED: frozenset(
        {UnpackItemStatus.MATCH_PENDING, UnpackItemStatus.CANCELLED}
    ),
    UnpackItemStatus.MATCH_PENDING: frozenset(
        {UnpackItemStatus.MATCHING, UnpackItemStatus.CANCELLED}
    ),
    UnpackItemStatus.MATCHING: frozenset(
        {
            UnpackItemStatus.MATCHED_AUTO,
            UnpackItemStatus.REVIEW_REQUIRED,
            UnpackItemStatus.NO_MATCH,
            UnpackItemStatus.MATCH_TIMEOUT,
            UnpackItemStatus.MATCH_ERROR,
            UnpackItemStatus.CANCELLED,
        }
    ),
    UnpackItemStatus.MATCHED_AUTO: frozenset(
        {
            UnpackItemStatus.REVIEW_REQUIRED,
            UnpackItemStatus.MATCHED_MANUAL,
            UnpackItemStatus.NO_MATCH,
            UnpackItemStatus.TORRENT_FETCHING,
            UnpackItemStatus.CANCELLED,
        }
    ),
    UnpackItemStatus.REVIEW_REQUIRED: frozenset(
        {
            UnpackItemStatus.MATCHED_MANUAL,
            UnpackItemStatus.NO_MATCH,
            UnpackItemStatus.MATCH_PENDING,
            UnpackItemStatus.CANCELLED,
        }
    ),
    UnpackItemStatus.MATCHED_MANUAL: frozenset(
        {UnpackItemStatus.TORRENT_FETCHING, UnpackItemStatus.CANCELLED}
    ),
    UnpackItemStatus.MATCH_TIMEOUT: frozenset(
        {UnpackItemStatus.MATCH_PENDING, UnpackItemStatus.CANCELLED}
    ),
    UnpackItemStatus.NO_MATCH: frozenset(
        {UnpackItemStatus.MATCH_PENDING, UnpackItemStatus.CANCELLED}
    ),
    UnpackItemStatus.MATCH_ERROR: frozenset(
        {UnpackItemStatus.MATCH_PENDING, UnpackItemStatus.CANCELLED}
    ),
    UnpackItemStatus.TORRENT_FETCHING: frozenset(
        {
            UnpackItemStatus.AUXILIARY_FETCHING,
            UnpackItemStatus.CONTENT_VERIFYING,
            UnpackItemStatus.MATCHED_MANUAL,
            UnpackItemStatus.NO_MATCH,
            UnpackItemStatus.MATCH_ERROR,
            UnpackItemStatus.CANCELLED,
        }
    ),
    UnpackItemStatus.AUXILIARY_FETCHING: frozenset(
        {
            UnpackItemStatus.CONTENT_VERIFYING,
            UnpackItemStatus.MATCH_ERROR,
            UnpackItemStatus.CANCELLED,
        }
    ),
    UnpackItemStatus.CONTENT_VERIFYING: frozenset(
        {
            # Recheck another auto-proposed candidate before any downloader
            # or filesystem side effect. The service checks origin and journal.
            UnpackItemStatus.MATCHED_AUTO,
            UnpackItemStatus.CONTENT_VERIFIED,
            UnpackItemStatus.CONTENT_MISMATCH,
            UnpackItemStatus.REVIEW_REQUIRED,
            UnpackItemStatus.MATCHED_MANUAL,
            UnpackItemStatus.NO_MATCH,
            UnpackItemStatus.CANCELLED,
        }
    ),
    UnpackItemStatus.CONTENT_VERIFIED: frozenset(
        {
            UnpackItemStatus.MATCHED_MANUAL,
            UnpackItemStatus.NO_MATCH,
            UnpackItemStatus.PLAN_PENDING,
            UnpackItemStatus.CANCELLED,
        }
    ),
    UnpackItemStatus.CONTENT_MISMATCH: frozenset(
        {
            UnpackItemStatus.REVIEW_REQUIRED,
            UnpackItemStatus.MATCH_PENDING,
            UnpackItemStatus.CANCELLED,
        }
    ),
    UnpackItemStatus.PLAN_PENDING: frozenset(
        {
            UnpackItemStatus.MATCHED_MANUAL,
            UnpackItemStatus.NO_MATCH,
            UnpackItemStatus.EXECUTING,
            UnpackItemStatus.EXECUTION_ERROR,
            UnpackItemStatus.CANCELLED,
        }
    ),
    UnpackItemStatus.EXECUTING: frozenset(
        {
            UnpackItemStatus.CLIENT_VERIFYING,
            UnpackItemStatus.EXECUTION_ERROR,
            UnpackItemStatus.CANCELLED,
        }
    ),
    UnpackItemStatus.CLIENT_VERIFYING: frozenset(
        {
            UnpackItemStatus.COMPLETED,
            UnpackItemStatus.EXECUTION_ERROR,
            UnpackItemStatus.CANCELLED,
        }
    ),
}


def execution_transition_allowed(
    current: UnpackExecutionStatus,
    target: UnpackExecutionStatus,
) -> bool:
    if current == target:
        return True
    if current in TERMINAL_EXECUTION_STATUSES:
        return False
    return target in _EXECUTION_TRANSITIONS.get(current, frozenset())


def item_transition_allowed(current: UnpackItemStatus, target: UnpackItemStatus) -> bool:
    if current == target:
        return True
    if current is UnpackItemStatus.NO_MATCH and target is UnpackItemStatus.MATCH_PENDING:
        # An explicit, policy-gated retry may reopen a terminal no-match item.
        return True
    if current in TERMINAL_ITEM_STATUSES:
        return False
    return target in _ITEM_TRANSITIONS.get(current, frozenset())


def validate_auto_match_threshold_bps(value: int) -> int:
    if value < 0 or value > 10_000:
        raise ValueError("自动匹配阈值必须位于 0.0% 到 100.0% 之间")
    return value
