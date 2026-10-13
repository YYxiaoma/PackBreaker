import os
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.errors import ApplicationError
from backend.app.application.unpack_existing_reuse import verify_existing_reuse
from backend.app.application.unpack_materialization import UnpackMaterializationService
from backend.app.domain.execution_plan import ExecutionPlanBlockReason
from backend.app.domain.operation import OperationStatus
from backend.app.domain.task_definition import TaskConflictPolicy, TaskStorageMode
from backend.app.domain.unpack import UnpackExecutionStatus, UnpackItemStatus
from backend.app.domain.unpack_execution_plan import (
    UnpackExecutionAction,
    UnpackExecutionActionKind,
    UnpackExecutionPlan,
    unpack_execution_plan_from_payload,
    unpack_execution_plan_to_payload,
)
from backend.app.domain.verification import VerificationLevel
from backend.app.infrastructure.authorized_paths import AuthorizedPathScope
from backend.app.infrastructure.persistence.base import Base
from backend.app.infrastructure.persistence.models import (
    UnpackDefinition,
    UnpackExecution,
    UnpackExecutionItem,
    UnpackExternalOperationJournal,
    utc_now,
)
from backend.app.infrastructure.source_inventory import current_file_snapshot


def _fixture(
    tmp_path: Path,
    storage_mode: TaskStorageMode,
) -> tuple[
    UnpackMaterializationService,
    sessionmaker[Session],
    Path,
    Path,
]:
    data_root = tmp_path / "data"
    source = data_root / "source" / "Movie.mkv"
    output = data_root / "seeding"
    source.parent.mkdir(parents=True)
    output.mkdir()
    source.write_bytes(b"synthetic-content")
    snapshot = current_file_snapshot(source)
    now = utc_now()
    plan = UnpackExecutionPlan(
        item_id="item-1",
        item_version_before=4,
        candidate_id="candidate-1",
        candidate_generation=1,
        metainfo_digest="a" * 64,
        verification_level=VerificationLevel.FULL_VERIFIED,
        output_directory=output.as_posix(),
        storage_mode=storage_mode,
        conflict_policy=TaskConflictPolicy.VERIFY_REUSE_OR_STOP,
        target_device=output.stat(follow_symlinks=False).st_dev,
        target_downloader_id="downloader-target",
        target_downloader_version=3,
        target_downloader_binding_digest="b" * 64,
        target_remote_save_path="/downloads/seeding",
        client_check_required=storage_mode is not TaskStorageMode.HARDLINK,
        actions=(
            UnpackExecutionAction(
                torrent_path="Pack/Movie.mkv",
                kind=UnpackExecutionActionKind.MATERIALIZE,
                length=snapshot.size,
                source_path=source.as_posix(),
                source_snapshot=snapshot,
            ),
        ),
        create_directories=("Pack",),
        blocked_reasons=(),
        created_at=now,
    )

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory: sessionmaker[Session] = sessionmaker(
        bind=engine,
        expire_on_commit=False,
        autoflush=False,
    )
    with factory() as session:
        session.add(
            UnpackDefinition(
                id="definition-1",
                name="materialize",
                trigger_kind="MANUAL",
                status="PENDING_EXECUTION",
                source_kind="DIRECTORY",
                execution_scope_kind="ALL_MATCHING_MEDIA",
                source_config={"directory_path": source.parent.as_posix()},
                file_filter={"extensions": [".mkv"]},
                site_ids=["site-1"],
                output_config={
                    "output_directory": output.as_posix(),
                    "storage_mode": storage_mode.value,
                    "conflict_policy": "VERIFY_REUSE_OR_STOP",
                    "target_downloader_id": "downloader-target",
                },
                retry_enabled=True,
                max_retries=3,
                auto_match_threshold_bps=10000,
                version=1,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            UnpackExecution(
                id="execution-1",
                definition_id="definition-1",
                trigger="MANUAL",
                status=UnpackExecutionStatus.EXECUTING.value,
                config_snapshot={},
                discovery_complete=True,
                total_count=1,
                matched_auto_count=1,
                review_count=0,
                content_verified_count=1,
                content_mismatch_count=0,
                timeout_count=0,
                error_count=0,
                completed_count=0,
                started_at=now,
                version=1,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            UnpackExecutionItem(
                id="item-1",
                execution_id="execution-1",
                source_object_key="source-1",
                source_snapshot={},
                media_identity={},
                status=UnpackItemStatus.PLAN_PENDING.value,
                selected_candidate_id="candidate-1",
                candidate_generation=1,
                retry_count=0,
                content_verification_level=VerificationLevel.FULL_VERIFIED.value,
                torrent_metainfo_digest="a" * 64,
                execution_plan=unpack_execution_plan_to_payload(plan),
                execution_plan_digest=plan.plan_digest,
                execution_plan_created_at=now,
                version=5,
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()

    scope = AuthorizedPathScope.legacy_only(legacy_data_root=data_root)
    service = UnpackMaterializationService(
        factory,
        data_root=data_root,
        path_scope=scope,
    )
    return service, factory, source, output


@pytest.mark.parametrize("misleading_ready_flag", (False, True))
def test_blocked_frozen_plan_is_not_scheduled_or_failed_by_manual_materialize(
    tmp_path: Path, misleading_ready_flag: bool
) -> None:
    service, factory, source, output = _fixture(tmp_path, TaskStorageMode.COPY)
    original_source = source.read_bytes()
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        original_plan = unpack_execution_plan_from_payload(item.execution_plan)
        blocked_plan = replace(
            original_plan,
            blocked_reasons=(ExecutionPlanBlockReason.TARGET_PARENT_UNSAFE,),
            plan_digest="",
        )
        payload = unpack_execution_plan_to_payload(blocked_plan)
        if misleading_ready_flag:
            # Inconsistent cached ready flags never override real blockers.
            payload["ready"] = True
        item.execution_plan = payload
        item.execution_plan_digest = blocked_plan.plan_digest
        item.last_error_code = "UNPACK_EXECUTION_PLAN_BLOCKED"
        item.last_error_message = "TARGET_PARENT_UNSAFE"
        session.commit()

    assert "execution-1" not in service.list_materializable_execution_ids()
    report = service.materialize_next_batch("execution-1")
    assert report.error_count == 0
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == UnpackItemStatus.PLAN_PENDING.value
        assert item.last_error_code == "UNPACK_EXECUTION_PLAN_BLOCKED"
        assert item.execution_plan_digest == blocked_plan.plan_digest
        assert session.scalars(select(UnpackExternalOperationJournal)).all() == []
    assert source.read_bytes() == original_source
    assert not (output / "Pack").exists()


def _freeze_reuse_plan(
    service: UnpackMaterializationService,
    factory: sessionmaker[Session],
    source: Path,
    output: Path,
    *,
    same_inode: bool,
) -> Path:
    target = output / "Pack" / "Movie.mkv"
    target.parent.mkdir(parents=True)
    if same_inode:
        os.link(source, target)
    else:
        target.write_bytes(source.read_bytes())
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        plan = unpack_execution_plan_from_payload(item.execution_plan)
        action = plan.actions[0]
        assert action.source_path is not None and action.source_snapshot is not None
        evidence = verify_existing_reuse(
            service._filesystem,
            source_path=action.source_path,
            source_snapshot=action.source_snapshot,
            target_path=target.as_posix(),
        )
        changed = replace(
            action,
            reuse_target_snapshot=evidence.target_snapshot,
            reuse_sha256=evidence.sha256,
        )
        plan = replace(plan, actions=(changed,), create_directories=(), plan_digest="")
        item.execution_plan = unpack_execution_plan_to_payload(plan)
        item.execution_plan_digest = plan.plan_digest
        session.commit()
    return target


@pytest.mark.parametrize("same_inode", [True, False])
def test_reuse_existing_file_journal_is_read_only_and_idempotent(
    tmp_path: Path, same_inode: bool
) -> None:
    service, factory, source, output = _fixture(tmp_path, TaskStorageMode.HARDLINK)
    target = _freeze_reuse_plan(service, factory, source, output, same_inode=same_inode)
    previous_target_stat = target.stat(follow_symlinks=False)
    previous_source_stat = source.stat(follow_symlinks=False)

    first = service.materialize_next_batch("execution-1")
    assert first.materialized_count == 1
    assert target.stat(follow_symlinks=False) == previous_target_stat
    assert source.stat(follow_symlinks=False) == previous_source_stat
    with factory() as session:
        journals = session.scalars(select(UnpackExternalOperationJournal)).all()
        assert len(journals) == 1
        assert journals[0].operation_type == "UNPACK_EXEC_REUSE_EXISTING"
        assert journals[0].status == OperationStatus.APPLIED.value
        assert journals[0].intent["no_file_mutation"] is True
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        item.execution_state = {
            "stage": "MATERIALIZING",
            "plan_digest": item.execution_plan_digest,
        }
        session.commit()
    second = service.materialize_next_batch("execution-1")
    assert second.materialized_count == 1
    assert target.stat(follow_symlinks=False) == previous_target_stat
    with factory() as session:
        assert len(session.scalars(select(UnpackExternalOperationJournal)).all()) == 1


@pytest.mark.parametrize("replace_with_symlink", [False, True])
def test_reuse_fails_closed_when_frozen_target_changes(
    tmp_path: Path, replace_with_symlink: bool
) -> None:
    service, factory, source, output = _fixture(tmp_path, TaskStorageMode.HARDLINK)
    target = _freeze_reuse_plan(service, factory, source, output, same_inode=False)
    if replace_with_symlink:
        target.unlink()
        target.symlink_to(source)
    else:
        target.write_bytes(b"X" + source.read_bytes()[1:])
    actual_bytes = target.read_bytes()
    source_bytes = source.read_bytes()
    report = service.materialize_next_batch("execution-1")
    assert report.error_count == 1
    assert source.read_bytes() == source_bytes
    assert target.read_bytes() == actual_bytes
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == UnpackItemStatus.EXECUTION_ERROR.value
        assert item.last_error_code == "UNPACK_EXEC_REUSE_TARGET_CHANGED"
        journals = session.scalars(select(UnpackExternalOperationJournal)).all()
        assert len(journals) == 1
        assert journals[0].status == OperationStatus.RECONCILE_REQUIRED.value


@pytest.mark.parametrize("mutation", ("version", "candidate", "generation"))
def test_materialization_preflight_invalidated_item_cannot_create_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    """After a filesystem preflight, a replaced item may not reserve a write."""
    service, factory, source, output = _fixture(tmp_path, TaskStorageMode.HARDLINK)
    inspect = service._filesystem.inspect_directory_creation
    intercepted = False

    def invalidate_during_read(
        *, target_root_relative_path: str, directory_relative_path: str
    ) -> object:
        nonlocal intercepted
        result = inspect(
            target_root_relative_path=target_root_relative_path,
            directory_relative_path=directory_relative_path,
        )
        if not intercepted:
            intercepted = True
            with factory() as session:
                item = session.get(UnpackExecutionItem, "item-1")
                assert item is not None
                assert item.status == UnpackItemStatus.EXECUTING.value
                if mutation == "version":
                    item.version += 1
                elif mutation == "candidate":
                    item.selected_candidate_id = "new-candidate"
                else:
                    item.candidate_generation += 1
                session.commit()
        return result

    monkeypatch.setattr(service._filesystem, "inspect_directory_creation", invalidate_during_read)
    service.materialize_next_batch("execution-1")

    assert intercepted
    assert not (output / "Pack").exists()
    assert source.read_bytes() == b"synthetic-content"
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == UnpackItemStatus.EXECUTING.value
        assert session.scalar(select(UnpackExternalOperationJournal.id)) is None
        if mutation == "version":
            assert item.version == 7  # Reservation advanced 5 -> 6; newer worker made 7.
        elif mutation == "candidate":
            assert item.selected_candidate_id == "new-candidate"
        else:
            assert item.candidate_generation == 2


def test_materialization_worker_does_not_rewind_final_seeding_stage(tmp_path: Path) -> None:
    """Materialization must not treat a final-seeding STARTING item as its own."""
    service, factory, _source, output = _fixture(tmp_path, TaskStorageMode.HARDLINK)
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        item.status = UnpackItemStatus.EXECUTING.value
        item.execution_state = {
            "stage": "STARTING",
            "plan_digest": item.execution_plan_digest,
        }
        item.version = 7
        session.commit()
    report = service.materialize_next_batch("execution-1")
    assert report.processed_count == 0
    assert not (output / "Pack").exists()
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == UnpackItemStatus.EXECUTING.value
        assert item.execution_state is not None
        assert item.execution_state["stage"] == "STARTING"
        assert session.scalar(select(UnpackExternalOperationJournal.id)) is None


def test_materialization_reconciliation_is_sticky_against_late_success(tmp_path: Path) -> None:
    service, factory, _source, _output = _fixture(tmp_path, TaskStorageMode.HARDLINK)
    assert service.materialize_next_batch("execution-1").materialized_count == 1
    with factory() as session:
        row = session.scalar(
            select(UnpackExternalOperationJournal).where(
                UnpackExternalOperationJournal.operation_type == "UNPACK_EXEC_MATERIALIZE_HARDLINK"
            )
        )
        assert row is not None
        journal_id = row.id
    service._mark_reconcile(journal_id, "UNPACK_EXEC_APPLIED_TARGET_CHANGED")
    with pytest.raises(ApplicationError):
        service._mark_applied(journal_id, {"file_type": "regular"})
    with factory() as session:
        row = session.get(UnpackExternalOperationJournal, journal_id)
        assert row is not None
        assert row.status == OperationStatus.RECONCILE_REQUIRED.value
        assert row.last_error_code == "UNPACK_EXEC_APPLIED_TARGET_CHANGED"


def test_materialization_late_write_error_cannot_demote_applied_evidence(tmp_path: Path) -> None:
    service, factory, _source, _output = _fixture(tmp_path, TaskStorageMode.HARDLINK)
    assert service.materialize_next_batch("execution-1").materialized_count == 1
    with factory() as session:
        row = session.scalar(
            select(UnpackExternalOperationJournal).where(
                UnpackExternalOperationJournal.operation_type == "UNPACK_EXEC_MATERIALIZE_HARDLINK"
            )
        )
        assert row is not None
        journal_id = row.id
        proof = row.after_snapshot
    service._mark_reconcile(journal_id, "UNPACK_EXEC_MATERIALIZE_RESULT_UNKNOWN")
    with factory() as session:
        row = session.get(UnpackExternalOperationJournal, journal_id)
        assert row is not None
        assert row.status == OperationStatus.APPLIED.value
        assert row.after_snapshot == proof
        assert row.last_error_code is None


@pytest.mark.parametrize(
    ("mode", "expected_kind"),
    [
        (TaskStorageMode.HARDLINK, "hardlink"),
        (TaskStorageMode.SYMLINK, "symlink"),
        (TaskStorageMode.COPY, "copy"),
    ],
)
def test_materialization_commits_journal_backed_file(
    tmp_path: Path,
    mode: TaskStorageMode,
    expected_kind: str,
) -> None:
    service, factory, source, output = _fixture(tmp_path, mode)

    report = service.materialize_next_batch("execution-1")

    target = output / "Pack" / "Movie.mkv"
    assert report.materialized_count == 1
    assert target.exists()
    if expected_kind == "hardlink":
        assert (
            target.stat(follow_symlinks=False).st_ino == source.stat(follow_symlinks=False).st_ino
        )
    elif expected_kind == "symlink":
        assert target.is_symlink()
        assert target.resolve() == source
    else:
        assert target.read_bytes() == source.read_bytes()
        assert (
            target.stat(follow_symlinks=False).st_ino != source.stat(follow_symlinks=False).st_ino
        )

    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == UnpackItemStatus.EXECUTING.value
        assert item.execution_state is not None
        assert item.execution_state["stage"] == "FILES_MATERIALIZED"
        journals = session.scalars(
            select(UnpackExternalOperationJournal).order_by(
                UnpackExternalOperationJournal.created_at
            )
        ).all()
        assert len(journals) == 2
        assert all(row.status == OperationStatus.APPLIED.value for row in journals)


@pytest.mark.parametrize(
    ("operation_type", "tamper"),
    (
        ("UNPACK_EXEC_CREATE_DIRECTORY", "missing"),
        ("UNPACK_EXEC_CREATE_DIRECTORY", "reconcile"),
        ("UNPACK_EXEC_MATERIALIZE_HARDLINK", "missing"),
        ("UNPACK_EXEC_MATERIALIZE_HARDLINK", "reconcile"),
        ("UNPACK_EXEC_MATERIALIZE_HARDLINK", "target"),
        ("UNPACK_EXEC_MATERIALIZE_HARDLINK", "after"),
        ("UNPACK_EXEC_MATERIALIZE_HARDLINK", "wrong_file_type"),
        ("UNPACK_EXEC_MATERIALIZE_HARDLINK", "wrong_storage_mode"),
    ),
)
def test_materialized_checkpoint_requires_atomic_applied_file_journals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation_type: str,
    tamper: str,
) -> None:
    """A competing worker must not steal a valid FILES_MATERIALIZED checkpoint."""
    service, factory, _source, _output = _fixture(tmp_path, TaskStorageMode.HARDLINK)
    original_complete = service._mark_materialized

    def tamper_just_before_completion(
        item_id: str, plan: UnpackExecutionPlan, *, expected_version: int
    ) -> None:
        with factory() as session:
            row = session.scalar(
                select(UnpackExternalOperationJournal).where(
                    UnpackExternalOperationJournal.operation_type == operation_type
                )
            )
            assert row is not None and row.status == OperationStatus.APPLIED.value
            if tamper == "missing":
                session.delete(row)
            elif tamper == "reconcile":
                row.status = OperationStatus.RECONCILE_REQUIRED.value
            elif tamper == "target":
                row.target = {**row.target, "path": "/unrelated"}
            elif tamper == "wrong_file_type":
                row.after_snapshot = {**(row.after_snapshot or {}), "file_type": "directory"}
            elif tamper == "wrong_storage_mode":
                row.intent = {**row.intent, "storage_mode": "COPY"}
            else:
                row.after_snapshot = None
            session.commit()
        original_complete(item_id, plan, expected_version=expected_version)

    monkeypatch.setattr(service, "_mark_materialized", tamper_just_before_completion)
    report = service.materialize_next_batch("execution-1")
    assert report.materialized_count == 0
    assert report.error_count == 1
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status != UnpackItemStatus.COMPLETED.value
        assert (item.execution_state or {}).get("stage") != "FILES_MATERIALIZED"


def test_materialization_restart_reuses_applied_hardlink_without_second_write(
    tmp_path: Path,
) -> None:
    service, factory, source, output = _fixture(tmp_path, TaskStorageMode.HARDLINK)

    first = service.materialize_next_batch("execution-1")
    assert first.materialized_count == 1
    target = output / "Pack" / "Movie.mkv"
    first_inode = target.stat(follow_symlinks=False).st_ino

    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        item.execution_state = {
            "stage": "MATERIALIZING",
            "plan_digest": item.execution_plan_digest,
        }
        session.commit()

    second = service.materialize_next_batch("execution-1")

    assert second.materialized_count == 1
    assert target.stat(follow_symlinks=False).st_ino == first_inode
    assert target.stat(follow_symlinks=False).st_ino == source.stat(follow_symlinks=False).st_ino
    with factory() as session:
        journals = session.scalars(select(UnpackExternalOperationJournal)).all()
        assert len(journals) == 2
        assert all(row.status == OperationStatus.APPLIED.value for row in journals)


def test_materialization_blocks_source_change_before_any_target_file_write(
    tmp_path: Path,
) -> None:
    service, factory, source, output = _fixture(tmp_path, TaskStorageMode.HARDLINK)
    source.write_bytes(b"changed-after-plan")

    report = service.materialize_next_batch("execution-1")

    assert report.error_count == 1
    assert not (output / "Pack" / "Movie.mkv").exists()
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == UnpackItemStatus.EXECUTION_ERROR.value
        assert item.last_error_code == "SOURCE_CHANGED"
