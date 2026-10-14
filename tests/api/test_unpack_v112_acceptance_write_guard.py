"""Acceptance-marked executions must fail closed before any write worker runs."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import func, select

from backend.app.application.unpack_acceptance import (
    READ_ONLY_ACCEPTANCE_MARKER,
    is_write_blocked_acceptance_execution,
)
from backend.app.infrastructure.persistence.models import (
    UnpackExecution,
    UnpackExecutionItem,
    UnpackExternalOperationJournal,
)
from tests.api.test_unpack_v2 import _authenticated_client, _seed_item_action_fixture


@pytest.mark.parametrize(
    "acceptance_mode",
    [
        READ_ONLY_ACCEPTANCE_MARKER,
        "SCOPED_WRITE_SITE_ACCEPTANCE",
        "UNRECOGNIZED_ACCEPTANCE_MODE",
        "",
        42,
        None,
        False,
        {"future": "marker"},
    ],
)
def test_unapproved_acceptance_modes_are_not_scheduled_or_executed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, acceptance_mode: object
) -> None:
    client, app = _authenticated_client(tmp_path)
    try:
        item_id, _ = _seed_item_action_fixture(app, item_status="PLAN_PENDING")
        with app.state.runtime.session_factory() as session:
            item = session.get(UnpackExecutionItem, item_id)
            assert item is not None
            execution_id = item.execution_id
            execution = session.get(UnpackExecution, execution_id)
            assert execution is not None
            execution.config_snapshot = {"acceptance_mode": acceptance_mode}
            item.execution_plan = {"ready": True, "blocked_reasons": []}
            execution.status = "CONTENT_VERIFYING"
            session.commit()

        factory = app.state.runtime.session_factory
        assert is_write_blocked_acceptance_execution(factory, execution_id)

        materializer = app.state.unpack_materialization_service
        auxiliary = app.state.unpack_auxiliary_service
        seeder = app.state.unpack_seeding_service

        # PLANNED: synthetic ready flag is not a grant. Never schedule the execution.
        assert execution_id not in materializer.list_materializable_execution_ids()
        monkeypatch.setattr(
            materializer,
            "_list_item_ids",
            lambda *_args, **_kwargs: pytest.fail("materializer entered write path"),
        )
        assert materializer.materialize_next_batch(execution_id).processed_count == 0

        # AUXILIARY_FETCHING: no staging, torrent ADD, or file write.
        with factory() as session:
            item = session.get(UnpackExecutionItem, item_id)
            assert item is not None
            item.status = "AUXILIARY_FETCHING"
            session.commit()
        assert execution_id not in auxiliary.list_auxiliary_execution_ids()
        monkeypatch.setattr(
            auxiliary,
            "_list_item_ids",
            lambda *_args, **_kwargs: pytest.fail("auxiliary entered write path"),
        )
        assert asyncio.run(auxiliary.advance_next_batch(execution_id)).processed_count == 0

        # FILES_MATERIALIZED: no final ADD / VERIFY / START and no client probing.
        with factory() as session:
            item = session.get(UnpackExecutionItem, item_id)
            assert item is not None
            item.status = "EXECUTING"
            item.execution_state = {"stage": "FILES_MATERIALIZED"}
            execution = session.get(UnpackExecution, execution_id)
            assert execution is not None
            execution.status = "EXECUTING"
            session.commit()
        assert execution_id not in seeder.list_seedable_execution_ids()
        monkeypatch.setattr(
            seeder,
            "_list_item_ids",
            lambda *_args, **_kwargs: pytest.fail("seeder entered write path"),
        )
        assert asyncio.run(seeder.advance_next_batch(execution_id)).processed_count == 0
        with factory() as session:
            assert session.scalar(select(func.count(UnpackExternalOperationJournal.id))) == 0
    finally:
        client.__exit__(None, None, None)


def test_normal_execution_without_acceptance_mode_remains_schedulable(tmp_path: Path) -> None:
    client, app = _authenticated_client(tmp_path)
    try:
        item_id, _ = _seed_item_action_fixture(app, item_status="PLAN_PENDING")
        with app.state.runtime.session_factory() as session:
            item = session.get(UnpackExecutionItem, item_id)
            assert item is not None
            execution_id = item.execution_id
            execution = session.get(UnpackExecution, execution_id)
            assert execution is not None
            execution.config_snapshot = {"definition_id": "normal-synthetic"}
            item.execution_plan = {"ready": True, "blocked_reasons": []}
            session.commit()
        assert not is_write_blocked_acceptance_execution(
            app.state.runtime.session_factory, execution_id
        )
        assert (
            execution_id
            in app.state.unpack_materialization_service.list_materializable_execution_ids()
        )
    finally:
        client.__exit__(None, None, None)
