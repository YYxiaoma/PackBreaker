from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.unpack_materialization import UnpackMaterializationService
from backend.app.domain.operation import OperationStatus
from backend.app.domain.task_definition import TaskConflictPolicy, TaskStorageMode
from backend.app.domain.unpack import UnpackExecutionStatus, UnpackItemStatus
from backend.app.domain.unpack_execution_plan import (
    UnpackExecutionAction,
    UnpackExecutionActionKind,
    UnpackExecutionPlan,
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
