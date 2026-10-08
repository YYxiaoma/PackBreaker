import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.errors import ApplicationError
from backend.app.application.unpack_item_actions import UnpackItemActionService
from backend.app.domain.operation import OperationStatus
from backend.app.domain.unpack import (
    UnpackExecutionStatus,
    UnpackItemStatus,
    UnpackReviewDecision,
)
from backend.app.infrastructure.persistence.base import Base
from backend.app.infrastructure.persistence.models import (
    UnpackDefinition,
    UnpackExecution,
    UnpackExecutionItem,
    UnpackExternalOperationJournal,
    UnpackMatchCandidate,
    UnpackReviewDecisionModel,
    utc_now,
)


def _fixture(
    *,
    item_status: UnpackItemStatus,
    retry_enabled: bool = True,
    max_retries: int = 3,
    retry_count: int = 0,
    generation: int = 2,
    selected_candidate_id: str | None = None,
) -> tuple[UnpackItemActionService, sessionmaker[Session]]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory: sessionmaker[Session] = sessionmaker(
        bind=engine,
        expire_on_commit=False,
        autoflush=False,
    )
    now = utc_now()
    with factory() as session:
        session.add(
            UnpackDefinition(
                id="definition-1",
                name="动作测试",
                trigger_kind="MANUAL",
                status="PENDING_EXECUTION",
                source_kind="DIRECTORY",
                execution_scope_kind="ALL_MATCHING_MEDIA",
                source_config={"directory_path": "/data/movies"},
                file_filter={"extensions": [".mkv"]},
                site_ids=["site-1"],
                output_config={
                    "output_directory": "/data/seeding",
                    "storage_mode": "HARDLINK",
                    "conflict_policy": "VERIFY_REUSE_OR_STOP",
                    "target_downloader_id": "downloader-1",
                },
                retry_enabled=retry_enabled,
                max_retries=max_retries,
                auto_match_threshold_bps=9800,
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
                status=(
                    UnpackExecutionStatus.REVIEW_REQUIRED.value
                    if item_status
                    in {UnpackItemStatus.REVIEW_REQUIRED, UnpackItemStatus.MATCHED_AUTO}
                    else UnpackExecutionStatus.COMPLETED_WITH_ERRORS.value
                ),
                config_snapshot={},
                discovery_complete=True,
                total_count=1,
                matched_auto_count=1 if item_status is UnpackItemStatus.MATCHED_AUTO else 0,
                review_count=1 if item_status is UnpackItemStatus.REVIEW_REQUIRED else 0,
                content_verified_count=0,
                content_mismatch_count=0,
                timeout_count=1 if item_status is UnpackItemStatus.MATCH_TIMEOUT else 0,
                error_count=1 if item_status is UnpackItemStatus.MATCH_ERROR else 0,
                completed_count=0,
                started_at=now,
                finished_at=(
                    now
                    if item_status in {UnpackItemStatus.MATCH_TIMEOUT, UnpackItemStatus.MATCH_ERROR}
                    else None
                ),
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
                media_identity={"title": "Movie"},
                status=item_status.value,
                selected_candidate_id=selected_candidate_id,
                candidate_generation=generation,
                retry_count=retry_count,
                last_error_code=(
                    "UNPACK_MATCH_TIMEOUT"
                    if item_status is UnpackItemStatus.MATCH_TIMEOUT
                    else "UNPACK_MATCH_ERROR"
                    if item_status is UnpackItemStatus.MATCH_ERROR
                    else None
                ),
                last_error_message=(
                    "匹配失败"
                    if item_status in {UnpackItemStatus.MATCH_TIMEOUT, UnpackItemStatus.MATCH_ERROR}
                    else None
                ),
                match_finished_at=now,
                version=3,
                created_at=now,
                updated_at=now,
            )
        )
        session.add_all(
            [
                UnpackMatchCandidate(
                    id="candidate-1",
                    item_id="item-1",
                    generation=generation,
                    site_id="site-1",
                    candidate_key="torrent-1",
                    title="Movie",
                    size_bytes=100,
                    score_bps=9800,
                    is_exact_match=False,
                    evidence={"hard_conflicts": []},
                    verification_status="NOT_CHECKED",
                    raw_ref={"torrent_id": "torrent-1"},
                    created_at=now,
                ),
                UnpackMatchCandidate(
                    id="candidate-2",
                    item_id="item-1",
                    generation=generation,
                    site_id="site-1",
                    candidate_key="torrent-2",
                    title="Other",
                    size_bytes=100,
                    score_bps=9900,
                    is_exact_match=False,
                    evidence={"hard_conflicts": ["TITLE_CONFLICT"]},
                    verification_status="NOT_CHECKED",
                    raw_ref={"torrent_id": "torrent-2"},
                    created_at=now,
                ),
            ]
        )
        session.commit()
    return UnpackItemActionService(factory), factory


def test_review_approve_moves_item_to_manual_match_and_is_idempotent() -> None:
    service, factory = _fixture(item_status=UnpackItemStatus.REVIEW_REQUIRED)

    result = service.review(
        "item-1",
        decision=UnpackReviewDecision.APPROVE,
        candidate_id="candidate-1",
        generation=2,
        expected_item_version=3,
        idempotency_key="review-1",
    )

    assert result.item_status is UnpackItemStatus.MATCHED_MANUAL
    assert result.item_version == 4
    assert result.replayed is False

    replay = service.review(
        "item-1",
        decision=UnpackReviewDecision.APPROVE,
        candidate_id="candidate-1",
        generation=2,
        expected_item_version=3,
        idempotency_key="review-1",
    )
    assert replay.decision_id == result.decision_id
    assert replay.replayed is True

    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        execution = session.get(UnpackExecution, "execution-1")
        assert item is not None and execution is not None
        assert item.selected_candidate_id == "candidate-1"
        assert item.match_origin == "MANUAL"
        assert item.status == UnpackItemStatus.MATCHED_MANUAL.value
        assert execution.status == UnpackExecutionStatus.CONTENT_VERIFYING.value
        assert session.scalar(select(UnpackReviewDecisionModel)) is not None


def test_review_auto_match_can_be_rejected_as_no_match() -> None:
    service, factory = _fixture(
        item_status=UnpackItemStatus.MATCHED_AUTO,
        selected_candidate_id="candidate-1",
    )

    result = service.review(
        "item-1",
        decision=UnpackReviewDecision.NO_MATCH,
        candidate_id=None,
        generation=2,
        expected_item_version=3,
        idempotency_key="review-no-match",
    )

    assert result.item_status is UnpackItemStatus.NO_MATCH
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        execution = session.get(UnpackExecution, "execution-1")
        assert item is not None and execution is not None
        assert item.selected_candidate_id is None
        assert item.match_origin is None
        assert execution.status == UnpackExecutionStatus.COMPLETED_WITH_ERRORS.value


@pytest.mark.parametrize(
    "status",
    [
        UnpackItemStatus.TORRENT_FETCHING,
        UnpackItemStatus.CONTENT_VERIFYING,
        UnpackItemStatus.CONTENT_VERIFIED,
        UnpackItemStatus.PLAN_PENDING,
    ],
)
def test_review_is_allowed_before_external_side_effects(status: UnpackItemStatus) -> None:
    service, factory = _fixture(
        item_status=status,
        selected_candidate_id="candidate-1",
    )

    result = service.review(
        "item-1",
        decision=UnpackReviewDecision.APPROVE,
        candidate_id="candidate-1",
        generation=2,
        expected_item_version=3,
        idempotency_key=f"review-{status.value}",
    )

    assert result.item_status is UnpackItemStatus.MATCHED_MANUAL
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        execution = session.get(UnpackExecution, "execution-1")
        assert item is not None and execution is not None
        assert item.status == UnpackItemStatus.MATCHED_MANUAL.value
        assert item.content_verification_level is None
        assert item.torrent_metainfo_digest is None
        assert item.execution_plan is None
        assert item.execution_state is None
        assert execution.status == UnpackExecutionStatus.CONTENT_VERIFYING.value


def test_review_is_blocked_after_any_external_operation_journal_exists() -> None:
    service, factory = _fixture(
        item_status=UnpackItemStatus.CONTENT_VERIFIED,
        selected_candidate_id="candidate-1",
    )
    now = utc_now()
    with factory() as session:
        session.add(
            UnpackExternalOperationJournal(
                id="operation-1",
                item_id="item-1",
                idempotency_key="x" * 64,
                operation_type="UNPACK_AUX_TORRENT_ADD",
                target={"downloader_id": "downloader-1"},
                intent={"reason": "synthetic"},
                status=OperationStatus.APPLIED.value,
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()

    with pytest.raises(ApplicationError) as failure:
        service.review(
            "item-1",
            decision=UnpackReviewDecision.APPROVE,
            candidate_id="candidate-1",
            generation=2,
            expected_item_version=3,
            idempotency_key="review-after-side-effect",
        )
    assert failure.value.code == "UNPACK_REVIEW_EXTERNAL_EFFECT_STARTED"


def test_review_rejects_hard_conflict_and_idempotency_conflict() -> None:
    service, _factory = _fixture(item_status=UnpackItemStatus.REVIEW_REQUIRED)

    with pytest.raises(ApplicationError) as blocked:
        service.review(
            "item-1",
            decision=UnpackReviewDecision.APPROVE,
            candidate_id="candidate-2",
            generation=2,
            expected_item_version=3,
            idempotency_key="blocked-review",
        )
    assert blocked.value.code == "UNPACK_REVIEW_CANDIDATE_BLOCKED"

    service.review(
        "item-1",
        decision=UnpackReviewDecision.APPROVE,
        candidate_id="candidate-1",
        generation=2,
        expected_item_version=3,
        idempotency_key="review-conflict",
    )
    with pytest.raises(ApplicationError) as conflict:
        service.review(
            "item-1",
            decision=UnpackReviewDecision.NO_MATCH,
            candidate_id=None,
            generation=2,
            expected_item_version=3,
            idempotency_key="review-conflict",
        )
    assert conflict.value.code == "UNPACK_REVIEW_IDEMPOTENCY_CONFLICT"


def test_retry_match_reopens_execution_and_advances_generation() -> None:
    service, factory = _fixture(item_status=UnpackItemStatus.MATCH_TIMEOUT)

    result = service.retry_match("item-1", expected_item_version=3)

    assert result.item_status is UnpackItemStatus.MATCH_PENDING
    assert result.generation == 3
    assert result.retry_count == 1
    assert result.item_version == 4

    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        execution = session.get(UnpackExecution, "execution-1")
        assert item is not None and execution is not None
        assert item.selected_candidate_id is None
        assert item.match_origin is None
        assert item.last_error_code is None
        assert item.match_started_at is None
        assert item.match_finished_at is None
        assert execution.status == UnpackExecutionStatus.MATCHING.value
        assert execution.finished_at is None
        old_candidates = session.scalars(
            select(UnpackMatchCandidate).where(UnpackMatchCandidate.generation == 2)
        ).all()
        assert len(old_candidates) == 2


@pytest.mark.parametrize(
    ("retry_enabled", "max_retries", "retry_count", "expected_code"),
    [
        (False, 3, 0, "UNPACK_MATCH_RETRY_DISABLED"),
        (True, 2, 2, "UNPACK_MATCH_RETRY_EXHAUSTED"),
    ],
)
def test_retry_match_respects_task_retry_policy(
    retry_enabled: bool,
    max_retries: int,
    retry_count: int,
    expected_code: str,
) -> None:
    service, _factory = _fixture(
        item_status=UnpackItemStatus.MATCH_ERROR,
        retry_enabled=retry_enabled,
        max_retries=max_retries,
        retry_count=retry_count,
    )

    with pytest.raises(ApplicationError) as failure:
        service.retry_match("item-1", expected_item_version=3)
    assert failure.value.code == expected_code


def test_item_action_version_conflict_fails_closed() -> None:
    service, _factory = _fixture(item_status=UnpackItemStatus.MATCH_TIMEOUT)

    with pytest.raises(ApplicationError) as failure:
        service.retry_match("item-1", expected_item_version=2)
    assert failure.value.code == "UNPACK_ITEM_VERSION_CONFLICT"
