from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256

from backend.app.domain.verification import VerificationLevel

EXECUTION_GATE_SCHEMA_VERSION = "packbreaker-execution-gate-v1"


class ExecutionVerificationSource(StrEnum):
    PREFLIGHT = "PREFLIGHT"
    REVIEW_REVERIFICATION = "REVIEW_REVERIFICATION"


class ExecutionGateBlockReason(StrEnum):
    TASK_STATE_INVALID = "TASK_STATE_INVALID"
    PREFLIGHT_STALE = "PREFLIGHT_STALE"
    APPROVED_CANDIDATE_MISSING = "APPROVED_CANDIDATE_MISSING"
    CANDIDATE_INVALID = "CANDIDATE_INVALID"
    CANDIDATE_HARD_REJECTED = "CANDIDATE_HARD_REJECTED"
    CANDIDATE_ERROR = "CANDIDATE_ERROR"
    CANDIDATE_NOT_VERIFIED = "CANDIDATE_NOT_VERIFIED"
    REVERIFICATION_REQUIRED = "REVERIFICATION_REQUIRED"
    REVERIFICATION_MISMATCH = "REVERIFICATION_MISMATCH"
    VERIFICATION_BLOCKED = "VERIFICATION_BLOCKED"
    VERIFICATION_LEVEL_UNSUPPORTED = "VERIFICATION_LEVEL_UNSUPPORTED"


@dataclass(frozen=True, slots=True)
class ExecutionGateSnapshot:
    task_id: str
    task_version: int
    task_unit_id: str
    preflight_snapshot_id: str
    preflight_snapshot_digest: str
    preflight_stale_reasons: tuple[str, ...]
    review_revision_id: str
    review_version: int
    candidate_id: str | None
    source_inventory_digest: str
    metainfo_digest: str | None
    verification_source: ExecutionVerificationSource | None
    review_verification_id: str | None
    review_verification_digest: str | None
    verification_level: VerificationLevel | None
    eligible: bool
    client_check_required: bool
    blocked_reasons: tuple[ExecutionGateBlockReason, ...]
    created_at: datetime
    schema_version: str = EXECUTION_GATE_SCHEMA_VERSION
    gate_digest: str = ""

    def __post_init__(self) -> None:
        if self.task_version < 1 or self.review_version < 1:
            raise ValueError("execution gate 必须绑定有效 task/review version")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("execution gate 创建时间必须带时区")
        object.__setattr__(self, "created_at", self.created_at.astimezone(UTC))
        normalized_reasons = tuple(sorted(set(self.blocked_reasons), key=lambda item: item.value))
        object.__setattr__(self, "blocked_reasons", normalized_reasons)
        object.__setattr__(
            self, "preflight_stale_reasons", tuple(sorted(self.preflight_stale_reasons))
        )

        if self.eligible:
            if normalized_reasons:
                raise ValueError("eligible execution gate 不能同时包含阻断原因")
            if self.verification_level not in {
                VerificationLevel.FULL_VERIFIED,
                VerificationLevel.CLIENT_CHECK_REQUIRED,
            }:
                raise ValueError("eligible execution gate 必须具备可执行验证等级")
        expected_client_check = (
            self.eligible and self.verification_level is VerificationLevel.CLIENT_CHECK_REQUIRED
        )
        if self.client_check_required != expected_client_check:
            raise ValueError("client_check_required 与验证等级不一致")

        payload = execution_gate_to_payload(self, include_digest=False)
        payload.pop("created_at", None)
        digest = sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        if self.gate_digest and self.gate_digest != digest:
            raise ValueError("execution gate digest 与证据内容不一致")
        object.__setattr__(self, "gate_digest", digest)


def execution_gate_to_payload(
    snapshot: ExecutionGateSnapshot,
    *,
    include_digest: bool = True,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": snapshot.schema_version,
        "task_id": snapshot.task_id,
        "task_version": snapshot.task_version,
        "task_unit_id": snapshot.task_unit_id,
        "preflight_snapshot_id": snapshot.preflight_snapshot_id,
        "preflight_snapshot_digest": snapshot.preflight_snapshot_digest,
        "preflight_stale_reasons": list(snapshot.preflight_stale_reasons),
        "review_revision_id": snapshot.review_revision_id,
        "review_version": snapshot.review_version,
        "candidate_id": snapshot.candidate_id,
        "source_inventory_digest": snapshot.source_inventory_digest,
        "metainfo_digest": snapshot.metainfo_digest,
        "verification_source": (
            snapshot.verification_source.value if snapshot.verification_source is not None else None
        ),
        "review_verification_id": snapshot.review_verification_id,
        "review_verification_digest": snapshot.review_verification_digest,
        "verification_level": (
            snapshot.verification_level.value if snapshot.verification_level is not None else None
        ),
        "eligible": snapshot.eligible,
        "client_check_required": snapshot.client_check_required,
        "blocked_reasons": [item.value for item in snapshot.blocked_reasons],
        "created_at": snapshot.created_at.isoformat(),
    }
    if include_digest:
        payload["gate_digest"] = snapshot.gate_digest
    return payload
