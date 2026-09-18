from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.task_definition_executions import TaskDefinitionExecutionService
from backend.app.domain.operation import OperationStatus
from backend.app.domain.task_definition import TaskExecutionStatus
from backend.app.domain.task_state import TaskStatus
from backend.app.infrastructure.persistence.base import Base
from backend.app.infrastructure.persistence.models import (
    OperationJournal,
    TaskExecution,
    TaskExecutionItem,
    UnpackTask,
)


def _service(tmp_path: Path) -> tuple[TaskDefinitionExecutionService, sessionmaker[Session]]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory: sessionmaker[Session] = sessionmaker(bind=engine, expire_on_commit=False)
    return (
        TaskDefinitionExecutionService(
            factory,
            cast(Any, None),
            cast(Any, None),
            cast(Any, None),
            data_root=tmp_path,
        ),
        factory,
    )


def _seed_terminal_execution(
    factory: sessionmaker[Session],
    *,
    journals: tuple[tuple[str, OperationStatus], ...],
) -> tuple[str, str]:
    now = datetime.now(UTC)
    execution_id = "execution-stage-e"
    task_id = "unpack-stage-e"
    with factory() as session:
        session.add(
            UnpackTask(
                id=task_id,
                type="MANUAL",
                source_downloader_id="downloader-source",
                source_hash="source-hash-stage-e",
                normalized_unit_key="movie-stage-e",
                idempotency_key="task-stage-e-idempotency",
                parent_task_id=None,
                run_number=1,
                status=TaskStatus.DONE.value,
                trace_id="00000000-0000-0000-0000-000000000018",
                checkpoint={"downloader_kind": "QBITTORRENT"},
                error_code=None,
                version=1,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            TaskExecution(
                id=execution_id,
                task_definition_id=None,
                task_name="Stage E 闭环",
                trigger="MANUAL",
                status="RUNNING",
                phase="VERIFYING",
                source_execution_id=None,
                trace_id="00000000-0000-0000-0000-000000000018",
                config_snapshot={},
                discovered_count=1,
                success_count=0,
                failed_count=0,
                skipped_count=0,
                started_at=now,
                finished_at=None,
                created_at=now,
            )
        )
        session.add(
            TaskExecutionItem(
                id="item-stage-e",
                execution_id=execution_id,
                unpack_task_id=task_id,
                source_object_key="movie-stage-e",
                name="Movie.Stage.E.mkv",
                source="/downloads/Movie.Stage.E.mkv",
                size_bytes=1024,
                phase="VERIFYING",
                progress=90,
                result=None,
                error_code=None,
                error_summary_zh=None,
                technical_detail=None,
                retryable=False,
                retry_count=0,
                created_at=now,
                updated_at=now,
            )
        )
        for index, (operation_type, status) in enumerate(journals):
            session.add(
                OperationJournal(
                    id=f"journal-stage-e-{index}",
                    task_id=task_id,
                    idempotency_key=f"stage-e-operation-{index}",
                    operation_type=operation_type,
                    target={},
                    intent={},
                    status=status.value,
                    before_snapshot=None,
                    after_snapshot={} if status is OperationStatus.APPLIED else None,
                    created_at=now,
                    updated_at=now,
                )
            )
        session.commit()
    return execution_id, task_id


def test_done_execution_requires_finalization_attention_before_success(tmp_path: Path) -> None:
    service, factory = _service(tmp_path)
    execution_id, task_id = _seed_terminal_execution(
        factory,
        journals=(
            ("CREATE_HARDLINK", OperationStatus.APPLIED),
            ("QBITTORRENT_START", OperationStatus.RECONCILE_REQUIRED),
        ),
    )

    execution = service.get_execution(execution_id)
    item = execution.items[0]

    assert execution.status == TaskExecutionStatus.PARTIAL_FAILED.value
    assert item.result == "PARTIAL_FAILED"
    assert item.error_code == "TASK_FINALIZATION_ATTENTION_REQUIRED"
    assert item.retryable is False
    assert item.lifecycle_stage == "FINALIZE"
    assert item.authorization_status == "BLOCKED"
    assert item.closure.status == "ATTENTION_REQUIRED"
    assert item.closure.filesystem_status == "OK"
    assert item.closure.downloader_status == "ATTENTION_REQUIRED"
    assert item.closure.operation_attention_count == 1
    assert item.closure.reconcile_required_count == 1
    assert item.closure.manual_attention_required is False
    assert item.closure.issue_codes == ("OPERATION_RECONCILE_REQUIRED",)

    with factory() as session:
        journal = session.get(OperationJournal, "journal-stage-e-1")
        assert journal is not None
        journal.status = OperationStatus.APPLIED.value
        journal.after_snapshot = {"proved": True}
        session.commit()

    reconciled = service.get_execution(execution_id)
    reconciled_item = reconciled.items[0]
    assert reconciled.status == TaskExecutionStatus.COMPLETED.value
    assert reconciled_item.result == "SUCCESS"
    assert reconciled_item.lifecycle_stage == "COMPLETE"
    assert reconciled_item.closure.status == "COMPLETE"
    assert reconciled_item.closure.downloader_status == "OK"

    with factory() as session:
        task = session.get(UnpackTask, task_id)
        assert task is not None and task.status == TaskStatus.DONE.value


def test_retention_candidates_are_reported_without_becoming_cleanup_failures(
    tmp_path: Path,
) -> None:
    service, factory = _service(tmp_path)
    execution_id, _ = _seed_terminal_execution(
        factory,
        journals=(
            ("CREATE_DIRECTORY", OperationStatus.NOOP),
            ("CREATE_HARDLINK", OperationStatus.ROLLED_BACK),
        ),
    )

    execution = service.get_execution(execution_id)
    item = execution.items[0]

    assert execution.status == TaskExecutionStatus.COMPLETED.value
    assert item.result == "SUCCESS"
    assert item.closure.status == "COMPLETE"
    assert item.closure.filesystem_status == "OK"
    assert item.closure.downloader_status == "NOT_APPLICABLE"
    assert item.closure.retention_candidate_count == 2
    assert item.closure.operation_attention_count == 0

    with factory() as session:
        journals = tuple(session.query(OperationJournal).all())
        assert len(journals) == 2
        assert {journal.status for journal in journals} == {
            OperationStatus.NOOP.value,
            OperationStatus.ROLLED_BACK.value,
        }


def test_rollback_blocked_is_manual_attention_and_never_full_success(tmp_path: Path) -> None:
    service, factory = _service(tmp_path)
    execution_id, _ = _seed_terminal_execution(
        factory,
        journals=(("CREATE_HARDLINK", OperationStatus.ROLLBACK_BLOCKED),),
    )

    execution = service.get_execution(execution_id)
    item = execution.items[0]

    assert execution.status == TaskExecutionStatus.PARTIAL_FAILED.value
    assert item.result == "PARTIAL_FAILED"
    assert item.retryable is False
    assert item.lifecycle_stage == "FINALIZE"
    assert item.closure.status == "ATTENTION_REQUIRED"
    assert item.closure.filesystem_status == "ATTENTION_REQUIRED"
    assert item.closure.rollback_blocked_count == 1
    assert item.closure.manual_attention_required is True
    assert item.closure.issue_codes == ("OPERATION_ROLLBACK_BLOCKED",)
