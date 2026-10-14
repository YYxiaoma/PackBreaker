"""A scheduler/worker race cannot mutate an acceptance item into an error."""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path

import pytest
from sqlalchemy import func, select

from backend.app.infrastructure.persistence.models import (
    UnpackExecution,
    UnpackExecutionItem,
    UnpackExternalOperationJournal,
)
from tests.api.test_unpack_v2 import _authenticated_client, _seed_item_action_fixture


@pytest.mark.parametrize(
    ("worker", "module_path"),
    [
        ("materialization", "backend.app.application.unpack_materialization"),
        ("auxiliary", "backend.app.application.unpack_auxiliary"),
        ("seeding", "backend.app.application.unpack_seeding"),
    ],
)
def test_stale_scheduler_never_rewrites_frozen_acceptance_item(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    worker: str,
    module_path: str,
) -> None:
    client, app = _authenticated_client(tmp_path)
    try:
        item_id, candidate_id = _seed_item_action_fixture(app, item_status="PLAN_PENDING")
        factory = app.state.runtime.session_factory
        with factory() as session:
            item = session.get(UnpackExecutionItem, item_id)
            assert item is not None
            execution = session.get(UnpackExecution, item.execution_id)
            assert execution is not None
            execution_id = execution.id
            execution.config_snapshot = {
                **execution.config_snapshot,
                "acceptance_mode": "SCOPED_WRITE_SITE_ACCEPTANCE",
            }
            if worker == "auxiliary":
                item.status = "AUXILIARY_FETCHING"
            elif worker == "seeding":
                item.status = "EXECUTING"
                item.execution_state = {"stage": "FILES_MATERIALIZED"}
            else:
                item.execution_plan = {"ready": True, "blocked_reasons": []}
            session.commit()
            frozen = (item.status, item.version, item.execution_state, item.last_error_code)
            version = item.version
            generation = item.candidate_generation

        # Reproduce an earlier successful queue check that raced with a marker
        # change. The current worker's transaction must still deny writes.
        module = importlib.import_module(module_path)
        monkeypatch.setattr(module, "is_write_blocked_acceptance_execution", lambda *_args: False)
        service = getattr(app.state, f"unpack_{worker}_service")
        if worker == "materialization":
            monkeypatch.setattr(
                service,
                "_list_item_ids",
                lambda *_args, **_kwargs: ((item_id, version, candidate_id, generation),),
            )
            service.materialize_next_batch(execution_id)
        elif worker == "auxiliary":
            monkeypatch.setattr(service, "_list_item_ids", lambda *_args, **_kwargs: (item_id,))
            asyncio.run(service.advance_next_batch(execution_id))
        else:
            monkeypatch.setattr(
                service,
                "_list_item_ids",
                lambda *_args, **_kwargs: ((item_id, version, candidate_id),),
            )
            asyncio.run(service.advance_next_batch(execution_id))

        with factory() as session:
            item = session.get(UnpackExecutionItem, item_id)
            assert item is not None
            assert (item.status, item.version, item.execution_state, item.last_error_code) == frozen
            assert session.scalar(select(func.count(UnpackExternalOperationJournal.id))) == 0
    finally:
        client.__exit__(None, None, None)
