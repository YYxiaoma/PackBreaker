from dataclasses import dataclass
from enum import StrEnum

from backend.app.domain.errors import DomainViolation, ErrorCode


class OperationStatus(StrEnum):
    INTENT_RECORDED = "INTENT_RECORDED"
    APPLIED = "APPLIED"
    NOOP = "NOOP"
    ROLLBACK_PENDING = "ROLLBACK_PENDING"
    ROLLED_BACK = "ROLLED_BACK"
    RECONCILE_REQUIRED = "RECONCILE_REQUIRED"
    ROLLBACK_BLOCKED = "ROLLBACK_BLOCKED"


_ALLOWED_TRANSITIONS: dict[OperationStatus, frozenset[OperationStatus]] = {
    OperationStatus.INTENT_RECORDED: frozenset(
        {
            OperationStatus.APPLIED,
            OperationStatus.NOOP,
            OperationStatus.RECONCILE_REQUIRED,
        }
    ),
    OperationStatus.APPLIED: frozenset(
        {OperationStatus.ROLLBACK_PENDING, OperationStatus.RECONCILE_REQUIRED}
    ),
    OperationStatus.NOOP: frozenset(),
    OperationStatus.ROLLBACK_PENDING: frozenset(
        {
            OperationStatus.ROLLED_BACK,
            OperationStatus.ROLLBACK_BLOCKED,
            OperationStatus.RECONCILE_REQUIRED,
        }
    ),
    OperationStatus.ROLLED_BACK: frozenset(),
    OperationStatus.RECONCILE_REQUIRED: frozenset(
        {
            OperationStatus.APPLIED,
            OperationStatus.NOOP,
            OperationStatus.ROLLBACK_PENDING,
            OperationStatus.ROLLBACK_BLOCKED,
        }
    ),
    OperationStatus.ROLLBACK_BLOCKED: frozenset({OperationStatus.RECONCILE_REQUIRED}),
}


@dataclass(frozen=True, slots=True)
class OperationTransition:
    from_status: OperationStatus
    to_status: OperationStatus


def transition_operation(
    current: OperationStatus,
    to_status: OperationStatus,
) -> OperationTransition:
    """校验 operation journal 状态推进；外部副作用不得绕过该状态机。"""

    if to_status not in _ALLOWED_TRANSITIONS[current]:
        raise DomainViolation(
            ErrorCode.INVALID_STATE_TRANSITION,
            f"operation journal 不允许从 {current.value} 推进到 {to_status.value}",
        )
    return OperationTransition(current, to_status)
