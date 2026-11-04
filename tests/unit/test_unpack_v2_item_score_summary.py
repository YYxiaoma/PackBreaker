from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.unpack_executions import UnpackExecutionQueryService
from backend.app.infrastructure.persistence.base import Base
from backend.app.infrastructure.persistence.models import (
    UnpackDefinition,
    UnpackExecution,
    UnpackExecutionItem,
    UnpackMatchCandidate,
    utc_now,
)


def test_execution_item_page_uses_only_current_generation_best_score() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory: sessionmaker[Session] = sessionmaker(
        bind=engine, expire_on_commit=False, autoflush=False
    )
    now = utc_now()
    with factory() as session:
        session.add(
            UnpackDefinition(
                id="definition-1",
                name="候选最高分测试",
                trigger_kind="MANUAL",
                status="PENDING_EXECUTION",
                source_kind="DIRECTORY",
                execution_scope_kind="ALL_MATCHING_MEDIA",
                source_config={"directory_path": "/data"},
                file_filter={"extensions": [".mkv"]},
                site_ids=["site-1"],
                output_config={},
                retry_enabled=False,
                max_retries=3,
                auto_match_threshold_bps=9900,
                auto_match_enabled=True,
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
                status="REVIEW_REQUIRED",
                config_snapshot={"site_ids": ["site-1"]},
                discovery_complete=True,
                total_count=2,
                matched_auto_count=0,
                review_count=2,
                version=1,
                created_at=now,
                updated_at=now,
            )
        )
        for item_id, generation in (("item-1", 2), ("item-2", 3)):
            session.add(
                UnpackExecutionItem(
                    id=item_id,
                    execution_id="execution-1",
                    source_object_key=item_id,
                    source_snapshot={"path": "/data/" + item_id + ".mkv"},
                    media_identity={"raw_name": item_id},
                    status="REVIEW_REQUIRED",
                    candidate_generation=generation,
                    retry_count=0,
                    version=1,
                    created_at=now,
                    updated_at=now,
                )
            )
        session.flush()
        for key, generation, score in (
            ("historic", 1, 10000),
            ("current-a", 2, 7100),
            ("current-b", 2, 8725),
        ):
            session.add(
                UnpackMatchCandidate(
                    id=key,
                    item_id="item-1",
                    generation=generation,
                    site_id="site-1",
                    candidate_key=key,
                    title=key,
                    score_bps=score,
                    is_exact_match=False,
                    evidence={},
                    raw_ref={},
                    created_at=now,
                )
            )
        session.commit()

    query = UnpackExecutionQueryService(factory)
    result = query.list_items("execution-1", limit=20)
    items = {item.id: item for item in result.items}
    assert len(items) == 2
    assert items["item-1"].highest_candidate_score_bps == 8725
    assert items["item-2"].highest_candidate_score_bps is None

    # Cursor pagination must retain current-generation filtering.
    first = query.list_items("execution-1", limit=1)
    second = query.list_items("execution-1", cursor=first.next_cursor, limit=1)
    assert first.items[0].highest_candidate_score_bps == 8725
    assert second.items[0].highest_candidate_score_bps is None
    engine.dispose()
