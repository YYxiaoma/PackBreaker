"""Real SQLite journal payloads, synthetic scope; audit never permits writes."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select

from backend.app.application.unpack_scoped_journal_audit import (
    ScopedJournalAuditStatus,
    audit_scoped_carpt_journal_rows,
)
from backend.app.infrastructure.persistence.models import (
    UnpackExecution,
    UnpackExecutionItem,
    UnpackExternalOperationJournal,
    new_uuid,
)
from tests.api.test_unpack_v112_scoped_write_proposal import _setup

_COPY = "UNPACK_EXEC_MATERIALIZE_COPY"
_ADD = "UNPACK_EXEC_TRANSMISSION_ADD"
_VERIFY = "UNPACK_EXEC_TRANSMISSION_VERIFY"
_START = "UNPACK_EXEC_TRANSMISSION_START"
_HASH = "a" * 40
_NOW = datetime(2026, 10, 14, 9, tzinfo=UTC)


def _journal(
    *,
    item: str,
    digest: str,
    output: str,
    downloader: str,
    save_path: str,
    operation: str,
    status: str = "APPLIED",
    key: str | None = None,
    modify_target: dict[str, Any] | None = None,
    modify_intent: dict[str, Any] | None = None,
) -> UnpackExternalOperationJournal:
    target: dict[str, Any]
    owner = "packbreaker-unpack-" + hashlib.sha256(f"{item}:{digest}".encode()).hexdigest()[:16]
    intent: dict[str, Any] = {"plan_digest": digest, "schema_version": "synthetic"}
    if operation == _COPY:
        target = {
            "path": output + "/movie/synthetic-video.mkv",
            "torrent_path": "movie/synthetic-video.mkv",
        }
        intent.update(
            {
                "storage_mode": "COPY",
                "source_path": str(Path(output).parent / "isolated-source" / "synthetic-video.mkv"),
            }
        )
    elif operation == _ADD:
        target = {"downloader_id": downloader, "remote_save_path": save_path}
        intent.update(
            {
                "expected_hashes": [_HASH],
                "skip_checking": False,
                "ownership_tag": owner,
            }
        )
    elif operation in {_VERIFY, _START}:
        target = {"downloader_id": downloader, "torrent_hash": _HASH}
        intent.update({"remote_save_path": save_path, "ownership_tag": owner})
    else:
        target = {"path": output + "/movie"}
        intent.update({"directory_relative_path": "movie"})
    target.update(modify_target or {})
    intent.update(modify_intent or {})
    return UnpackExternalOperationJournal(
        id=new_uuid(),
        item_id=item,
        idempotency_key=key or new_uuid(),
        operation_type=operation,
        target=target,
        intent=intent,
        status=status,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _check(
    tmp_path: Path,
    operations: tuple[str, ...] = (),
    *,
    modes: tuple[str, ...] | None = None,
    changed_index: int = -1,
    target_change: dict[str, Any] | None = None,
    intent_change: dict[str, Any] | None = None,
    scope_ok: bool = True,
    marker: str | None = None,
    different_execution: bool = False,
) -> ScopedJournalAuditStatus:
    client, app, proposal, execution_id, item_id = _setup(tmp_path)
    try:
        factory = app.state.runtime.session_factory
        with factory() as session:
            item = session.get(UnpackExecutionItem, item_id)
            execution = session.get(UnpackExecution, execution_id)
            assert item is not None and execution is not None
            if marker is not None:
                execution.config_snapshot = {
                    **execution.config_snapshot,
                    "acceptance_mode": marker,
                }
            for i, operation in enumerate(operations):
                row = _journal(
                    item=item_id,
                    digest=proposal["plan_digest"],
                    output=proposal["output_directory"],
                    downloader=proposal["downloader_id"],
                    save_path=proposal["target_remote_save_path"],
                    operation=operation,
                    status=modes[i] if modes else "APPLIED",
                    modify_target=target_change if i == changed_index else None,
                    modify_intent=intent_change if i == changed_index else None,
                )
                session.add(row)
            session.commit()
        with factory() as session:
            before = session.scalar(select(func.count(UnpackExternalOperationJournal.id)))
            result = audit_scoped_carpt_journal_rows(
                session,
                execution_id="not-original" if different_execution else execution_id,
                item_id=item_id,
                expected_plan_digest=proposal["plan_digest"],
                scope_metadata_matches=scope_ok,
            )
            assert result.writes_authorized is False
            assert result.client_contacted is False
            assert result.ownership_verified is False
            assert result.permits_replay is False
            if result.status not in {
                ScopedJournalAuditStatus.BLOCKED_SCOPE,
                ScopedJournalAuditStatus.NO_INTENT_NO_GRANT,
            }:
                assert result.journal_count == len(operations)
            assert session.scalar(select(func.count(UnpackExternalOperationJournal.id))) == before
            return result.status
    finally:
        client.__exit__(None, None, None)


def test_no_intent_is_never_a_write_grant(tmp_path: Path) -> None:
    assert _check(tmp_path) is ScopedJournalAuditStatus.NO_INTENT_NO_GRANT


def test_applied_copy_is_still_only_evidence(tmp_path: Path) -> None:
    assert _check(tmp_path, (_COPY,)) is ScopedJournalAuditStatus.APPLIED_EVIDENCE_ONLY


def test_copy_add_verify_start_shapes_from_real_worker_pass_only_as_evidence(
    tmp_path: Path,
) -> None:
    assert (
        _check(tmp_path, (_COPY, _ADD, _VERIFY, _START))
        is ScopedJournalAuditStatus.APPLIED_EVIDENCE_ONLY
    )


@pytest.mark.parametrize("mode", ("READ_ONLY_SITE_ACCEPTANCE", "UNKNOWN_MODE"))
def test_original_frozen_and_unknown_mode_are_never_accepted(tmp_path: Path, mode: str) -> None:
    assert _check(tmp_path, (_COPY,), marker=mode) is ScopedJournalAuditStatus.BLOCKED_SCOPE


@pytest.mark.parametrize(
    ("operations", "states", "expected"),
    [
        ((_COPY,), ("INTENT_RECORDED",), ScopedJournalAuditStatus.RESULT_UNKNOWN),
        ((_COPY,), ("RECONCILE_REQUIRED",), ScopedJournalAuditStatus.RECONCILE_REQUIRED),
        ((_COPY,), ("ROLLBACK_BLOCKED",), ScopedJournalAuditStatus.RECONCILE_REQUIRED),
        ((_COPY, _ADD), ("APPLIED", "INTENT_RECORDED"), ScopedJournalAuditStatus.RESULT_UNKNOWN),
        (
            (_COPY, _ADD, _VERIFY),
            ("APPLIED", "APPLIED", "INTENT_RECORDED"),
            ScopedJournalAuditStatus.RESULT_UNKNOWN,
        ),
        (
            (_COPY, _ADD, _VERIFY, _START),
            ("APPLIED", "APPLIED", "APPLIED", "INTENT_RECORDED"),
            ScopedJournalAuditStatus.RESULT_UNKNOWN,
        ),
        ((_COPY,), ("NOOP",), ScopedJournalAuditStatus.BLOCKED_JOURNAL),
        ((_COPY,), ("ROLLED_BACK",), ScopedJournalAuditStatus.BLOCKED_JOURNAL),
    ],
)
def test_unknown_or_reconciliation_states_never_replay(
    tmp_path: Path,
    operations: tuple[str, ...],
    states: tuple[str, ...],
    expected: ScopedJournalAuditStatus,
) -> None:
    assert _check(tmp_path, operations, modes=states) is expected


@pytest.mark.parametrize(
    ("ops", "expected"),
    [
        ((_ADD,), ScopedJournalAuditStatus.BLOCKED_SEQUENCE),
        ((_COPY, _VERIFY), ScopedJournalAuditStatus.BLOCKED_SEQUENCE),
        ((_COPY, _ADD, _START), ScopedJournalAuditStatus.BLOCKED_SEQUENCE),
        ((_COPY, _ADD, _VERIFY, _START, _START), ScopedJournalAuditStatus.BLOCKED_JOURNAL),
    ],
)
def test_missing_prerequisites_are_blocked(
    tmp_path: Path, ops: tuple[str, ...], expected: ScopedJournalAuditStatus
) -> None:
    assert _check(tmp_path, ops) is expected


@pytest.mark.parametrize(
    ("target", "intent"),
    [
        ({"path": "/elsewhere/owned"}, None),
        ({"torrent_path": "../escape"}, None),
        ({"downloader_id": "other-downloader"}, None),
        ({"torrent_hash": "different"}, None),
        (None, {"plan_digest": "b" * 64}),
        (None, {"remote_save_path": "/remote/other"}),
    ],
)
def test_mismatched_real_journal_payload_never_claims_success(
    tmp_path: Path,
    target: dict[str, Any] | None,
    intent: dict[str, Any] | None,
) -> None:
    ops = (_COPY, _ADD, _VERIFY)
    change_index = 0 if target and "path" in target or target and "torrent_path" in target else 2
    assert _check(
        tmp_path, ops, changed_index=change_index, target_change=target, intent_change=intent
    ) in {
        ScopedJournalAuditStatus.BLOCKED_JOURNAL,
        ScopedJournalAuditStatus.BLOCKED_SEQUENCE,
    }


def test_scoped_proof_required_even_with_applied_rows(tmp_path: Path) -> None:
    assert _check(tmp_path, (_COPY,), scope_ok=False) is ScopedJournalAuditStatus.BLOCKED_SCOPE


def test_another_execution_cannot_reuse_a_ledger(tmp_path: Path) -> None:
    assert (
        _check(tmp_path, (_COPY,), different_execution=True)
        is ScopedJournalAuditStatus.BLOCKED_SCOPE
    )


@pytest.mark.parametrize(
    ("ops", "index", "target", "intent"),
    [
        ((_COPY,), 0, None, {"source_path": "/tmp/foreign-file"}),
        ((_COPY, _ADD), 1, None, {"ownership_tag": "reassigned"}),
        ((_COPY, _ADD, _VERIFY), 2, None, {"ownership_tag": "different-owner"}),
        ((_COPY, _ADD, _VERIFY), 1, None, {"expected_hashes": ["invalid-hash"]}),
        ((_COPY, _ADD, _VERIFY), 2, {"torrent_hash": "Z" * 40}, None),
        ((_COPY, _ADD, _VERIFY), 2, None, {"remote_save_path": "/other"}),
        ((_COPY, _ADD, _VERIFY), 1, None, {"skip_checking": True}),
        ((_COPY, _ADD, _VERIFY), 2, {"downloader_id": "unauthorized"}, None),
    ],
)
def test_real_journal_exact_site_and_client_scope_binding(
    tmp_path: Path,
    ops: tuple[str, ...],
    index: int,
    target: dict[str, Any] | None,
    intent: dict[str, Any] | None,
) -> None:
    result = _check(tmp_path, ops, changed_index=index, target_change=target, intent_change=intent)
    assert result in {
        ScopedJournalAuditStatus.BLOCKED_JOURNAL,
        ScopedJournalAuditStatus.BLOCKED_SEQUENCE,
    }


@pytest.mark.parametrize(
    "path",
    [
        "/isolated/target/evil\x00filename",
        "/isolated/target/bad\\name",
        "/isolated/target/../escape",
        "/isolated/target//repeated",
        "/isolated/target/./dot",
    ],
)
def test_journal_path_guard_rejects_malformed_components(path: str) -> None:
    from backend.app.application.unpack_scoped_journal_audit import _inside

    assert not _inside("/isolated/target", path)
