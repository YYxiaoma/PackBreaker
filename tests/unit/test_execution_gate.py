from datetime import UTC, datetime

import pytest

from backend.app.domain.execution_gate import (
    ExecutionGateBlockReason,
    ExecutionGateSnapshot,
    ExecutionVerificationSource,
)
from backend.app.domain.verification import VerificationLevel


def _snapshot(**overrides: object) -> ExecutionGateSnapshot:
    values: dict[str, object] = {
        "task_id": "task-1",
        "task_version": 7,
        "task_unit_id": "unit-1",
        "preflight_snapshot_id": "preflight-1",
        "preflight_snapshot_digest": "a" * 64,
        "preflight_stale_reasons": (),
        "review_revision_id": "review-1",
        "review_version": 2,
        "candidate_id": "candidate-1",
        "source_inventory_digest": "b" * 64,
        "metainfo_digest": "c" * 64,
        "verification_source": ExecutionVerificationSource.REVIEW_REVERIFICATION,
        "review_verification_id": "verification-1",
        "review_verification_digest": "d" * 64,
        "verification_level": VerificationLevel.FULL_VERIFIED,
        "eligible": True,
        "client_check_required": False,
        "blocked_reasons": (),
        "created_at": datetime(2026, 9, 10, tzinfo=UTC),
    }
    values.update(overrides)
    return ExecutionGateSnapshot(**values)  # type: ignore[arg-type]


def test_client_check_required_is_preserved_for_eligible_gate() -> None:
    snapshot = _snapshot(
        verification_level=VerificationLevel.CLIENT_CHECK_REQUIRED,
        client_check_required=True,
    )

    assert snapshot.eligible is True
    assert snapshot.client_check_required is True


def test_gate_digest_is_stable_across_created_at() -> None:
    first = _snapshot(created_at=datetime(2026, 9, 10, 1, tzinfo=UTC))
    second = _snapshot(created_at=datetime(2026, 9, 10, 2, tzinfo=UTC))

    assert first.gate_digest == second.gate_digest


def test_ineligible_gate_cannot_claim_client_check_requirement() -> None:
    with pytest.raises(ValueError, match="client_check_required"):
        _snapshot(
            eligible=False,
            client_check_required=True,
            verification_level=VerificationLevel.CLIENT_CHECK_REQUIRED,
            blocked_reasons=(ExecutionGateBlockReason.PREFLIGHT_STALE,),
        )
