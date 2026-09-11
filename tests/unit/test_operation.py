import pytest

from backend.app.domain.errors import DomainViolation, ErrorCode
from backend.app.domain.operation import (
    OperationStatus,
    operation_event_summary,
    transition_operation,
)


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


@pytest.mark.parametrize(
    ("operation_type", "status", "event_type", "reason_fragment"),
    [
        (
            "QBITTORRENT_ADD",
            OperationStatus.APPLIED,
            "QBITTORRENT_ADD_APPLIED",
            "添加任务",
        ),
        (
            "QBITTORRENT_RECHECK",
            OperationStatus.RECONCILE_REQUIRED,
            "QBITTORRENT_RECHECK_RECONCILE_REQUIRED",
            "安全对账",
        ),
        (
            "QBITTORRENT_REMOVE",
            OperationStatus.ROLLED_BACK,
            "QBITTORRENT_REMOVE_ROLLED_BACK",
            "完成回滚",
        ),
    ],
)
def test_operation_event_summary_uses_fixed_public_identity(
    operation_type: str,
    status: OperationStatus,
    event_type: str,
    reason_fragment: str,
) -> None:
    summary = operation_event_summary(operation_type, status)

    assert summary is not None
    assert summary.event_type == event_type
    assert reason_fragment in summary.reason


def test_operation_event_summary_never_echoes_unknown_operation_type() -> None:
    secret_bearing_type = "CUSTOM_/private/path_PASSKEY-should-not-leak"

    summary = operation_event_summary(secret_bearing_type, OperationStatus.INTENT_RECORDED)

    assert summary is not None
    assert summary.event_type == "OPERATION_INTENT_RECORDED"
    assert secret_bearing_type not in summary.event_type
    assert secret_bearing_type not in summary.reason


@pytest.mark.parametrize(
    "status",
    [
        OperationStatus.INTENT_RECORDED,
        OperationStatus.APPLIED,
        OperationStatus.NOOP,
        OperationStatus.ROLLBACK_PENDING,
        OperationStatus.ROLLED_BACK,
    ],
)
def test_routine_filesystem_operation_events_are_suppressed(status: OperationStatus) -> None:
    assert operation_event_summary("CREATE_DIRECTORY", status) is None
    assert operation_event_summary("CREATE_HARDLINK", status) is None


@pytest.mark.parametrize(
    "status",
    [OperationStatus.RECONCILE_REQUIRED, OperationStatus.ROLLBACK_BLOCKED],
)
def test_filesystem_safety_failures_remain_visible(status: OperationStatus) -> None:
    summary = operation_event_summary("CREATE_HARDLINK", status)

    assert summary is not None
    assert summary.event_type == f"FILESYSTEM_HARDLINK_{status.value}"
