import pytest

from backend.app.domain.errors import DomainViolation, ErrorCode
from backend.app.domain.operation import OperationStatus, transition_operation


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (OperationStatus.INTENT_RECORDED, OperationStatus.APPLIED),
        (OperationStatus.INTENT_RECORDED, OperationStatus.NOOP),
        (OperationStatus.INTENT_RECORDED, OperationStatus.RECONCILE_REQUIRED),
        (OperationStatus.APPLIED, OperationStatus.ROLLBACK_PENDING),
        (OperationStatus.ROLLBACK_PENDING, OperationStatus.ROLLED_BACK),
        (OperationStatus.ROLLBACK_PENDING, OperationStatus.ROLLBACK_BLOCKED),
        (OperationStatus.RECONCILE_REQUIRED, OperationStatus.APPLIED),
        (OperationStatus.ROLLBACK_BLOCKED, OperationStatus.RECONCILE_REQUIRED),
    ],
)
def test_operation_transition_allows_recovery_paths(
    current: OperationStatus,
    target: OperationStatus,
) -> None:
    result = transition_operation(current, target)

    assert result.from_status is current
    assert result.to_status is target


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (OperationStatus.INTENT_RECORDED, OperationStatus.ROLLED_BACK),
        (OperationStatus.APPLIED, OperationStatus.NOOP),
        (OperationStatus.NOOP, OperationStatus.APPLIED),
        (OperationStatus.ROLLED_BACK, OperationStatus.RECONCILE_REQUIRED),
    ],
)
def test_operation_transition_rejects_unsafe_shortcuts(
    current: OperationStatus,
    target: OperationStatus,
) -> None:
    with pytest.raises(DomainViolation) as failure:
        transition_operation(current, target)

    assert failure.value.code is ErrorCode.INVALID_STATE_TRANSITION
