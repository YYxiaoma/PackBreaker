"""Transaction-fenced first-intent readiness evidence, with NO write grant.

This dry-run bridge deliberately holds SQLite BEGIN IMMEDIATE for the entire
identity/scope/descriptor/journal check, so a rival journal transaction cannot
reserve an operation concurrently. It NEVER inserts a row or returns a
permission. It cannot be used to bypass acceptance Worker guards.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.unpack_scoped_readonly_chain import (
    ScopedChainStatus,
    inspect_pristine_scoped_carpt_chain,
)
from backend.app.application.unpack_scoped_write_proposal import ScopedWriteProposalInspector
from backend.app.domain.unpack_execution_plan import (
    UnpackExecutionActionKind,
    unpack_execution_plan_from_payload,
)
from backend.app.infrastructure.persistence.database import begin_immediate_write
from backend.app.infrastructure.persistence.models import (
    UnpackExecutionItem,
    UnpackExternalOperationJournal,
)

_DIR = "UNPACK_EXEC_CREATE_DIRECTORY"
_COPY = "UNPACK_EXEC_MATERIALIZE_COPY"


class ScopedInitialIntentStatus(StrEnum):
    BLOCKED_INPUT = "BLOCKED_INPUT"
    BLOCKED_CHAIN = "BLOCKED_CHAIN"
    BLOCKED_OPERATION = "BLOCKED_OPERATION"
    BLOCKED_KEY_CONFLICT = "BLOCKED_KEY_CONFLICT"
    BLOCKED_TRANSACTION = "BLOCKED_TRANSACTION"
    FIRST_INTENT_SHAPE_ONLY_NO_GRANT = "FIRST_INTENT_SHAPE_ONLY_NO_GRANT"


@dataclass(frozen=True, slots=True)
class ScopedInitialIntentRequest:
    execution_id: str
    item_id: str
    plan_digest: str
    operation_type: str
    target_path: str
    source_path: str | None
    relative_path: str | None
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class ScopedInitialIntentReport:
    status: ScopedInitialIntentStatus
    detail: str = ""
    writes_authorized: Literal[False] = False
    journal_created: Literal[False] = False
    permits_replay: Literal[False] = False
    host_approval_obtained: Literal[False] = False
    downloader_contacted: Literal[False] = False
    media_content_read: Literal[False] = False


def _inspect_first_intent_inside_locked_session(
    session: Session,
    inspector: ScopedWriteProposalInspector,
    request: ScopedInitialIntentRequest,
    *,
    approved_parent: Path,
    expected_output: Path,
    now: datetime | None = None,
) -> ScopedInitialIntentReport:
    """Only evidence inside caller's BEGIN IMMEDIATE. Never grants or inserts.

    For atomicity experiments only; it is not a permission source and must not
    be used as authorization by a live acceptance Worker.
    """
    report = ScopedInitialIntentReport
    if (
        not request.execution_id
        or not request.item_id
        or not request.idempotency_key
        or len(request.idempotency_key) > 64
        or len(request.plan_digest) != 64
        or any(c not in "0123456789abcdef" for c in request.plan_digest)
        or request.operation_type not in {_DIR, _COPY}
    ):
        return report(ScopedInitialIntentStatus.BLOCKED_INPUT)
    checked = inspect_pristine_scoped_carpt_chain(
        session,
        inspector,
        execution_id=request.execution_id,
        item_id=request.item_id,
        approved_parent=approved_parent,
        expected_output=expected_output,
        now=now,
    )
    if checked.status is not ScopedChainStatus.OBSERVED_ONLY_NO_GRANT:
        return report(ScopedInitialIntentStatus.BLOCKED_CHAIN, checked.status.value)
    item = session.get(UnpackExecutionItem, request.item_id)
    if (
        item is None
        or item.execution_plan_digest != request.plan_digest
        or item.execution_plan is None
    ):
        return report(ScopedInitialIntentStatus.BLOCKED_OPERATION)
    try:
        plan = unpack_execution_plan_from_payload(item.execution_plan)
    except (TypeError, ValueError):
        return report(ScopedInitialIntentStatus.BLOCKED_OPERATION)
    if plan.plan_digest != request.plan_digest:
        return report(ScopedInitialIntentStatus.BLOCKED_OPERATION)

    # Exactly one first operation may be shaped. All subsequent
    # intents require separately designed APPLIED/recovery gates.
    if plan.create_directories:
        first_relative = plan.create_directories[0]
        valid = (
            request.operation_type == _DIR
            and request.relative_path == first_relative
            and request.source_path is None
            and request.target_path == plan.output_directory + "/" + first_relative
        )
    else:
        actions = [
            action
            for action in plan.actions
            if action.kind is UnpackExecutionActionKind.MATERIALIZE
        ]
        first = actions[0] if actions else None
        valid = (
            first is not None
            and request.operation_type == _COPY
            and request.relative_path == first.torrent_path
            and request.source_path == first.source_path
            and request.target_path == plan.output_directory + "/" + first.torrent_path
        )
    if not valid:
        return report(ScopedInitialIntentStatus.BLOCKED_OPERATION)

    # Idempotency keys are unique across ALL execution items, not
    # only current item's journal. The actual Worker must recheck
    # this in its own commit transaction.
    conflicting_key = session.scalar(
        select(UnpackExternalOperationJournal.id).where(
            UnpackExternalOperationJournal.idempotency_key == request.idempotency_key
        )
    )
    if conflicting_key is not None:
        return report(ScopedInitialIntentStatus.BLOCKED_KEY_CONFLICT)
    return report(ScopedInitialIntentStatus.FIRST_INTENT_SHAPE_ONLY_NO_GRANT)


def inspect_initial_scoped_intent_transaction(
    session_factory: sessionmaker[Session],
    inspector: ScopedWriteProposalInspector,
    request: ScopedInitialIntentRequest,
    *,
    approved_parent: Path,
    expected_output: Path,
    now: datetime | None = None,
) -> ScopedInitialIntentReport:
    """Take a real SQLite lock, inspect only, rollback; never reserve intent."""
    report = ScopedInitialIntentReport
    try:
        with session_factory() as session:
            begin_immediate_write(session)
            try:
                return _inspect_first_intent_inside_locked_session(
                    session,
                    inspector,
                    request,
                    approved_parent=approved_parent,
                    expected_output=expected_output,
                    now=now,
                )
            finally:
                session.rollback()
    except Exception:
        # Infrastructure errors must not turn into implicit approval.
        return report(ScopedInitialIntentStatus.BLOCKED_TRANSACTION)
