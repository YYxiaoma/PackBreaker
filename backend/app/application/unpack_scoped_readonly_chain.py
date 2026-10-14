"""Compose read-only CarPT proposal, descriptor, and journal evidence.

The chain intentionally accepts *only* a pristine new scoped execution. It
never produces a permit, leases no descriptors, and cannot resume an external
operation. All returned results remain non-authorizing. Real write admission
requires a separately approved host scope and execution-time transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal

from sqlalchemy.orm import Session

from backend.app.application.unpack_scoped_descriptor_evidence import (
    ScopedDescriptorStatus,
    inspect_scoped_descriptor_evidence,
)
from backend.app.application.unpack_scoped_journal_audit import (
    ScopedJournalAuditStatus,
    audit_scoped_carpt_journal_rows,
)
from backend.app.application.unpack_scoped_write_proposal import ScopedWriteProposalInspector
from backend.app.domain.unpack_execution_plan import unpack_execution_plan_from_payload
from backend.app.infrastructure.persistence.models import UnpackExecutionItem


class ScopedChainStatus(StrEnum):
    BLOCKED_PROPOSAL = "BLOCKED_PROPOSAL"
    BLOCKED_TRUSTED_TARGET = "BLOCKED_TRUSTED_TARGET"
    BLOCKED_FROZEN_PLAN = "BLOCKED_FROZEN_PLAN"
    BLOCKED_DESCRIPTOR = "BLOCKED_DESCRIPTOR"
    BLOCKED_JOURNAL = "BLOCKED_JOURNAL"
    OBSERVED_ONLY_NO_GRANT = "OBSERVED_ONLY_NO_GRANT"


@dataclass(frozen=True, slots=True)
class ScopedChainReport:
    status: ScopedChainStatus
    detail: str = ""
    source_count: int = 0
    copy_bytes: int = 0
    writes_authorized: Literal[False] = False
    permits_replay: Literal[False] = False
    realtime_write_lease: Literal[False] = False
    downloader_contacted: Literal[False] = False
    media_content_read: Literal[False] = False


def inspect_pristine_scoped_carpt_chain(
    session: Session,
    inspector: ScopedWriteProposalInspector,
    *,
    execution_id: str,
    item_id: str,
    approved_parent: Path,
    expected_output: Path,
    now: datetime | None = None,
) -> ScopedChainReport:
    """Chain current metadata evidence without authorizing any side effects."""
    report = ScopedChainReport
    # An independent trusted parent/child is required: never derive the
    # approved filesystem root solely from the user-provided proposal.
    if (
        not approved_parent.is_absolute()
        or not expected_output.is_absolute()
        or expected_output.parent != approved_parent
    ):
        return report(ScopedChainStatus.BLOCKED_TRUSTED_TARGET)
    with session.no_autoflush:
        initial_snapshot = inspector._read_with_identity(now=now)
        if initial_snapshot is None:
            return report(ScopedChainStatus.BLOCKED_PROPOSAL)
        initial, file_identity = initial_snapshot
        if (
            initial["execution_id"] != execution_id
            or initial["item_id"] != item_id
            or initial["output_directory"] != expected_output.as_posix()
        ):
            return report(ScopedChainStatus.BLOCKED_PROPOSAL)
        if not inspector.matches_frozen_preflight(
            session, execution_id=execution_id, item_id=item_id, now=now
        ):
            return report(ScopedChainStatus.BLOCKED_PROPOSAL)
        item = session.get(UnpackExecutionItem, item_id)
        if (
            item is None
            or item.execution_plan is None
            or item.execution_plan_digest != initial["plan_digest"]
        ):
            return report(ScopedChainStatus.BLOCKED_FROZEN_PLAN)
        try:
            plan = unpack_execution_plan_from_payload(item.execution_plan)
        except (TypeError, ValueError):
            return report(ScopedChainStatus.BLOCKED_FROZEN_PLAN)
        if (
            plan.plan_digest != initial["plan_digest"]
            or plan.output_directory != initial["output_directory"]
            or plan.target_device != initial["target_device"]
        ):
            return report(ScopedChainStatus.BLOCKED_FROZEN_PLAN)

        evidence = inspect_scoped_descriptor_evidence(
            plan,
            source_directory=Path(initial["source_directory"]),
            source_filename=initial["source_filename"],
            approved_parent=approved_parent,
            expected_output=expected_output,
            proposal_metadata_matches=True,
        )
        if evidence.status is not ScopedDescriptorStatus.OBSERVED_ONLY_NO_GRANT:
            return report(ScopedChainStatus.BLOCKED_DESCRIPTOR, detail=evidence.status.value)

        # A revoked/edited proposal or newly reserved journal must not be
        # collapsed into a "valid" report. This double-check is only another
        # read observation; it is not an atomic runtime write gate.
        # The first ORM identity-map read may now be stale if another worker
        # changed the item while the filesystem evidence was collected.
        # Expire it before the second read; this is NOT a write lock or grant.
        session.expire_all()
        current_snapshot = inspector._read_with_identity(now=now)
        if (
            current_snapshot is None
            or current_snapshot != (initial, file_identity)
            or not inspector.matches_frozen_preflight(
                session, execution_id=execution_id, item_id=item_id, now=now
            )
        ):
            return report(ScopedChainStatus.BLOCKED_PROPOSAL)
        audit = audit_scoped_carpt_journal_rows(
            session,
            execution_id=execution_id,
            item_id=item_id,
            expected_plan_digest=initial["plan_digest"],
            scope_metadata_matches=True,
        )
        if audit.status is not ScopedJournalAuditStatus.NO_INTENT_NO_GRANT:
            return report(ScopedChainStatus.BLOCKED_JOURNAL, detail=audit.status.value)
        return ScopedChainReport(
            ScopedChainStatus.OBSERVED_ONLY_NO_GRANT,
            source_count=evidence.source_count,
            copy_bytes=evidence.copy_bytes,
        )
