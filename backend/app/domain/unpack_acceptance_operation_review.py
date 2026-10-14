"""Offline-only journal/operation risk classification for scoped acceptance.

Pure metadata analysis: no filesystem, SQLite, downloader, host approval, or
write-worker calls. NONE of these decisions authorizes a side effect.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Literal

from backend.app.domain.operation import OperationStatus

_SCOPED_OPERATIONS = frozenset(
    {
        "UNPACK_EXEC_CREATE_DIRECTORY",
        "UNPACK_EXEC_MATERIALIZE_COPY",
        "UNPACK_EXEC_TRANSMISSION_ADD",
        "UNPACK_EXEC_TRANSMISSION_VERIFY",
        "UNPACK_EXEC_TRANSMISSION_START",
    }
)
_DOWNLOAD_OPERATIONS = frozenset(
    {
        "UNPACK_EXEC_TRANSMISSION_ADD",
        "UNPACK_EXEC_TRANSMISSION_VERIFY",
        "UNPACK_EXEC_TRANSMISSION_START",
    }
)
_FS_OPERATIONS = _SCOPED_OPERATIONS - _DOWNLOAD_OPERATIONS


class OfflineReviewStatus(StrEnum):
    """Metadata evidence classification, never an execution grant."""

    BLOCKED_IDENTITY = "BLOCKED_IDENTITY"
    BLOCKED_TARGET = "BLOCKED_TARGET"
    BLOCKED_SEQUENCE = "BLOCKED_SEQUENCE"
    JOURNAL_CONFLICT = "JOURNAL_CONFLICT"
    RECONCILE_REQUIRED = "RECONCILE_REQUIRED"
    INTENT_NOT_RECORDED = "INTENT_NOT_RECORDED"
    INTENT_RESULT_UNKNOWN = "INTENT_RESULT_UNKNOWN"
    APPLIED_DO_NOT_REPLAY = "APPLIED_DO_NOT_REPLAY"
    METADATA_REVIEW_ONLY = "METADATA_REVIEW_ONLY"


@dataclass(frozen=True, slots=True)
class OfflineJournalEvidence:
    """Extracted from a synthetic/read-only operation ledger, without secrets."""

    item_id: str
    plan_digest: str
    operation_type: str
    idempotency_key: str
    target_path: str
    status: str


@dataclass(frozen=True, slots=True)
class OfflineOperationReviewRequest:
    execution_id: str
    item_id: str
    plan_digest: str
    scope_execution_id: str
    scope_item_id: str
    scope_plan_digest: str
    item_version_expected: int
    item_version_observed: int
    operation_type: str
    target_path: str
    idempotency_key: str
    output_directory: str
    target_remote_save_path: str
    expected_copy_actions: int
    proposal_metadata_matches: bool
    proposal_file_current: bool


@dataclass(frozen=True, slots=True)
class OfflineOperationReview:
    status: OfflineReviewStatus
    writes_authorized: Literal[False] = False
    realtime_proof_verified: Literal[False] = False
    external_client_contacted: Literal[False] = False


def _safe_target(value: str) -> bool:
    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or value == "/"
        or "\x00" in value
        or "\\" in value
        or "//" in value
    ):
        return False
    return str(PurePosixPath(value)) == value and all(
        part not in {".", ".."} for part in value.split("/")
    )


def review_scoped_operation_journal(
    request: OfflineOperationReviewRequest,
    evidence: tuple[OfflineJournalEvidence, ...],
) -> OfflineOperationReview:
    """Classify a hypothetical operation; callers MUST NOT treat result as permit."""
    if (
        not request.proposal_metadata_matches
        or not request.proposal_file_current
        or not request.execution_id
        or not request.item_id
        or request.execution_id != request.scope_execution_id
        or request.item_id != request.scope_item_id
        or request.plan_digest != request.scope_plan_digest
        or len(request.plan_digest) != 64
        or any(c not in "0123456789abcdef" for c in request.plan_digest)
        or request.item_version_expected < 1
        or request.item_version_observed != request.item_version_expected
        or not request.idempotency_key
        or request.expected_copy_actions < 1
        or request.operation_type not in _SCOPED_OPERATIONS
    ):
        return OfflineOperationReview(OfflineReviewStatus.BLOCKED_IDENTITY)
    if not (
        _safe_target(request.output_directory)
        and _safe_target(request.target_remote_save_path)
        and _safe_target(request.target_path)
    ):
        return OfflineOperationReview(OfflineReviewStatus.BLOCKED_TARGET)
    if request.operation_type in _FS_OPERATIONS:
        if not request.target_path.startswith(request.output_directory + "/"):
            return OfflineOperationReview(OfflineReviewStatus.BLOCKED_TARGET)
    elif request.target_path != request.target_remote_save_path:
        return OfflineOperationReview(OfflineReviewStatus.BLOCKED_TARGET)

    seen: set[str] = set()
    for item in evidence:
        if (
            item.item_id != request.item_id
            or item.plan_digest != request.plan_digest
            or item.operation_type not in _SCOPED_OPERATIONS
            or not item.idempotency_key
            or item.idempotency_key in seen
            or not _safe_target(item.target_path)
            or (
                item.operation_type in _FS_OPERATIONS
                and not item.target_path.startswith(request.output_directory + "/")
            )
            or (
                item.operation_type in _DOWNLOAD_OPERATIONS
                and item.target_path != request.target_remote_save_path
            )
        ):
            return OfflineOperationReview(OfflineReviewStatus.JOURNAL_CONFLICT)
        seen.add(item.idempotency_key)
        if item.status in {
            OperationStatus.RECONCILE_REQUIRED.value,
            OperationStatus.ROLLBACK_PENDING.value,
            OperationStatus.ROLLBACK_BLOCKED.value,
        }:
            return OfflineOperationReview(OfflineReviewStatus.RECONCILE_REQUIRED)
        if item.status not in {
            OperationStatus.INTENT_RECORDED.value,
            OperationStatus.APPLIED.value,
            OperationStatus.NOOP.value,
            OperationStatus.ROLLED_BACK.value,
        }:
            return OfflineOperationReview(OfflineReviewStatus.JOURNAL_CONFLICT)

    # Independently keyed APPLIED operations for the same target imply drift.
    applied_targets: set[tuple[str, str]] = set()
    for item in evidence:
        if item.status != OperationStatus.APPLIED.value:
            continue
        signature = (item.operation_type, item.target_path)
        if signature in applied_targets:
            return OfflineOperationReview(OfflineReviewStatus.JOURNAL_CONFLICT)
        applied_targets.add(signature)

    current = [item for item in evidence if item.idempotency_key == request.idempotency_key]
    if current and (
        len(current) != 1
        or current[0].operation_type != request.operation_type
        or current[0].target_path != request.target_path
    ):
        return OfflineOperationReview(OfflineReviewStatus.JOURNAL_CONFLICT)

    applied = [item for item in evidence if item.status == OperationStatus.APPLIED.value]
    if request.operation_type in _DOWNLOAD_OPERATIONS:
        copies = [item for item in applied if item.operation_type == "UNPACK_EXEC_MATERIALIZE_COPY"]
        if len(copies) != request.expected_copy_actions:
            return OfflineOperationReview(OfflineReviewStatus.BLOCKED_SEQUENCE)
        if (
            request.operation_type
            in {
                "UNPACK_EXEC_TRANSMISSION_VERIFY",
                "UNPACK_EXEC_TRANSMISSION_START",
            }
            and len(
                [item for item in applied if item.operation_type == "UNPACK_EXEC_TRANSMISSION_ADD"]
            )
            != 1
        ):
            return OfflineOperationReview(OfflineReviewStatus.BLOCKED_SEQUENCE)
        if (
            request.operation_type == "UNPACK_EXEC_TRANSMISSION_START"
            and len(
                [
                    item
                    for item in applied
                    if item.operation_type == "UNPACK_EXEC_TRANSMISSION_VERIFY"
                ]
            )
            != 1
        ):
            return OfflineOperationReview(OfflineReviewStatus.BLOCKED_SEQUENCE)

    if any(
        item.status == OperationStatus.INTENT_RECORDED.value
        and item.idempotency_key != request.idempotency_key
        for item in evidence
    ):
        return OfflineOperationReview(OfflineReviewStatus.INTENT_RESULT_UNKNOWN)

    if not current:
        # No durable intent exists. Never perform an operation from this result.
        return OfflineOperationReview(OfflineReviewStatus.INTENT_NOT_RECORDED)
    if current[0].status == OperationStatus.INTENT_RECORDED.value:
        # Crash after intent leaves the physical result unknown, even for ADD.
        return OfflineOperationReview(OfflineReviewStatus.INTENT_RESULT_UNKNOWN)
    if current[0].status == OperationStatus.APPLIED.value:
        return OfflineOperationReview(OfflineReviewStatus.APPLIED_DO_NOT_REPLAY)
    # NOOP / ROLLED_BACK cannot be construed as fresh permission or ownership.
    return OfflineOperationReview(OfflineReviewStatus.METADATA_REVIEW_ONLY)
