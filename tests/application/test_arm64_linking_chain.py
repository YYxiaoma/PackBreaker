"""Isolated synthetic bridge from approved execution plan to journal-backed add.

The native ARM64 real-downloader tests reuse the same LINKING helper and run
the subsequent add/verify/seed against dedicated no-network Docker clients.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from backend.app.application.filesystem_operations import CREATE_HARDLINK_OPERATION
from backend.app.domain.task_state import TaskStatus
from backend.app.infrastructure.persistence.models import OperationJournal, UnpackTask
from tests.application.test_task_adding import _AddingFixture
from tests.integration.test_arm64_real_task_chain import _link_approved_synthetic_task

pytest_plugins = ("tests.application.test_task_adding",)


@pytest.mark.asyncio
async def test_approved_linking_creates_real_hardlink_and_add_reuses_journal(
    adding_fixture: _AddingFixture,
) -> None:
    original = adding_fixture.source_file.stat(follow_symlinks=False)
    journal_id = _link_approved_synthetic_task(adding_fixture)

    added = await adding_fixture.coordinator.execute(
        adding_fixture.unit_id, execution_plan_id=adding_fixture.plan_id
    )
    assert added.status is TaskStatus.SEEDING
    repeated = await adding_fixture.coordinator.execute(
        adding_fixture.unit_id, execution_plan_id=adding_fixture.plan_id
    )
    assert repeated.replayed and repeated.qbit_journal_id == added.qbit_journal_id
    with adding_fixture.factory() as session:
        task = session.get(UnpackTask, adding_fixture.task_id)
        assert task is not None and task.status == TaskStatus.SEEDING.value
        journals = list(
            session.scalars(select(OperationJournal).order_by(OperationJournal.created_at))
        )
        assert [item.operation_type for item in journals] == [
            CREATE_HARDLINK_OPERATION,
            "QBITTORRENT_ADD",
        ]
        assert journals[0].id == journal_id
        assert all(item.status == "APPLIED" for item in journals)
    target = adding_fixture.data_root / "target" / adding_fixture.source_file.name
    after = adding_fixture.source_file.stat(follow_symlinks=False)
    assert (original.st_dev, original.st_ino, original.st_size, original.st_mtime_ns) == (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    )
    assert target.stat(follow_symlinks=False).st_ino == original.st_ino
