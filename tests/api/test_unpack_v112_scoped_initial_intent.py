"""First-intent transaction gate exercised only against synthetic SQLite/media."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select

from backend.app.application import unpack_scoped_initial_intent as intent_gate
from backend.app.application.unpack_scoped_initial_intent import (
    ScopedInitialIntentRequest,
    ScopedInitialIntentStatus,
    inspect_initial_scoped_intent_transaction,
)
from backend.app.application.unpack_scoped_readonly_chain import (
    ScopedChainReport,
    inspect_pristine_scoped_carpt_chain,
)
from backend.app.application.unpack_scoped_write_proposal import ScopedWriteProposalInspector
from backend.app.infrastructure.persistence.database import begin_immediate_write
from backend.app.infrastructure.persistence.models import (
    UnpackExecution,
    UnpackExternalOperationJournal,
)
from tests.api.test_unpack_v2 import _seed_item_action_fixture
from tests.api.test_unpack_v112_scoped_journal_audit import _journal
from tests.api.test_unpack_v112_scoped_readonly_chain import _scenario
from tests.api.test_unpack_v112_scoped_write_proposal import _NOW, _rewrite

_DIR = "UNPACK_EXEC_CREATE_DIRECTORY"
_COPY = "UNPACK_EXEC_MATERIALIZE_COPY"


def _first(ex: str, item: str, proposal: dict[str, Any]) -> ScopedInitialIntentRequest:
    return ScopedInitialIntentRequest(
        execution_id=ex,
        item_id=item,
        plan_digest=proposal["plan_digest"],
        operation_type=_DIR,
        target_path=proposal["output_directory"] + "/movie",
        source_path=None,
        relative_path="movie",
        idempotency_key="isolated-first-intent",
    )


def _inspect(
    app: Any, parent: Path, output: Path, request: ScopedInitialIntentRequest
) -> ScopedInitialIntentStatus:
    inspector = ScopedWriteProposalInspector(app.state.settings.config_dir)
    factory = app.state.runtime.session_factory
    with factory() as session:
        before = session.scalar(select(func.count(UnpackExternalOperationJournal.id)))
    result = inspect_initial_scoped_intent_transaction(
        factory,
        inspector,
        request,
        approved_parent=parent,
        expected_output=output,
        now=_NOW,
    )
    with factory() as session:
        after = session.scalar(select(func.count(UnpackExternalOperationJournal.id)))
    assert before == after
    assert result.writes_authorized is False
    assert result.journal_created is False
    assert result.permits_replay is False
    assert result.host_approval_obtained is False
    assert result.downloader_contacted is False
    assert result.media_content_read is False
    return result.status


def test_first_operation_is_structurally_valid_but_never_authorized(tmp_path: Path) -> None:
    client, app, proposal, ex, item, parent, output, _ = _scenario(tmp_path)
    try:
        assert _inspect(app, parent, output, _first(ex, item, proposal)) is (
            ScopedInitialIntentStatus.FIRST_INTENT_SHAPE_ONLY_NO_GRANT
        )
        assert list(output.iterdir()) == []
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize(
    "change",
    [
        {"operation_type": _COPY},
        {"operation_type": "UNPACK_EXEC_TRANSMISSION_ADD"},
        {"operation_type": "UNPACK_EXEC_MATERIALIZE_HARDLINK"},
        {"relative_path": "../movie"},
        {"relative_path": "another-dir"},
        {"target_path": "/tmp/escape"},
        {"source_path": "/tmp/foreign.mkv"},
    ],
)
def test_only_exact_first_directory_may_be_observed(tmp_path: Path, change: dict[str, Any]) -> None:
    client, app, proposal, ex, item, parent, output, _ = _scenario(tmp_path)
    try:
        base = _first(ex, item, proposal)
        from dataclasses import replace

        status = _inspect(app, parent, output, replace(base, **change))
        assert status in {
            ScopedInitialIntentStatus.BLOCKED_INPUT,
            ScopedInitialIntentStatus.BLOCKED_OPERATION,
        }
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize(
    "change",
    [
        {"plan_digest": "b" * 64},
        {"idempotency_key": ""},
        {"idempotency_key": "a" * 65},
        {"item_id": "not-this-item"},
        {"execution_id": "not-this-execution"},
    ],
)
def test_identity_and_untrusted_keys_fail_closed(tmp_path: Path, change: dict[str, Any]) -> None:
    client, app, proposal, ex, item, parent, output, _ = _scenario(tmp_path)
    try:
        from dataclasses import replace

        status = _inspect(app, parent, output, replace(_first(ex, item, proposal), **change))
        assert status in {
            ScopedInitialIntentStatus.BLOCKED_INPUT,
            ScopedInitialIntentStatus.BLOCKED_CHAIN,
            ScopedInitialIntentStatus.BLOCKED_OPERATION,
        }
    finally:
        client.__exit__(None, None, None)


def test_scope_revoked_and_old_readonly_mode_denied(tmp_path: Path) -> None:
    client, app, proposal, ex, item, parent, output, _ = _scenario(tmp_path)
    try:
        _rewrite(app, {**proposal, "expires_at_utc": "2026-10-14T09:00:00+00:00"})
        assert _inspect(app, parent, output, _first(ex, item, proposal)) == (
            ScopedInitialIntentStatus.BLOCKED_CHAIN
        )
        _rewrite(app, proposal)
        with app.state.runtime.session_factory() as session:
            execution = session.get(UnpackExecution, ex)
            assert execution is not None
            execution.config_snapshot = {
                **execution.config_snapshot,
                "acceptance_mode": "READ_ONLY_SITE_ACCEPTANCE",
            }
            session.commit()
        assert _inspect(app, parent, output, _first(ex, item, proposal)) == (
            ScopedInitialIntentStatus.BLOCKED_CHAIN
        )
    finally:
        client.__exit__(None, None, None)


def test_existing_unknown_intent_is_never_replayed(tmp_path: Path) -> None:
    client, app, proposal, ex, item, parent, output, _ = _scenario(tmp_path)
    try:
        with app.state.runtime.session_factory() as session:
            session.add(
                _journal(
                    item=item,
                    digest=proposal["plan_digest"],
                    output=proposal["output_directory"],
                    downloader=proposal["downloader_id"],
                    save_path=proposal["target_remote_save_path"],
                    operation=_DIR,
                    status="INTENT_RECORDED",
                )
            )
            session.commit()
        assert _inspect(app, parent, output, _first(ex, item, proposal)) is (
            ScopedInitialIntentStatus.BLOCKED_CHAIN
        )
    finally:
        client.__exit__(None, None, None)


def test_global_idempotency_key_collision_different_item_is_denied(tmp_path: Path) -> None:
    client, app, proposal, ex, item, parent, output, _ = _scenario(tmp_path)
    try:
        other_item, _ = _seed_item_action_fixture(app, item_status="PLAN_PENDING")
        with app.state.runtime.session_factory() as session:
            session.add(
                _journal(
                    item=other_item,
                    digest=proposal["plan_digest"],
                    output=proposal["output_directory"],
                    downloader=proposal["downloader_id"],
                    save_path=proposal["target_remote_save_path"],
                    operation=_DIR,
                    key="isolated-first-intent",
                )
            )
            session.commit()
        assert _inspect(app, parent, output, _first(ex, item, proposal)) is (
            ScopedInitialIntentStatus.BLOCKED_KEY_CONFLICT
        )
    finally:
        client.__exit__(None, None, None)


def test_write_transaction_precedes_readonly_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, app, proposal, ex, item, parent, output, _ = _scenario(tmp_path)
    try:
        seen = False
        original_begin = begin_immediate_write
        original_chain = inspect_pristine_scoped_carpt_chain

        def wrapped_begin(session: Any) -> None:
            nonlocal seen
            original_begin(session)
            seen = True

        def wrapped_chain(*args: Any, **kwargs: Any) -> ScopedChainReport:
            assert seen
            return original_chain(*args, **kwargs)

        monkeypatch.setattr(intent_gate, "begin_immediate_write", wrapped_begin)
        monkeypatch.setattr(intent_gate, "inspect_pristine_scoped_carpt_chain", wrapped_chain)
        assert _inspect(app, parent, output, _first(ex, item, proposal)) is (
            ScopedInitialIntentStatus.FIRST_INTENT_SHAPE_ONLY_NO_GRANT
        )
        assert seen
    finally:
        client.__exit__(None, None, None)
