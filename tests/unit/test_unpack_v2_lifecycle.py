from __future__ import annotations

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.unpack_lifecycle import refresh_unpack_execution_state
from backend.app.domain.unpack import UnpackExecutionStatus, UnpackItemStatus
from backend.app.infrastructure.persistence.base import Base
from backend.app.infrastructure.persistence.database import configure_sqlite_engine
from backend.app.infrastructure.persistence.models import (
    UnpackDefinition,
    UnpackExecution,
    UnpackExecutionItem,
    utc_now,
)


def _factory() -> tuple[sessionmaker[Session], UnpackExecution]:
    engine = configure_sqlite_engine(create_engine("sqlite+pysqlite:///:memory:"))
    Base.metadata.create_all(engine)
    factory: sessionmaker[Session] = sessionmaker(
        bind=engine, expire_on_commit=False, autoflush=False
    )
    now = utc_now()
    with factory() as session:
        session.add(
            UnpackDefinition(
                id="definition-1",
                name="lifecycle",
                trigger_kind="MANUAL",
                status="PENDING_EXECUTION",
                source_kind="DIRECTORY",
                execution_scope_kind="ALL_MATCHING_MEDIA",
                source_config={"directory_path": "/data/movies"},
                file_filter={"extensions": [".mkv"]},
                site_ids=["site-1"],
                output_config={"output_directory": "/data/seeding"},
                retry_enabled=True,
                max_retries=3,
                auto_match_threshold_bps=10000,
                version=1,
                created_at=now,
                updated_at=now,
            )
        )
        execution = UnpackExecution(
            id="execution-1",
            definition_id="definition-1",
            trigger="MANUAL",
            status=UnpackExecutionStatus.CONTENT_VERIFYING.value,
            config_snapshot={},
            discovery_complete=True,
            total_count=0,
            matched_auto_count=0,
            review_count=0,
            content_verified_count=0,
            content_mismatch_count=0,
            timeout_count=0,
            error_count=0,
            completed_count=0,
            started_at=now,
            version=1,
            created_at=now,
            updated_at=now,
        )
        session.add(execution)
        session.commit()
        session.refresh(execution)
        return factory, execution


def _add_item(
    factory: sessionmaker[Session],
    item_id: str,
    status: UnpackItemStatus,
    *,
    selected_candidate_id: str | None = None,
) -> None:
    now = utc_now()
    with factory() as session:
        session.add(
            UnpackExecutionItem(
                id=item_id,
                execution_id="execution-1",
                source_object_key=f"source-{item_id}",
                source_snapshot={},
                media_identity={"title": item_id},
                status=status.value,
                selected_candidate_id=selected_candidate_id,
                candidate_generation=1,
                retry_count=0,
                version=1,
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()


def _status(factory: sessionmaker[Session]) -> UnpackExecutionStatus:
    with factory() as session:
        execution = session.get(UnpackExecution, "execution-1")
        assert execution is not None
        execution.total_count = int(
            session.scalar(
                select(func.count(UnpackExecutionItem.id)).where(
                    UnpackExecutionItem.execution_id == execution.id
                )
            )
            or 0
        )
        refresh_unpack_execution_state(session, execution)
        session.commit()
        return UnpackExecutionStatus(execution.status)


def test_pending_review_does_not_block_other_items_progress() -> None:
    factory, _execution = _factory()
    _add_item(factory, "item-done", UnpackItemStatus.COMPLETED)
    _add_item(factory, "item-review", UnpackItemStatus.REVIEW_REQUIRED)
    _add_item(factory, "item-verified", UnpackItemStatus.CONTENT_VERIFIED)

    # 仍有可推进的影片时，执行状态必须停留在活跃阶段，否则驱动不会调度。
    assert _status(factory) is UnpackExecutionStatus.CONTENT_VERIFYING


def test_pending_review_does_not_block_while_items_are_executing() -> None:
    factory, _execution = _factory()
    _add_item(factory, "item-review", UnpackItemStatus.REVIEW_REQUIRED)
    _add_item(factory, "item-exec", UnpackItemStatus.EXECUTING)

    assert _status(factory) is UnpackExecutionStatus.EXECUTING


def test_review_only_execution_is_not_reported_as_completed() -> None:
    factory, _execution = _factory()
    _add_item(factory, "item-done", UnpackItemStatus.COMPLETED)
    _add_item(factory, "item-review", UnpackItemStatus.REVIEW_REQUIRED)

    # 只剩待审核时必须落到 REVIEW_REQUIRED，而不是被“全部完成”分支抢走。
    assert _status(factory) is UnpackExecutionStatus.REVIEW_REQUIRED


def test_execution_completes_when_no_review_is_pending() -> None:
    factory, _execution = _factory()
    _add_item(factory, "item-a", UnpackItemStatus.COMPLETED)
    _add_item(factory, "item-b", UnpackItemStatus.COMPLETED)

    assert _status(factory) is UnpackExecutionStatus.COMPLETED
