import pytest

from backend.app.domain.unpack import (
    UnpackExecutionStatus,
    UnpackItemStatus,
    execution_transition_allowed,
    item_transition_allowed,
    validate_auto_match_threshold_bps,
)


@pytest.mark.parametrize("value", [0, 1, 9500, 9680, 10_000])
def test_auto_match_threshold_accepts_basis_point_range(value: int) -> None:
    assert validate_auto_match_threshold_bps(value) == value


@pytest.mark.parametrize("value", [-1, 10_001])
def test_auto_match_threshold_rejects_out_of_range(value: int) -> None:
    with pytest.raises(ValueError, match="自动匹配阈值"):
        validate_auto_match_threshold_bps(value)


def test_item_flow_requires_content_verification_before_execution() -> None:
    assert item_transition_allowed(
        UnpackItemStatus.MATCHED_AUTO,
        UnpackItemStatus.TORRENT_FETCHING,
    )
    assert not item_transition_allowed(
        UnpackItemStatus.MATCHED_AUTO,
        UnpackItemStatus.EXECUTING,
    )
    assert item_transition_allowed(
        UnpackItemStatus.CONTENT_VERIFIED,
        UnpackItemStatus.PLAN_PENDING,
    )
    assert item_transition_allowed(
        UnpackItemStatus.PLAN_PENDING,
        UnpackItemStatus.EXECUTING,
    )


def test_item_can_return_to_review_after_content_mismatch() -> None:
    assert item_transition_allowed(
        UnpackItemStatus.CONTENT_MISMATCH,
        UnpackItemStatus.REVIEW_REQUIRED,
    )
    assert item_transition_allowed(
        UnpackItemStatus.CONTENT_MISMATCH,
        UnpackItemStatus.MATCH_PENDING,
    )


def test_auto_match_can_be_manually_reviewed_before_side_effects() -> None:
    assert item_transition_allowed(
        UnpackItemStatus.MATCHED_AUTO,
        UnpackItemStatus.REVIEW_REQUIRED,
    )


def test_terminal_item_cannot_restart() -> None:
    assert not item_transition_allowed(
        UnpackItemStatus.COMPLETED,
        UnpackItemStatus.MATCH_PENDING,
    )


def test_execution_can_pause_and_resume_matching() -> None:
    assert execution_transition_allowed(
        UnpackExecutionStatus.MATCHING,
        UnpackExecutionStatus.PAUSED,
    )
    assert execution_transition_allowed(
        UnpackExecutionStatus.PAUSED,
        UnpackExecutionStatus.MATCHING,
    )


def test_terminal_execution_cannot_restart() -> None:
    assert not execution_transition_allowed(
        UnpackExecutionStatus.COMPLETED,
        UnpackExecutionStatus.MATCHING,
    )
