"""Never let an acceptance execution reserve an external intent after a race.

All operations here use synthetic SQLite and no filesystem/downloader adapter calls.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy import func, select

from backend.app.application import unpack_auxiliary
from backend.app.application.errors import ApplicationError
from backend.app.application.unpack_acceptance import (
    assert_unmarked_execution_for_write,
    is_write_blocked_acceptance_execution,
)
from backend.app.infrastructure.persistence.models import (
    UnpackExecution,
    UnpackExecutionItem,
    UnpackExternalOperationJournal,
)
from tests.api.test_unpack_v2 import _authenticated_client, _seed_item_action_fixture


@pytest.mark.parametrize(
    "mode",
    ["READ_ONLY_SITE_ACCEPTANCE", "SCOPED_WRITE_SITE_ACCEPTANCE", None, "unknown"],
)
@pytest.mark.parametrize("worker", ["materialization", "auxiliary", "seeding"])
def test_revoked_after_initial_worker_check_cannot_record_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: object, worker: str
) -> None:
    client, app = _authenticated_client(tmp_path)
    try:
        item_id, candidate_id = _seed_item_action_fixture(app, item_status="PLAN_PENDING")
        factory = app.state.runtime.session_factory
        with factory() as session:
            item = session.get(UnpackExecutionItem, item_id)
            assert item is not None
            execution_id = item.execution_id
            assert not is_write_blocked_acceptance_execution(factory, execution_id)
            # This mutation is the synthetic race: the queue saw a normal job,
            # but the transaction that would reserve an external intent must
            # see the newly frozen acceptance marker.
            execution = session.get(UnpackExecution, execution_id)
            assert execution is not None
            execution.config_snapshot = {
                **execution.config_snapshot,
                "acceptance_mode": mode,
            }
            item.status = "AUXILIARY_FETCHING" if worker == "auxiliary" else "EXECUTING"
            item.execution_state = {"stage": "MATERIALIZING"}
            version = item.version
            generation = item.candidate_generation
            session.commit()

        with factory() as session:
            with pytest.raises(ApplicationError) as exc:
                assert_unmarked_execution_for_write(session, execution_id)
            assert exc.value.code == "UNPACK_ACCEPTANCE_WRITE_NOT_AUTHORIZED"

        if worker == "materialization":
            plan = cast(
                Any,
                SimpleNamespace(
                    candidate_id=candidate_id,
                    candidate_generation=generation,
                    plan_digest="a" * 64,
                ),
            )
            with pytest.raises(ApplicationError) as exc:
                app.state.unpack_materialization_service._record_intent(
                    item_id,
                    "UNPACK_EXEC_MATERIALIZE_COPY",
                    plan=plan,
                    expected_version=version,
                    key="synthetic-copy-guard",
                    target={"path": "/synthetic/target"},
                    intent={"synthetic": True},
                )
        elif worker == "auxiliary":
            monkeypatch.setattr(
                unpack_auxiliary, "_journal_key", lambda *args: "synthetic-aux-guard"
            )
            monkeypatch.setattr(unpack_auxiliary, "_current_records", lambda *args: None)
            prepared = cast(Any, SimpleNamespace(context=SimpleNamespace()))
            with pytest.raises(ApplicationError) as exc:
                app.state.unpack_auxiliary_service._record_intent(
                    item_id,
                    "UNPACK_AUX_TORRENT_ADD",
                    prepared,
                    target={"path": "/synthetic/target"},
                    intent={"synthetic": True},
                )
        else:
            prepared = cast(Any, SimpleNamespace(item_id=item_id))
            with pytest.raises(ApplicationError) as exc:
                app.state.unpack_seeding_service._record_intent(
                    prepared,
                    "UNPACK_EXEC_TRANSMISSION_ADD",
                    key="synthetic-tr-guard",
                    target={"path": "/synthetic/target"},
                    intent={"synthetic": True},
                    before_snapshot=None,
                )
        assert exc.value.code == "UNPACK_ACCEPTANCE_WRITE_NOT_AUTHORIZED"
        with factory() as session:
            assert session.scalar(select(func.count(UnpackExternalOperationJournal.id))) == 0
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize("worker", ["materialization", "auxiliary", "seeding"])
def test_direct_private_prepare_does_not_consume_acceptance_execution(
    tmp_path: Path, worker: str
) -> None:
    client, app = _authenticated_client(tmp_path)
    try:
        item_id, _ = _seed_item_action_fixture(app, item_status="PLAN_PENDING")
        with app.state.runtime.session_factory() as session:
            item = session.get(UnpackExecutionItem, item_id)
            assert item is not None
            execution = session.get(UnpackExecution, item.execution_id)
            assert execution is not None
            execution.config_snapshot = {
                **execution.config_snapshot,
                "acceptance_mode": "READ_ONLY_SITE_ACCEPTANCE",
            }
            session.commit()
        with pytest.raises(ApplicationError) as exc:
            if worker == "materialization":
                app.state.unpack_materialization_service._reserve_and_load_plan(item_id)
            elif worker == "auxiliary":
                app.state.unpack_auxiliary_service._load_context(item_id)
            else:
                asyncio.run(app.state.unpack_seeding_service._prepare(item_id))
        assert exc.value.code == "UNPACK_ACCEPTANCE_WRITE_NOT_AUTHORIZED"
    finally:
        client.__exit__(None, None, None)
