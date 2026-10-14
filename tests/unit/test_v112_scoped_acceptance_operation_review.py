"""Offline-only scoped acceptance operation/journal risk classification."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, cast

import pytest

from backend.app.domain.unpack_acceptance_operation_review import (
    OfflineJournalEvidence,
    OfflineOperationReviewRequest,
    OfflineReviewStatus,
    review_scoped_operation_journal,
)

_ROOT = "/synthetic/packbreaker-acceptance"
_REMOTE = "/remote/packbreaker-acceptance"
_ITEM = "synthetic-item"
_PLAN = "a" * 64
_COPY = "UNPACK_EXEC_MATERIALIZE_COPY"
_DIR = "UNPACK_EXEC_CREATE_DIRECTORY"
_ADD = "UNPACK_EXEC_TRANSMISSION_ADD"
_VERIFY = "UNPACK_EXEC_TRANSMISSION_VERIFY"
_START = "UNPACK_EXEC_TRANSMISSION_START"


def _request(
    operation: str = _COPY,
    *,
    target: str | None = None,
    key: str = "next-intent",
) -> OfflineOperationReviewRequest:
    return OfflineOperationReviewRequest(
        execution_id="synthetic-execution",
        item_id=_ITEM,
        plan_digest=_PLAN,
        scope_execution_id="synthetic-execution",
        scope_item_id=_ITEM,
        scope_plan_digest=_PLAN,
        item_version_expected=3,
        item_version_observed=3,
        operation_type=operation,
        target_path=target or (_REMOTE if "TRANSMISSION" in operation else _ROOT + "/video.mkv"),
        idempotency_key=key,
        output_directory=_ROOT,
        target_remote_save_path=_REMOTE,
        expected_copy_actions=1,
        proposal_metadata_matches=True,
        proposal_file_current=True,
    )


def _journal(
    operation: str = _COPY,
    *,
    status: str = "APPLIED",
    key: str = "copy-1",
    target: str | None = None,
    item: str = _ITEM,
    plan: str = _PLAN,
) -> OfflineJournalEvidence:
    return OfflineJournalEvidence(
        item_id=item,
        plan_digest=plan,
        operation_type=operation,
        idempotency_key=key,
        target_path=target or (_REMOTE if "TRANSMISSION" in operation else _ROOT + "/video.mkv"),
        status=status,
    )


def _assess(
    request: OfflineOperationReviewRequest,
    journal: tuple[OfflineJournalEvidence, ...] = (),
) -> OfflineReviewStatus:
    result = review_scoped_operation_journal(request, journal)
    assert result.writes_authorized is False
    assert result.realtime_proof_verified is False
    assert result.external_client_contacted is False
    return result.status


@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ({"proposal_metadata_matches": False}, OfflineReviewStatus.BLOCKED_IDENTITY),
        ({"proposal_file_current": False}, OfflineReviewStatus.BLOCKED_IDENTITY),
        ({"execution_id": ""}, OfflineReviewStatus.BLOCKED_IDENTITY),
        ({"item_id": ""}, OfflineReviewStatus.BLOCKED_IDENTITY),
        ({"plan_digest": "c" * 64}, OfflineReviewStatus.BLOCKED_IDENTITY),
        ({"scope_plan_digest": "c" * 64}, OfflineReviewStatus.BLOCKED_IDENTITY),
        ({"scope_execution_id": "different"}, OfflineReviewStatus.BLOCKED_IDENTITY),
        ({"scope_item_id": "different"}, OfflineReviewStatus.BLOCKED_IDENTITY),
        ({"plan_digest": "bad-digest"}, OfflineReviewStatus.BLOCKED_IDENTITY),
        ({"item_version_observed": 4}, OfflineReviewStatus.BLOCKED_IDENTITY),
        ({"item_version_expected": 0}, OfflineReviewStatus.BLOCKED_IDENTITY),
        ({"idempotency_key": ""}, OfflineReviewStatus.BLOCKED_IDENTITY),
        (
            {"operation_type": "UNPACK_EXEC_MATERIALIZE_SYMLINK"},
            OfflineReviewStatus.BLOCKED_IDENTITY,
        ),
        (
            {"operation_type": "UNPACK_EXEC_TRANSMISSION_REMOVE"},
            OfflineReviewStatus.BLOCKED_IDENTITY,
        ),
        ({"expected_copy_actions": 0}, OfflineReviewStatus.BLOCKED_IDENTITY),
        ({"target_path": "/unapproved/media"}, OfflineReviewStatus.BLOCKED_TARGET),
        ({"target_path": _ROOT + "/../other"}, OfflineReviewStatus.BLOCKED_TARGET),
        ({"target_path": _ROOT + "-malicious/video.mkv"}, OfflineReviewStatus.BLOCKED_TARGET),
        ({"target_path": _ROOT + "/bad\\name"}, OfflineReviewStatus.BLOCKED_TARGET),
        ({"target_path": _ROOT + "/bad\x00name"}, OfflineReviewStatus.BLOCKED_TARGET),
        ({"target_remote_save_path": "/"}, OfflineReviewStatus.BLOCKED_TARGET),
        ({"output_directory": "/unapproved"}, OfflineReviewStatus.BLOCKED_TARGET),
    ],
)
def test_metadata_only_gate_fails_closed(
    override: dict[str, object], reason: OfflineReviewStatus
) -> None:
    assert _assess(replace(_request(), **cast(Any, override))) == reason


@pytest.mark.parametrize(
    ("operation", "target", "reason"),
    [
        (_COPY, _ROOT + "/video.mkv", OfflineReviewStatus.INTENT_NOT_RECORDED),
        (_DIR, _ROOT + "/subdir", OfflineReviewStatus.INTENT_NOT_RECORDED),
        (_ADD, _REMOTE, OfflineReviewStatus.BLOCKED_SEQUENCE),
        (_VERIFY, _REMOTE, OfflineReviewStatus.BLOCKED_SEQUENCE),
        (_START, _REMOTE, OfflineReviewStatus.BLOCKED_SEQUENCE),
        (_ADD, "/remote/another", OfflineReviewStatus.BLOCKED_TARGET),
        (_VERIFY, "/remote/another", OfflineReviewStatus.BLOCKED_TARGET),
        (_START, "/remote/another", OfflineReviewStatus.BLOCKED_TARGET),
    ],
)
def test_explicit_operation_allowlist_and_stage_dependencies(
    operation: str, target: str, reason: OfflineReviewStatus
) -> None:
    assert _assess(_request(operation, target=target)) == reason


@pytest.mark.parametrize(
    ("journal", "reason"),
    [
        (
            (_journal(status="INTENT_RECORDED", key="next-intent"),),
            OfflineReviewStatus.INTENT_RESULT_UNKNOWN,
        ),
        (
            (_journal(status="APPLIED", key="next-intent"),),
            OfflineReviewStatus.APPLIED_DO_NOT_REPLAY,
        ),
        ((_journal(status="RECONCILE_REQUIRED"),), OfflineReviewStatus.RECONCILE_REQUIRED),
        ((_journal(status="ROLLBACK_PENDING"),), OfflineReviewStatus.RECONCILE_REQUIRED),
        ((_journal(status="ROLLBACK_BLOCKED"),), OfflineReviewStatus.RECONCILE_REQUIRED),
        ((_journal(status="unexpected"),), OfflineReviewStatus.JOURNAL_CONFLICT),
        ((_journal(item="different"),), OfflineReviewStatus.JOURNAL_CONFLICT),
        ((_journal(plan="b" * 64),), OfflineReviewStatus.JOURNAL_CONFLICT),
        ((_journal(operation="UNKNOWN"),), OfflineReviewStatus.JOURNAL_CONFLICT),
        ((_journal(key="copy-1", target="/bad/media"),), OfflineReviewStatus.JOURNAL_CONFLICT),
        ((_journal(status="NOOP", key="next-intent"),), OfflineReviewStatus.METADATA_REVIEW_ONLY),
        (
            (_journal(status="ROLLED_BACK", key="next-intent"),),
            OfflineReviewStatus.METADATA_REVIEW_ONLY,
        ),
        ((_journal(status="INTENT_RECORDED"),), OfflineReviewStatus.INTENT_RESULT_UNKNOWN),
        ((_journal(), _journal()), OfflineReviewStatus.JOURNAL_CONFLICT),
        ((_journal(), _journal(key="copy-2")), OfflineReviewStatus.JOURNAL_CONFLICT),
        (
            (_journal(_ADD, key="add-1"), _journal(_ADD, key="add-2")),
            OfflineReviewStatus.JOURNAL_CONFLICT,
        ),
        ((_journal(key="next-intent", operation=_DIR),), OfflineReviewStatus.JOURNAL_CONFLICT),
        (
            (_journal(key="next-intent", target=_ROOT + "/other.mkv"),),
            OfflineReviewStatus.JOURNAL_CONFLICT,
        ),
    ],
)
def test_journal_ambiguity_never_allows_replay(
    journal: tuple[OfflineJournalEvidence, ...], reason: OfflineReviewStatus
) -> None:
    assert _assess(_request(), journal) == reason


def test_copy_applied_prerequisite_does_not_authorize_transmission_add() -> None:
    assert _assess(_request(_ADD), (_journal(),)) == OfflineReviewStatus.INTENT_NOT_RECORDED
    assert (
        _assess(_request(_ADD, key="add-1"), (_journal(), _journal(_ADD, key="add-1")))
        == OfflineReviewStatus.APPLIED_DO_NOT_REPLAY
    )


def test_add_requires_every_copy_applied_and_no_unknown_intents() -> None:
    request = replace(_request(_ADD), expected_copy_actions=2)
    assert _assess(request, (_journal(),)) == OfflineReviewStatus.BLOCKED_SEQUENCE
    journal = (_journal(), _journal(_COPY, key="copy-2", target=_ROOT + "/other.mkv"))
    assert _assess(request, journal) == OfflineReviewStatus.INTENT_NOT_RECORDED
    uncertain = (_journal(), _journal(_COPY, status="INTENT_RECORDED", key="copy-2"))
    assert _assess(request, uncertain) == OfflineReviewStatus.BLOCKED_SEQUENCE


def test_verify_requires_applied_add_and_start_requires_applied_verify() -> None:
    copy = _journal()
    add = _journal(_ADD, key="add-1")
    verify = _journal(_VERIFY, key="verify-1")
    assert _assess(_request(_VERIFY), (copy,)) == OfflineReviewStatus.BLOCKED_SEQUENCE
    assert _assess(_request(_VERIFY), (copy, add)) == OfflineReviewStatus.INTENT_NOT_RECORDED
    assert _assess(_request(_START), (copy, add)) == OfflineReviewStatus.BLOCKED_SEQUENCE
    assert _assess(_request(_START), (copy, add, verify)) == OfflineReviewStatus.INTENT_NOT_RECORDED
    start = _journal(_START, key="start-1")
    assert (
        _assess(_request(_START, key="start-1"), (copy, add, verify, start))
        == OfflineReviewStatus.APPLIED_DO_NOT_REPLAY
    )


def test_proposal_revoked_mid_sequence_blocks_even_applied_journals() -> None:
    journals = (_journal(), _journal(_ADD, key="add-1"))
    assert (
        _assess(replace(_request(_VERIFY), proposal_file_current=False), journals)
        == OfflineReviewStatus.BLOCKED_IDENTITY
    )
    assert (
        _assess(replace(_request(_VERIFY), item_version_observed=4), journals)
        == OfflineReviewStatus.BLOCKED_IDENTITY
    )


def test_unknown_result_in_prior_add_never_proceeds_to_verify() -> None:
    rows = (_journal(), _journal(_ADD, key="add-1", status="INTENT_RECORDED"))
    assert _assess(_request(_VERIFY), rows) == OfflineReviewStatus.BLOCKED_SEQUENCE
