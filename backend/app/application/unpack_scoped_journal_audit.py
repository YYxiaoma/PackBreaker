"""Read-only audit of REAL unpack journal row shapes for proposed CarPT scope.

No scope document, record, flag, or returned status authorizes a write. This
adapter intentionally bridges production SQLite journal payloads to offline
risk evidence without calling the download client or reading media bytes.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.application.unpack_scoped_write_proposal import (
    PROPOSED_WRITE_ACCEPTANCE_MARKER,
)
from backend.app.domain.operation import OperationStatus
from backend.app.domain.unpack_execution_plan import (
    UnpackExecutionActionKind,
    unpack_execution_plan_from_payload,
)
from backend.app.infrastructure.persistence.models import (
    UnpackExecution,
    UnpackExecutionItem,
    UnpackExternalOperationJournal,
)

_DIRECTORY = "UNPACK_EXEC_CREATE_DIRECTORY"
_COPY = "UNPACK_EXEC_MATERIALIZE_COPY"
_ADD = "UNPACK_EXEC_TRANSMISSION_ADD"
_VERIFY = "UNPACK_EXEC_TRANSMISSION_VERIFY"
_START = "UNPACK_EXEC_TRANSMISSION_START"
_ALLOWED = frozenset({_DIRECTORY, _COPY, _ADD, _VERIFY, _START})
_RECONCILE = frozenset(
    {
        OperationStatus.RECONCILE_REQUIRED.value,
        OperationStatus.ROLLBACK_PENDING.value,
        OperationStatus.ROLLBACK_BLOCKED.value,
    }
)
_KNOWN = frozenset({OperationStatus.INTENT_RECORDED.value, OperationStatus.APPLIED.value})


class ScopedJournalAuditStatus(StrEnum):
    BLOCKED_SCOPE = "BLOCKED_SCOPE"
    BLOCKED_JOURNAL = "BLOCKED_JOURNAL"
    BLOCKED_SEQUENCE = "BLOCKED_SEQUENCE"
    RECONCILE_REQUIRED = "RECONCILE_REQUIRED"
    RESULT_UNKNOWN = "RESULT_UNKNOWN"
    NO_INTENT_NO_GRANT = "NO_INTENT_NO_GRANT"
    APPLIED_EVIDENCE_ONLY = "APPLIED_EVIDENCE_ONLY"


@dataclass(frozen=True, slots=True)
class ScopedJournalAuditReport:
    status: ScopedJournalAuditStatus
    journal_count: int = 0
    applied_count: int = 0
    writes_authorized: Literal[False] = False
    client_contacted: Literal[False] = False
    ownership_verified: Literal[False] = False
    permits_replay: Literal[False] = False


def _inside(root: str, value: object) -> bool:
    if (
        not isinstance(value, str)
        or not value.startswith(root + "/")
        or "//" in value
        or "\\x00" in value
        or "\\\\" in value
        or str(PurePosixPath(value)) != value
    ):
        return False
    return all(p not in {".", ".."} for p in value.split("/"))


def audit_scoped_carpt_journal_rows(
    session: Session,
    *,
    execution_id: str,
    item_id: str,
    expected_plan_digest: str,
    scope_metadata_matches: bool,
) -> ScopedJournalAuditReport:
    """Read only, including unknown-result journal states. Never authorizes replay."""
    report = ScopedJournalAuditReport
    if not scope_metadata_matches or not execution_id or not item_id:
        return report(ScopedJournalAuditStatus.BLOCKED_SCOPE)
    with session.no_autoflush:
        execution = session.get(UnpackExecution, execution_id)
        item = session.get(UnpackExecutionItem, item_id)
        if (
            execution is None
            or item is None
            or item.execution_id != execution.id
            or not isinstance(execution.config_snapshot, dict)
            or execution.config_snapshot.get("acceptance_mode") != PROPOSED_WRITE_ACCEPTANCE_MARKER
            or item.execution_plan_digest != expected_plan_digest
            or item.execution_plan is None
        ):
            return report(ScopedJournalAuditStatus.BLOCKED_SCOPE)
        try:
            plan = unpack_execution_plan_from_payload(item.execution_plan)
        except (ValueError, TypeError):
            return report(ScopedJournalAuditStatus.BLOCKED_SCOPE)
        if (
            not plan.ready
            or plan.plan_digest != expected_plan_digest
            or plan.item_id != item_id
            or plan.candidate_id != item.selected_candidate_id
            or plan.candidate_generation != item.candidate_generation
            or plan.storage_mode.value != "COPY"
            or plan.conflict_policy.value != "VERIFY_REUSE_OR_STOP"
            or not plan.target_downloader_id
            or not plan.target_remote_save_path
        ):
            return report(ScopedJournalAuditStatus.BLOCKED_SCOPE)
        rows = session.scalars(
            select(UnpackExternalOperationJournal)
            .where(UnpackExternalOperationJournal.item_id == item_id)
            .order_by(UnpackExternalOperationJournal.created_at, UnpackExternalOperationJournal.id)
        ).all()
        if not rows:
            return report(ScopedJournalAuditStatus.NO_INTENT_NO_GRANT)

        expected_copy: dict[str, tuple[str, str | None]] = {}
        for action in plan.actions:
            if action.kind is not UnpackExecutionActionKind.MATERIALIZE:
                continue
            expected_copy[action.torrent_path] = (
                plan.output_directory + "/" + action.torrent_path,
                action.source_path,
            )
        if not expected_copy:
            return report(ScopedJournalAuditStatus.BLOCKED_SCOPE)

        applied: set[str] = set()
        seen_keys: set[str] = set()
        seen_targets: set[tuple[str, str]] = set()
        statuses: list[str] = []
        expected_owner = (
            "packbreaker-unpack-"
            + hashlib.sha256(f"{item_id}:{expected_plan_digest}".encode()).hexdigest()[:16]
        )
        add_expected_hashes: set[str] = set()
        client_hash: str | None = None
        add_ownership_tag: str | None = None
        client_ownership_tags: set[str] = set()
        count_applied = 0
        for row in rows:
            operation, target, intent = row.operation_type, row.target, row.intent
            if (
                operation not in _ALLOWED
                or not isinstance(target, dict)
                or not isinstance(intent, dict)
                or not isinstance(row.idempotency_key, str)
                or not row.idempotency_key
                or row.idempotency_key in seen_keys
                or intent.get("plan_digest") != expected_plan_digest
                or (
                    operation in {_ADD, _VERIFY, _START}
                    and intent.get("ownership_tag") != expected_owner
                )
                or row.status not in (_KNOWN | _RECONCILE)
            ):
                return report(ScopedJournalAuditStatus.BLOCKED_JOURNAL, len(rows))
            seen_keys.add(row.idempotency_key)
            if operation in {_DIRECTORY, _COPY}:
                value = target.get("path")
                if not isinstance(value, str) or not _inside(plan.output_directory, value):
                    return report(ScopedJournalAuditStatus.BLOCKED_JOURNAL, len(rows))
                if operation == _COPY:
                    torrent_path = target.get("torrent_path")
                    if (
                        not isinstance(torrent_path, str)
                        or expected_copy.get(torrent_path) != (value, intent.get("source_path"))
                        or intent.get("storage_mode") != "COPY"
                    ):
                        return report(ScopedJournalAuditStatus.BLOCKED_JOURNAL, len(rows))
                else:
                    relative = intent.get("directory_relative_path")
                    if (
                        not isinstance(relative, str)
                        or value != plan.output_directory + "/" + relative
                        or relative not in plan.create_directories
                    ):
                        return report(ScopedJournalAuditStatus.BLOCKED_JOURNAL, len(rows))
                signature = (operation, value)
            elif operation == _ADD:
                hashes = intent.get("expected_hashes")
                if (
                    target
                    != {
                        "downloader_id": plan.target_downloader_id,
                        "remote_save_path": plan.target_remote_save_path,
                    }
                    or not isinstance(hashes, list)
                    or not hashes
                    or any(
                        not isinstance(h, str)
                        or len(h) not in (40, 64)
                        or any(c not in "0123456789abcdef" for c in h)
                        for h in hashes
                    )
                    or intent.get("skip_checking") is not False
                    or not isinstance(intent.get("ownership_tag"), str)
                    or not intent["ownership_tag"]
                ):
                    return report(ScopedJournalAuditStatus.BLOCKED_JOURNAL, len(rows))
                add_expected_hashes = set(hashes)
                add_ownership_tag = intent["ownership_tag"]
                signature = (operation, str(plan.target_downloader_id))
            else:
                torrent_hash = target.get("torrent_hash")
                if (
                    set(target) != {"downloader_id", "torrent_hash"}
                    or target["downloader_id"] != plan.target_downloader_id
                    or not isinstance(torrent_hash, str)
                    or len(torrent_hash) not in (40, 64)
                    or any(c not in "0123456789abcdef" for c in torrent_hash)
                    or intent.get("remote_save_path") != plan.target_remote_save_path
                    or not isinstance(intent.get("ownership_tag"), str)
                    or not intent["ownership_tag"]
                ):
                    return report(ScopedJournalAuditStatus.BLOCKED_JOURNAL, len(rows))
                if client_hash is not None and client_hash != torrent_hash:
                    return report(ScopedJournalAuditStatus.BLOCKED_JOURNAL, len(rows))
                client_hash = torrent_hash
                client_ownership_tags.add(intent["ownership_tag"])
                signature = (operation, torrent_hash)
            if signature in seen_targets:
                return report(ScopedJournalAuditStatus.BLOCKED_JOURNAL, len(rows))
            seen_targets.add(signature)
            statuses.append(row.status)
            if row.status == OperationStatus.APPLIED.value:
                applied.add(operation)
                count_applied += 1
        if any(status in _RECONCILE for status in statuses):
            return report(ScopedJournalAuditStatus.RECONCILE_REQUIRED, len(rows), count_applied)
        # A completed COPY is a prerequisite for any client operation, and
        # ADD's own expected hash must bind the verify/start hash. Never use
        # request acceptance alone as verification or ownership evidence.
        for row in rows:
            if (
                row.operation_type == _COPY
                and row.status != OperationStatus.APPLIED.value
                and any(r.operation_type in {_ADD, _VERIFY, _START} for r in rows)
            ):
                return report(ScopedJournalAuditStatus.BLOCKED_SEQUENCE, len(rows), count_applied)
        if any(row.operation_type in {_ADD, _VERIFY, _START} for row in rows):
            copies = {
                row.target["torrent_path"]
                for row in rows
                if row.operation_type == _COPY and row.status == OperationStatus.APPLIED.value
            }
            if copies != set(expected_copy):
                return report(ScopedJournalAuditStatus.BLOCKED_SEQUENCE, len(rows), count_applied)
        requested = {row.operation_type for row in rows}
        if _VERIFY in requested and _ADD not in applied:
            return report(ScopedJournalAuditStatus.BLOCKED_SEQUENCE, len(rows), count_applied)
        if _START in requested and _VERIFY not in applied:
            return report(ScopedJournalAuditStatus.BLOCKED_SEQUENCE, len(rows), count_applied)
        if (_VERIFY in applied or _START in applied) and (
            client_hash is None or client_hash not in add_expected_hashes
        ):
            return report(ScopedJournalAuditStatus.BLOCKED_SEQUENCE, len(rows), count_applied)
        if client_ownership_tags and (
            add_ownership_tag is None or client_ownership_tags != {add_ownership_tag}
        ):
            return report(ScopedJournalAuditStatus.BLOCKED_JOURNAL, len(rows), count_applied)
        if any(status == OperationStatus.INTENT_RECORDED.value for status in statuses):
            return report(ScopedJournalAuditStatus.RESULT_UNKNOWN, len(rows), count_applied)
        return report(ScopedJournalAuditStatus.APPLIED_EVIDENCE_ONLY, len(rows), count_applied)
