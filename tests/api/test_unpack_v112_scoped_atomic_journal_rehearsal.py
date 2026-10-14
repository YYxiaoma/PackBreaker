"""Synthetic-only rehearsal of atomic journal intent reservation.

CRITICAL: This file lives under tests/. The fake recorder deliberately
simulates a host authorization that DOES NOT EXIST in production. It is not
imported from app code. No external side effects, media writes, or downloader
calls occur. The real acceptance workers remain fail closed.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from backend.app.application.unpack_scoped_initial_intent import (
    ScopedInitialIntentRequest,
    ScopedInitialIntentStatus,
    _inspect_first_intent_inside_locked_session,
)
from backend.app.application.unpack_scoped_write_proposal import ScopedWriteProposalInspector
from backend.app.infrastructure.persistence.database import begin_immediate_write
from backend.app.infrastructure.persistence.models import (
    UnpackExecution,
    UnpackExternalOperationJournal,
)
from tests.api.test_unpack_v112_scoped_initial_intent import _first
from tests.api.test_unpack_v112_scoped_journal_audit import _journal
from tests.api.test_unpack_v112_scoped_readonly_chain import _scenario
from tests.api.test_unpack_v112_scoped_write_proposal import _NOW, _rewrite

_DIR = "UNPACK_EXEC_CREATE_DIRECTORY"


def _rows(app: Any) -> list[UnpackExternalOperationJournal]:
    with app.state.runtime.session_factory() as session:
        return list(session.scalars(select(UnpackExternalOperationJournal)).all())


def _rehearse_reservation(
    app: Any,
    proposal: dict[str, Any],
    parent: Path,
    output: Path,
    request: ScopedInitialIntentRequest,
    *,
    test_only_commit: bool = False,
    simulate_crash_before_commit: bool = False,
) -> ScopedInitialIntentStatus:
    """Test fixture explicitly simulates a missing host grant: NEVER production."""
    factory = app.state.runtime.session_factory
    inspector = ScopedWriteProposalInspector(app.state.settings.config_dir)
    with factory() as session:
        begin_immediate_write(session)
        try:
            result = _inspect_first_intent_inside_locked_session(
                session,
                inspector,
                request,
                approved_parent=parent,
                expected_output=output,
                now=_NOW,
            )
            assert result.writes_authorized is False
            assert result.journal_created is False
            assert result.host_approval_obtained is False
            assert result.permits_replay is False
            if (
                result.status is ScopedInitialIntentStatus.FIRST_INTENT_SHAPE_ONLY_NO_GRANT
                and test_only_commit
            ):
                # This is deliberately NOT part of any production module.
                session.add(
                    _journal(
                        item=request.item_id,
                        digest=request.plan_digest,
                        output=proposal["output_directory"],
                        downloader=proposal["downloader_id"],
                        save_path=proposal["target_remote_save_path"],
                        operation=request.operation_type,
                        key=request.idempotency_key,
                        status="INTENT_RECORDED",
                    )
                )
                session.flush()
                if simulate_crash_before_commit:
                    return result.status
                session.commit()
            return result.status
        finally:
            session.rollback()


def test_synthetic_durable_intent_blocks_any_second_reservation(tmp_path: Path) -> None:
    client, app, proposal, execution, item, parent, output, source = _scenario(tmp_path)
    try:
        before = source.read_bytes()
        request = _first(execution, item, proposal)
        assert (
            _rehearse_reservation(app, proposal, parent, output, request, test_only_commit=True)
            is ScopedInitialIntentStatus.FIRST_INTENT_SHAPE_ONLY_NO_GRANT
        )
        journal = _rows(app)
        assert len(journal) == 1
        assert journal[0].status == "INTENT_RECORDED"
        assert journal[0].operation_type == _DIR
        assert journal[0].idempotency_key == request.idempotency_key
        # The real worker remains protected, even after synthetic journal.
        assert (
            _rehearse_reservation(app, proposal, parent, output, request, test_only_commit=True)
            is ScopedInitialIntentStatus.BLOCKED_CHAIN
        )
        assert len(_rows(app)) == 1
        assert source.read_bytes() == before
        assert list(output.iterdir()) == []
    finally:
        client.__exit__(None, None, None)


def test_synthetic_crash_before_commit_rolls_back_uncommitted_intent(tmp_path: Path) -> None:
    client, app, proposal, execution, item, parent, output, _ = _scenario(tmp_path)
    try:
        req = _first(execution, item, proposal)
        assert (
            _rehearse_reservation(
                app,
                proposal,
                parent,
                output,
                req,
                test_only_commit=True,
                simulate_crash_before_commit=True,
            )
            is ScopedInitialIntentStatus.FIRST_INTENT_SHAPE_ONLY_NO_GRANT
        )
        assert _rows(app) == []
        assert (
            _rehearse_reservation(app, proposal, parent, output, req)
            is ScopedInitialIntentStatus.FIRST_INTENT_SHAPE_ONLY_NO_GRANT
        )
        assert _rows(app) == []
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize(
    "mutation",
    ["revoked", "old-readonly", "wrong-plan", "wrong-target", "new-key-with-existing-intent"],
)
def test_synthetic_rejected_operation_never_adds_journal(tmp_path: Path, mutation: str) -> None:
    client, app, proposal, execution, item, parent, output, _ = _scenario(tmp_path)
    try:
        req = _first(execution, item, proposal)
        if mutation == "revoked":
            _rewrite(app, {**proposal, "expires_at_utc": "2026-10-14T09:00:00+00:00"})
        elif mutation == "old-readonly":
            with app.state.runtime.session_factory() as session:
                execution_row = session.get(UnpackExecution, execution)
                assert execution_row is not None
                execution_row.config_snapshot = {
                    **execution_row.config_snapshot,
                    "acceptance_mode": "READ_ONLY_SITE_ACCEPTANCE",
                }
                session.commit()
        elif mutation == "wrong-plan":
            req = replace(req, plan_digest="b" * 64)
        elif mutation == "wrong-target":
            req = replace(req, target_path="/tmp/unapproved")
        else:
            assert mutation == "new-key-with-existing-intent"
            # One previous simulated reservation; changing key MUST NOT bypass
            # the zero-journal gate for this execution.
            assert (
                _rehearse_reservation(app, proposal, parent, output, req, test_only_commit=True)
                is ScopedInitialIntentStatus.FIRST_INTENT_SHAPE_ONLY_NO_GRANT
            )
            req = replace(req, idempotency_key="another-key")
        before = len(_rows(app))
        status = _rehearse_reservation(app, proposal, parent, output, req, test_only_commit=True)
        assert status in {
            ScopedInitialIntentStatus.BLOCKED_CHAIN,
            ScopedInitialIntentStatus.BLOCKED_OPERATION,
        }
        assert len(_rows(app)) == before
    finally:
        client.__exit__(None, None, None)


def test_second_sqlite_writer_cannot_take_begin_immediate_before_first_commits(
    tmp_path: Path,
) -> None:
    client, app, proposal, execution, item, parent, output, _ = _scenario(tmp_path)
    try:
        factory = app.state.runtime.session_factory
        inspector = ScopedWriteProposalInspector(app.state.settings.config_dir)
        req = _first(execution, item, proposal)
        with factory() as owner:
            begin_immediate_write(owner)
            assert (
                _inspect_first_intent_inside_locked_session(
                    owner,
                    inspector,
                    req,
                    approved_parent=parent,
                    expected_output=output,
                    now=_NOW,
                ).status
                is ScopedInitialIntentStatus.FIRST_INTENT_SHAPE_ONLY_NO_GRANT
            )
            with factory() as rival:
                rival.connection().exec_driver_sql("PRAGMA busy_timeout=0")
                with pytest.raises(OperationalError):
                    begin_immediate_write(rival)
                rival.rollback()
            owner.add(
                _journal(
                    item=item,
                    digest=proposal["plan_digest"],
                    output=proposal["output_directory"],
                    downloader=proposal["downloader_id"],
                    save_path=proposal["target_remote_save_path"],
                    operation=_DIR,
                    key=req.idempotency_key,
                    status="INTENT_RECORDED",
                )
            )
            owner.commit()
        assert len(_rows(app)) == 1
        assert (
            _rehearse_reservation(
                app,
                proposal,
                parent,
                output,
                req,
            )
            is ScopedInitialIntentStatus.BLOCKED_CHAIN
        )
    finally:
        client.__exit__(None, None, None)
