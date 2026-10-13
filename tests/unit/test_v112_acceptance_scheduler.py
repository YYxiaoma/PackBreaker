"""Read-only acceptance jobs must never enter background external-write queues."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.app.application.unpack_acceptance import READ_ONLY_ACCEPTANCE_MARKER
from backend.app.infrastructure.persistence.models import UnpackExecution, UnpackExecutionItem
from tests.api.test_unpack_v2 import _authenticated_client, _seed_item_action_fixture


@pytest.mark.parametrize(
    ("item_status", "execution_status", "stage", "service_name", "list_method"),
    (
        (
            "PLAN_PENDING",
            "EXECUTING",
            None,
            "unpack_materialization_service",
            "list_materializable_execution_ids",
        ),
        (
            "AUXILIARY_FETCHING",
            "CONTENT_VERIFYING",
            None,
            "unpack_auxiliary_service",
            "list_auxiliary_execution_ids",
        ),
        (
            "CLIENT_VERIFYING",
            "EXECUTING",
            "CLIENT_VERIFYING",
            "unpack_seeding_service",
            "list_seedable_execution_ids",
        ),
    ),
)
def test_scoped_acceptance_is_excluded_from_all_external_write_schedulers(
    tmp_path: Path,
    item_status: str,
    execution_status: str,
    stage: str | None,
    service_name: str,
    list_method: str,
) -> None:
    client, app = _authenticated_client(tmp_path)
    try:
        read_only_item, _ = _seed_item_action_fixture(app, item_status=item_status)
        ordinary_item, _ = _seed_item_action_fixture(app, item_status=item_status)
        with app.state.runtime.session_factory() as session:
            scoped = session.get(UnpackExecutionItem, read_only_item)
            ordinary = session.get(UnpackExecutionItem, ordinary_item)
            assert scoped is not None and ordinary is not None
            scoped_execution = session.get(UnpackExecution, scoped.execution_id)
            ordinary_execution = session.get(UnpackExecution, ordinary.execution_id)
            assert scoped_execution is not None and ordinary_execution is not None
            scoped_execution.status = ordinary_execution.status = execution_status
            scoped_execution.config_snapshot = {"acceptance_mode": READ_ONLY_ACCEPTANCE_MARKER}
            # An unmarked job with identical item state must remain schedulable.
            ordinary_execution.config_snapshot = {}
            if item_status == "PLAN_PENDING":
                # An ordinary ready plan is required for file scheduling.
                ordinary.execution_plan = {"ready": True, "blocked_reasons": []}
            if stage:
                scoped.execution_state = {"stage": stage}
                ordinary.execution_state = {"stage": stage}
            scoped_id = scoped_execution.id
            ordinary_id = ordinary_execution.id
            session.commit()
        method = getattr(getattr(app.state, service_name), list_method)
        selected = method()
        assert scoped_id not in selected
        assert ordinary_id in selected
    finally:
        client.__exit__(None, None, None)
