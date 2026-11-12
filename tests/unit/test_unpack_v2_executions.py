from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast

from backend.app.application.unpack_executions import UnpackExecutionQueryService
from backend.app.domain.unpack import UnpackExecutionStatus, UnpackItemStatus
from backend.app.infrastructure.persistence.models import UnpackExecution


def test_match_progress_excludes_pending_and_matching_items() -> None:
    now = datetime.now(UTC)
    execution = cast(
        UnpackExecution,
        SimpleNamespace(
            id="execution-1",
            definition_id="definition-1",
            trigger="MANUAL",
            status=UnpackExecutionStatus.MATCHING.value,
            discovery_complete=True,
            discovery_cursor=None,
            total_count=74,
            started_at=now,
            finished_at=None,
            version=1,
            created_at=now,
            updated_at=now,
        ),
    )

    view = UnpackExecutionQueryService._execution_view(
        execution,
        item_counts={
            UnpackItemStatus.MATCH_PENDING: 3,
            UnpackItemStatus.MATCHING: 1,
            UnpackItemStatus.MATCHED_AUTO: 9,
            UnpackItemStatus.NO_MATCH: 1,
            UnpackItemStatus.MATCH_ERROR: 60,
        },
    )

    assert view.match_pending_count == 3
    assert view.matching_count == 1
    assert view.match_progress_count == 70
