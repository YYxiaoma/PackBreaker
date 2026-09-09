import pytest

from backend.app.domain.errors import DomainViolation, ErrorCode
from backend.app.domain.task_state import (
    TaskStatus,
    can_transition,
    transition,
    transition_after_add,
)
from backend.app.domain.verification import (
    DownloaderKind,
    VerificationLevel,
)


def test_normal_analysis_path_is_allowed() -> None:
    assert can_transition(TaskStatus.PENDING, TaskStatus.ANALYZING)
    assert can_transition(TaskStatus.ANALYZING, TaskStatus.SEARCHING)
    assert can_transition(TaskStatus.SEARCHING, TaskStatus.MATCHING)
    assert can_transition(TaskStatus.MATCHING, TaskStatus.VERIFYING)
    assert can_transition(TaskStatus.VERIFYING, TaskStatus.PREFLIGHT)


def test_side_effect_states_cannot_fail_without_rollback_path() -> None:
    assert not can_transition(TaskStatus.LINKING, TaskStatus.FAILED)
    assert not can_transition(TaskStatus.ADDING, TaskStatus.FAILED)
    assert not can_transition(TaskStatus.ADDING, TaskStatus.SEEDING)
    assert not can_transition(TaskStatus.CLIENT_VERIFYING, TaskStatus.FAILED)
    assert can_transition(TaskStatus.LINKING, TaskStatus.ROLLING_BACK)


def test_after_add_uses_skip_checking_gate() -> None:
    allowed_transition = transition_after_add(
        downloader=DownloaderKind.QBITTORRENT,
        verification_level=VerificationLevel.FULL_VERIFIED,
        skip_checking_enabled=True,
        preflight_current=True,
        reason="verified",
    )
    denied_transition = transition_after_add(
        downloader=DownloaderKind.TRANSMISSION,
        verification_level=VerificationLevel.FULL_VERIFIED,
        skip_checking_enabled=True,
        preflight_current=True,
        reason="client check",
    )

    assert allowed_transition.to_status is TaskStatus.SEEDING
    assert denied_transition.to_status is TaskStatus.CLIENT_VERIFYING


def test_terminal_state_cannot_transition() -> None:
    assert not can_transition(TaskStatus.DONE, TaskStatus.ANALYZING)


def test_invalid_transition_returns_stable_error_code() -> None:
    with pytest.raises(DomainViolation) as exc_info:
        transition(TaskStatus.PENDING, TaskStatus.SEEDING, reason="unsafe shortcut")

    assert exc_info.value.code is ErrorCode.INVALID_STATE_TRANSITION
