import pytest
from sqlalchemy import create_engine, func, select
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
    UnpackDeletedItemAudit,
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
        assert execution.status == UnpackExecutionStatus.FAILED.value


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


@pytest.mark.parametrize(
    "error_code",
    (
        "UNPACK_EXEC_MATERIALIZATION_PROOF_MISSING",
        "UNPACK_EXEC_MATERIALIZATION_TARGET_DRIFT",
    ),
)
def test_retry_match_post_start_failure_requires_manual_reconciliation(
    error_code: str,
) -> None:
    """A START may have succeeded; generic Retry must never clear its evidence."""
    service, factory = _fixture(item_status=UnpackItemStatus.EXECUTION_ERROR)
    now = utc_now()
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        item.status = UnpackItemStatus.EXECUTION_ERROR.value
        item.last_error_code = error_code
        item.execution_state = {
            "stage": "STARTING",
            "torrent_hash": "a" * 40,
            "ownership_tag": "packbreaker-seed-item-1",
        }
        item.selected_candidate_id = "candidate-1"
        session.add(
            UnpackExternalOperationJournal(
                id="confirmed-start",
                item_id="item-1",
                idempotency_key="b" * 64,
                operation_type="UNPACK_EXEC_QBITTORRENT_START",
                target={"downloader_id": "downloader-1", "torrent_hash": "a" * 40},
                intent={"reason": "synthetic"},
                status=OperationStatus.APPLIED.value,
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()
        initial_version = item.version

    with pytest.raises(ApplicationError) as error:
        service.retry_match("item-1", expected_item_version=initial_version)
    assert error.value.status == 409
    assert error.value.code == "UNPACK_RETRY_EXTERNAL_RECONCILE_REQUIRED"
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        journal = session.get(UnpackExternalOperationJournal, "confirmed-start")
        assert item is not None and journal is not None
        assert item.status == UnpackItemStatus.EXECUTION_ERROR.value
        assert item.last_error_code == error_code
        assert item.version == initial_version
        assert item.candidate_generation == 2
        assert item.selected_candidate_id == "candidate-1"
        assert (item.execution_state or {}).get("stage") == "STARTING"
        assert journal.status == OperationStatus.APPLIED.value


def test_retry_match_reopens_execution_and_advances_generation() -> None:
    service, factory = _fixture(item_status=UnpackItemStatus.MATCH_TIMEOUT)

    result = service.retry_match("item-1", expected_item_version=3)

    assert result.item_status is UnpackItemStatus.MATCH_PENDING
    assert result.generation == 3
    assert result.retry_count == 0
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


def test_no_match_is_retryable_and_reopens_failed_execution() -> None:
    service, factory = _fixture(item_status=UnpackItemStatus.NO_MATCH)
    result = service.retry_match("item-1", expected_item_version=3)
    assert result.item_status is UnpackItemStatus.MATCH_PENDING
    with factory() as session:
        execution = session.get(UnpackExecution, "execution-1")
        assert execution is not None
        assert execution.status == UnpackExecutionStatus.MATCHING.value


def test_bulk_retry_all_eligible_items_is_atomic_and_version_checked() -> None:
    service, factory = _fixture(item_status=UnpackItemStatus.MATCH_ERROR)
    with factory() as session:
        execution = session.get(UnpackExecution, "execution-1")
        assert execution is not None
        execution.status = UnpackExecutionStatus.FAILED.value
        session.commit()
        version = execution.version
    assert (
        service.retry_eligible_items(
            "execution-1",
            expected_execution_version=version,
            selected_statuses=(UnpackItemStatus.MATCH_ERROR,),
        ).retried_count
        == 1
    )
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        execution = session.get(UnpackExecution, "execution-1")
        assert item is not None and execution is not None
        assert item.status == UnpackItemStatus.MATCH_PENDING.value
        assert execution.status == UnpackExecutionStatus.MATCHING.value
    with pytest.raises(ApplicationError) as caught:
        service.retry_eligible_items(
            "execution-1",
            expected_execution_version=version,
            selected_statuses=(UnpackItemStatus.MATCH_ERROR,),
        )
    assert caught.value.code == "UNPACK_EXECUTION_VERSION_CONFLICT"


def test_bulk_retry_preserves_post_start_reconciliation_item() -> None:
    """Bulk retry never requeues an item with any external journal."""
    service, factory = _fixture(item_status=UnpackItemStatus.EXECUTION_ERROR)
    now = utc_now()
    with factory() as session:
        execution = session.get(UnpackExecution, "execution-1")
        item = session.get(UnpackExecutionItem, "item-1")
        assert execution is not None and item is not None
        item.last_error_code = "UNPACK_EXEC_MATERIALIZATION_TARGET_DRIFT"
        item.execution_state = {"stage": "STARTING", "torrent_hash": "a" * 40}
        execution.total_count = 2
        execution.error_count = 2
        session.add(
            UnpackExecutionItem(
                id="item-safe-retry",
                execution_id="execution-1",
                source_object_key="source-safe",
                source_snapshot={},
                media_identity={"title": "Retry-Eligible"},
                status=UnpackItemStatus.MATCH_ERROR.value,
                candidate_generation=1,
                retry_count=0,
                last_error_code="UNPACK_MATCH_TIMEOUT",
                version=1,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            UnpackExternalOperationJournal(
                id="post-start-write-proof",
                item_id="item-1",
                idempotency_key="c" * 64,
                operation_type="UNPACK_EXEC_QBITTORRENT_START",
                target={"torrent_hash": "a" * 40},
                intent={},
                status=OperationStatus.APPLIED.value,
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()
        expected_version = execution.version
    result = service.retry_eligible_items(
        "execution-1",
        expected_execution_version=expected_version,
        selected_statuses=(
            UnpackItemStatus.MATCH_ERROR,
            UnpackItemStatus.EXECUTION_ERROR,
        ),
    )
    assert result.retried_count == 1
    assert result.skipped_unsafe_count == 1
    with factory() as session:
        dangerous = session.get(UnpackExecutionItem, "item-1")
        eligible = session.get(UnpackExecutionItem, "item-safe-retry")
        assert dangerous is not None and eligible is not None
        assert dangerous.status == UnpackItemStatus.EXECUTION_ERROR.value
        assert dangerous.last_error_code == "UNPACK_EXEC_MATERIALIZATION_TARGET_DRIFT"
        assert dangerous.version == 3
        assert (dangerous.execution_state or {}).get("stage") == "STARTING"
        assert eligible.status == UnpackItemStatus.MATCH_PENDING.value
        assert session.get(UnpackExternalOperationJournal, "post-start-write-proof") is not None


def test_bulk_manual_retry_ignores_exhausted_auto_budget() -> None:
    service, factory = _fixture(
        item_status=UnpackItemStatus.NO_MATCH,
        retry_enabled=False,
        max_retries=0,
        retry_count=9,
    )
    with factory() as session:
        execution = session.get(UnpackExecution, "execution-1")
        assert execution is not None
        execution.status = UnpackExecutionStatus.FAILED.value
        version = execution.version
        session.commit()
    assert (
        service.retry_eligible_items(
            "execution-1",
            expected_execution_version=version,
            selected_statuses=(UnpackItemStatus.NO_MATCH,),
        ).retried_count
        == 1
    )
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == UnpackItemStatus.MATCH_PENDING.value
        assert item.retry_count == 0


@pytest.mark.parametrize(
    ("retry_enabled", "max_retries", "retry_count"),
    [(False, 3, 0), (True, 2, 2), (False, 0, 9)],
)
def test_manual_retry_independent_of_auto_retry_policy(
    retry_enabled: bool,
    max_retries: int,
    retry_count: int,
) -> None:
    service, factory = _fixture(
        item_status=UnpackItemStatus.MATCH_ERROR,
        retry_enabled=retry_enabled,
        max_retries=max_retries,
        retry_count=retry_count,
    )
    result = service.retry_match("item-1", expected_item_version=3)
    assert result.item_status is UnpackItemStatus.MATCH_PENDING
    assert result.retry_count == 0
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None and item.retry_count == 0


def test_no_match_manual_retry_allowed_even_when_auto_budget_is_exhausted() -> None:
    service, factory = _fixture(
        item_status=UnpackItemStatus.NO_MATCH,
        retry_enabled=False,
        max_retries=0,
        retry_count=10,
    )
    result = service.retry_match("item-1", expected_item_version=3)
    assert result.retry_count == 0
    with factory() as session:
        execution = session.get(UnpackExecution, "execution-1")
        assert execution is not None and execution.status == "MATCHING"


def test_item_action_version_conflict_fails_closed() -> None:
    service, _factory = _fixture(item_status=UnpackItemStatus.MATCH_TIMEOUT)

    with pytest.raises(ApplicationError) as failure:
        service.retry_match("item-1", expected_item_version=2)
    assert failure.value.code == "UNPACK_ITEM_VERSION_CONFLICT"


def test_delete_failed_execution_item_without_side_effects_updates_totals() -> None:
    service, factory = _fixture(item_status=UnpackItemStatus.NO_MATCH)
    service.delete_item("item-1", expected_item_version=3)
    with factory() as session:
        assert session.get(UnpackExecutionItem, "item-1") is None
        assert session.scalar(select(func.count(UnpackMatchCandidate.id))) == 0
        execution = session.get(UnpackExecution, "execution-1")
        assert execution is not None
        assert execution.total_count == 0
        assert execution.status == UnpackExecutionStatus.CANCELLED.value


def test_delete_execution_item_with_external_journal_archives_evidence() -> None:
    service, factory = _fixture(item_status=UnpackItemStatus.MATCH_ERROR)
    _record_auxiliary_started(factory, complete=True)
    service.delete_item("item-1", expected_item_version=3)
    with factory() as session:
        assert session.get(UnpackExecutionItem, "item-1") is None
        audit = session.scalar(
            select(UnpackDeletedItemAudit).where(UnpackDeletedItemAudit.item_id == "item-1")
        )
        assert audit is not None
        assert len(audit.payload["operations"]) == 4
        assert audit.payload["operations"][0]["idempotency_key"] == f"{0:064d}"
        assert session.scalar(select(func.count(UnpackExternalOperationJournal.id))) == 0
        assert audit.payload["item_status"] == UnpackItemStatus.MATCH_ERROR.value


@pytest.mark.parametrize(
    "status",
    (
        UnpackItemStatus.DISCOVERED,
        UnpackItemStatus.MATCHED_AUTO,
        UnpackItemStatus.REVIEW_REQUIRED,
        UnpackItemStatus.CONTENT_VERIFIED,
        UnpackItemStatus.EXECUTING,
        UnpackItemStatus.EXECUTION_ERROR,
        UnpackItemStatus.CANCELLED,
    ),
)
def test_delete_non_matching_item_regardless_of_execution_status(
    status: UnpackItemStatus,
) -> None:
    service, factory = _fixture(item_status=status)
    service.delete_item("item-1", expected_item_version=3)
    with factory() as session:
        assert session.get(UnpackExecutionItem, "item-1") is None


@pytest.mark.parametrize("status", (UnpackItemStatus.MATCH_PENDING, UnpackItemStatus.MATCHING))
def test_delete_matching_item_is_rejected(status: UnpackItemStatus) -> None:
    service, factory = _fixture(item_status=status)
    with pytest.raises(ApplicationError) as error:
        service.delete_item("item-1", expected_item_version=3)
    assert error.value.code == "UNPACK_ITEM_DELETE_ACTIVE"
    with factory() as session:
        assert session.get(UnpackExecutionItem, "item-1") is not None


def test_delete_stale_item_still_rejected() -> None:
    service, factory = _fixture(item_status=UnpackItemStatus.REVIEW_REQUIRED)
    with pytest.raises(ApplicationError) as stale:
        service.delete_item("item-1", expected_item_version=2)
    assert stale.value.code == "UNPACK_ITEM_VERSION_CONFLICT"


def test_delete_one_of_two_completed_items_recounts_remaining_execution() -> None:
    service, factory = _fixture(item_status=UnpackItemStatus.NO_MATCH)
    now = utc_now()
    with factory() as session:
        execution = session.get(UnpackExecution, "execution-1")
        assert execution is not None
        execution.total_count = 2
        session.add(
            UnpackExecutionItem(
                id="item-2",
                execution_id="execution-1",
                source_object_key="source-2",
                source_snapshot={},
                media_identity={"title": "Another movie"},
                status=UnpackItemStatus.COMPLETED.value,
                candidate_generation=1,
                retry_count=0,
                version=1,
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()

    service.delete_item("item-1", expected_item_version=3)
    with factory() as session:
        execution = session.get(UnpackExecution, "execution-1")
        assert execution is not None
        assert execution.total_count == 1
        assert execution.completed_count == 1
        assert execution.status == UnpackExecutionStatus.COMPLETED.value
        assert session.get(UnpackExecutionItem, "item-2") is not None


def _record_auxiliary_started(factory: sessionmaker[Session], *, complete: bool) -> None:
    operations = (
        "UNPACK_AUX_STAGING_DIR",
        "UNPACK_AUX_TORRENT_ADD",
        "UNPACK_AUX_FILE_SELECTION",
        "UNPACK_AUX_TORRENT_START",
    )
    now = utc_now()
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        item.selected_candidate_id = "candidate-1"
        item.torrent_metainfo_digest = "c" * 64
        item.last_error_code = "DOWNLOADER_UNAVAILABLE"
        item.auxiliary_state = {
            "state": "DOWNLOADING",
            "missing_paths": ["Release/Movie.nfo"],
            "torrent_hash": "a" * 40,
            "binding_digest": "b" * 64,
            "remote_save_path": "/downloads/.packbreaker-staging/unpack/exec/item",
            "staging_path": "/data/.packbreaker-staging/unpack/exec/item",
            "ownership_tag": "packbreaker-aux-item-1",
            "target_downloader_id": "downloader-1",
            "downloader_version": 3,
        }
        for number, name in enumerate(operations if complete else operations[:1]):
            session.add(
                UnpackExternalOperationJournal(
                    id=f"operation-{number}",
                    item_id="item-1",
                    idempotency_key=f"{number:064d}",
                    operation_type=name,
                    target={"downloader_id": "downloader-1"},
                    intent={"reason": "synthetic"},
                    status=OperationStatus.APPLIED.value,
                    created_at=now,
                    updated_at=now,
                )
            )
        session.commit()


def test_retry_after_auxiliary_rpc_outage_resumes_existing_torrent_without_rematching() -> None:
    service, factory = _fixture(item_status=UnpackItemStatus.MATCH_ERROR)
    _record_auxiliary_started(factory, complete=True)

    result = service.retry_match("item-1", expected_item_version=3)

    assert result.item_status is UnpackItemStatus.AUXILIARY_FETCHING
    assert result.generation == 2
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        execution = session.get(UnpackExecution, "execution-1")
        assert item is not None and execution is not None
        assert item.selected_candidate_id == "candidate-1"
        assert item.candidate_generation == 2
        assert item.torrent_metainfo_digest == "c" * 64
        assert item.auxiliary_state is not None
        assert item.auxiliary_state["state"] == "DOWNLOADING"
        assert item.last_error_code is None
        assert execution.status == UnpackExecutionStatus.CONTENT_VERIFYING.value
        assert session.scalar(select(func.count(UnpackExternalOperationJournal.id))) == 4


def test_stop_state_lag_reconciles_existing_journal_without_rematching() -> None:
    service, factory = _fixture(item_status=UnpackItemStatus.MATCH_ERROR)
    _record_auxiliary_started(factory, complete=True)
    now = utc_now()
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        item.last_error_code = "UNPACK_AUXILIARY_CONFLICT"
        session.add(
            UnpackExternalOperationJournal(
                id="stop-journal",
                item_id="item-1",
                idempotency_key="a" * 64,
                operation_type="UNPACK_AUX_TORRENT_STOP",
                target={"downloader_id": "downloader-1", "torrent_hash": "a" * 40},
                intent={"reason": "synthetic"},
                status=OperationStatus.RECONCILE_REQUIRED.value,
                last_error_code="UNPACK_AUX_STOP_NOT_OBSERVED",
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()
    result = service.retry_match("item-1", expected_item_version=3)
    assert result.item_status is UnpackItemStatus.AUXILIARY_FETCHING
    assert result.generation == 2
    with factory() as session:
        journal = session.get(UnpackExternalOperationJournal, "stop-journal")
        assert journal is not None
        assert journal.status == OperationStatus.RECONCILE_REQUIRED.value
        assert session.scalar(select(func.count(UnpackExternalOperationJournal.id))) == 5


@pytest.mark.parametrize("stop_reason", ["UNPACK_AUX_STOP_RESULT_UNKNOWN", None])
def test_stop_reconcile_rejects_unrelated_or_missing_reason(
    stop_reason: str | None,
) -> None:
    service, factory = _fixture(item_status=UnpackItemStatus.MATCH_ERROR)
    _record_auxiliary_started(factory, complete=True)
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        item.last_error_code = "UNPACK_AUXILIARY_CONFLICT"
        session.add(
            UnpackExternalOperationJournal(
                id="stop-journal",
                item_id="item-1",
                idempotency_key="a" * 64,
                operation_type="UNPACK_AUX_TORRENT_STOP",
                target={"downloader_id": "downloader-1"},
                intent={"reason": "synthetic"},
                status=OperationStatus.RECONCILE_REQUIRED.value,
                last_error_code=stop_reason,
                created_at=utc_now(),
                updated_at=utc_now(),
            )
        )
        session.commit()
    with pytest.raises(ApplicationError) as exc:
        service.retry_match("item-1", expected_item_version=3)
    assert exc.value.code == "UNPACK_RETRY_EXTERNAL_RECONCILE_REQUIRED"


@pytest.mark.parametrize("error_code", ["SITE_UNAVAILABLE", "SITE_RATE_LIMITED", "SITE_TIMEOUT"])
def test_retry_after_transient_site_outage_preserves_auxiliary_journal(
    error_code: str,
) -> None:
    service, factory = _fixture(item_status=UnpackItemStatus.MATCH_ERROR)
    _record_auxiliary_started(factory, complete=True)
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        item.last_error_code = error_code
        session.commit()

    result = service.retry_match("item-1", expected_item_version=3)
    assert result.item_status == UnpackItemStatus.AUXILIARY_FETCHING
    assert result.generation == 2
    with factory() as session:
        assert session.scalar(select(func.count(UnpackExternalOperationJournal.id))) == 4


def test_retry_with_duplicate_auxiliary_journal_requires_manual_reconciliation() -> None:
    service, factory = _fixture(item_status=UnpackItemStatus.MATCH_ERROR)
    _record_auxiliary_started(factory, complete=True)
    now = utc_now()
    with factory() as session:
        session.add(
            UnpackExternalOperationJournal(
                id="operation-duplicate",
                item_id="item-1",
                idempotency_key="duplicate-operation".ljust(64, "x"),
                operation_type="UNPACK_AUX_TORRENT_START",
                target={"downloader_id": "downloader-1"},
                intent={"reason": "duplicate"},
                status=OperationStatus.APPLIED.value,
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()
    with pytest.raises(ApplicationError) as error:
        service.retry_match("item-1", expected_item_version=3)
    assert error.value.code == "UNPACK_RETRY_EXTERNAL_RECONCILE_REQUIRED"


def test_bulk_retry_skips_auxiliary_download_and_preserves_journal() -> None:
    service, factory = _fixture(item_status=UnpackItemStatus.MATCH_ERROR)
    _record_auxiliary_started(factory, complete=True)
    result = service.retry_eligible_items(
        "execution-1",
        expected_execution_version=1,
        selected_statuses=(UnpackItemStatus.MATCH_ERROR,),
    )
    assert result.retried_count == 0
    assert result.skipped_unsafe_count == 1
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == UnpackItemStatus.MATCH_ERROR.value
        assert item.candidate_generation == 2
        assert session.scalar(select(func.count(UnpackExternalOperationJournal.id))) == 4


def test_retry_with_incomplete_auxiliary_journal_requires_reconciliation() -> None:
    service, factory = _fixture(item_status=UnpackItemStatus.MATCH_ERROR)
    _record_auxiliary_started(factory, complete=False)

    with pytest.raises(ApplicationError) as error:
        service.retry_match("item-1", expected_item_version=3)
    assert error.value.code == "UNPACK_RETRY_EXTERNAL_RECONCILE_REQUIRED"
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == UnpackItemStatus.MATCH_ERROR.value
        assert item.candidate_generation == 2
        assert item.auxiliary_state is not None
        assert item.auxiliary_state["state"] == "DOWNLOADING"


def test_retry_failed_execution_without_external_operations_requeues_matching() -> None:
    service, factory = _fixture(
        item_status=UnpackItemStatus.EXECUTION_ERROR,
        selected_candidate_id="candidate-1",
    )
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        item.last_error_code = "SITE_INVALID_RESPONSE"
        session.commit()

    result = service.retry_match("item-1", expected_item_version=3)
    assert result.item_status is UnpackItemStatus.MATCH_PENDING
    assert result.generation == 3
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.selected_candidate_id is None


@pytest.mark.parametrize(
    ("status", "stage", "error_code", "allowed"),
    [
        (OperationStatus.APPLIED, "FILES_MATERIALIZED", "SITE_INVALID_RESPONSE", True),
        (
            OperationStatus.APPLIED,
            "FILES_MATERIALIZED",
            "UNPACK_SEED_SKIP_CHECK_NOT_COMPLETE",
            True,
        ),
        (OperationStatus.INTENT_RECORDED, "FILES_MATERIALIZED", "SITE_INVALID_RESPONSE", False),
        (OperationStatus.RECONCILE_REQUIRED, "FILES_MATERIALIZED", "SITE_INVALID_RESPONSE", False),
        (OperationStatus.APPLIED, "STARTING", "SITE_INVALID_RESPONSE", False),
        (
            OperationStatus.APPLIED,
            "FILES_MATERIALIZED",
            "UNPACK_EXEC_MATERIALIZATION_TARGET_DRIFT",
            False,
        ),
    ],
)
def test_failed_execution_resume_requires_safe_frozen_stage_and_applied_journal(
    status: OperationStatus, stage: str, error_code: str, allowed: bool
) -> None:
    service, factory = _fixture(
        item_status=UnpackItemStatus.EXECUTION_ERROR,
        selected_candidate_id="candidate-1",
    )
    now = utc_now()
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        item.last_error_code = error_code
        item.execution_plan = {"synthetic": "placeholder"}
        item.execution_plan_digest = "digest-1"
        item.torrent_metainfo_digest = "torrent-digest-1"
        item.execution_state = {"stage": stage, "plan_digest": "digest-1"}
        session.add(
            UnpackExternalOperationJournal(
                id="applied-materialization",
                item_id="item-1",
                idempotency_key="z" * 64,
                operation_type="UNPACK_EXEC_MATERIALIZE_HARDLINK",
                target={"path": "/data/seeding/movie.mkv"},
                intent={"reason": "synthetic"},
                status=status.value,
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()

    if allowed:
        result = service.retry_match("item-1", expected_item_version=3)
        assert result.item_status is UnpackItemStatus.EXECUTING
        assert result.generation == 2
        with factory() as session:
            item = session.get(UnpackExecutionItem, "item-1")
            journal = session.get(UnpackExternalOperationJournal, "applied-materialization")
            assert item is not None and journal is not None
            assert item.execution_plan_digest == "digest-1"
            assert item.selected_candidate_id == "candidate-1"
            assert item.execution_state == {"stage": stage, "plan_digest": "digest-1"}
            assert journal.status == status.value
    else:
        with pytest.raises(ApplicationError) as failure:
            service.retry_match("item-1", expected_item_version=3)
        assert failure.value.code == "UNPACK_RETRY_EXTERNAL_RECONCILE_REQUIRED"
        with factory() as session:
            item = session.get(UnpackExecutionItem, "item-1")
            assert item is not None
            assert item.status == UnpackItemStatus.EXECUTION_ERROR.value


def test_failed_execution_resume_never_accepts_mixed_client_add_journals() -> None:
    service, factory = _fixture(
        item_status=UnpackItemStatus.EXECUTION_ERROR,
        selected_candidate_id="candidate-1",
    )
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        item.last_error_code = "SITE_INVALID_RESPONSE"
        item.execution_plan = {"synthetic": "placeholder"}
        item.execution_plan_digest = "digest-1"
        item.torrent_metainfo_digest = "torrent-digest-1"
        item.execution_state = {"stage": "FILES_MATERIALIZED", "plan_digest": "digest-1"}
        for n, operation_type in enumerate(
            (
                "UNPACK_EXEC_MATERIALIZE_HARDLINK",
                "UNPACK_EXEC_QBITTORRENT_ADD",
                "UNPACK_EXEC_TRANSMISSION_ADD",
            )
        ):
            session.add(
                UnpackExternalOperationJournal(
                    id=f"journal-{n}",
                    item_id="item-1",
                    idempotency_key=(f"proof-{n}").ljust(64, "x"),
                    operation_type=operation_type,
                    target={"path": "/data/seeding"},
                    intent={},
                    status=OperationStatus.APPLIED.value,
                    created_at=utc_now(),
                    updated_at=utc_now(),
                )
            )
        session.commit()
    with pytest.raises(ApplicationError) as failure:
        service.retry_match("item-1", expected_item_version=3)
    assert failure.value.code == "UNPACK_RETRY_EXTERNAL_RECONCILE_REQUIRED"


@pytest.mark.parametrize(
    "status",
    (
        UnpackItemStatus.NO_MATCH,
        UnpackItemStatus.MATCHED_AUTO,
        UnpackItemStatus.MATCHED_MANUAL,
        UnpackItemStatus.REVIEW_REQUIRED,
        UnpackItemStatus.MATCH_TIMEOUT,
        UnpackItemStatus.MATCH_ERROR,
        UnpackItemStatus.CONTENT_MISMATCH,
        UnpackItemStatus.EXECUTION_ERROR,
    ),
)
def test_bulk_retry_in_matching_execution_requeues_eligible_item(
    status: UnpackItemStatus,
) -> None:
    service, factory = _fixture(item_status=status, selected_candidate_id="candidate-1")
    with factory() as session:
        execution = session.get(UnpackExecution, "execution-1")
        assert execution is not None
        execution.status = UnpackExecutionStatus.MATCHING.value
        session.commit()
        version = execution.version
    result = service.retry_eligible_items(
        "execution-1", expected_execution_version=version, selected_statuses=(status,)
    )
    assert result.retried_count == 1
    assert result.skipped_unsafe_count == 0
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        execution = session.get(UnpackExecution, "execution-1")
        assert item is not None and execution is not None
        assert item.status == UnpackItemStatus.MATCH_PENDING.value
        assert item.selected_candidate_id is None
        assert item.candidate_generation == 3
        assert execution.status == UnpackExecutionStatus.MATCHING.value


@pytest.mark.parametrize(
    "status",
    (
        UnpackItemStatus.MATCH_PENDING,
        UnpackItemStatus.MATCHING,
        UnpackItemStatus.COMPLETED,
        UnpackItemStatus.CANCELLED,
    ),
)
def test_bulk_retry_does_not_modify_success_active_or_cancelled(
    status: UnpackItemStatus,
) -> None:
    service, factory = _fixture(item_status=status)
    result = service.retry_eligible_items(
        "execution-1",
        expected_execution_version=1,
        selected_statuses=(UnpackItemStatus.NO_MATCH,),
    )
    assert result.retried_count == 0
    assert result.skipped_unsafe_count == 0
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == status.value
        assert item.version == 3


def test_bulk_retry_skips_unsafe_operations_without_touching_original_evidence() -> None:
    service, factory = _fixture(item_status=UnpackItemStatus.REVIEW_REQUIRED)
    now = utc_now()
    with factory() as session:
        session.add(
            UnpackExternalOperationJournal(
                id="unsafe-pending",
                item_id="item-1",
                idempotency_key="z" * 64,
                operation_type="UNPACK_EXEC_QBITTORRENT_ADD",
                target={"downloader_id": "downloader-1"},
                intent={},
                status=OperationStatus.INTENT_RECORDED.value,
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()
    result = service.retry_eligible_items(
        "execution-1",
        expected_execution_version=1,
        selected_statuses=(UnpackItemStatus.REVIEW_REQUIRED,),
    )
    assert result.retried_count == 0
    assert result.skipped_unsafe_count == 1
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        operation = session.get(UnpackExternalOperationJournal, "unsafe-pending")
        assert item is not None and operation is not None
        assert item.status == UnpackItemStatus.REVIEW_REQUIRED.value
        assert operation.status == OperationStatus.INTENT_RECORDED.value


def test_bulk_retry_selects_only_requested_child_status_even_when_others_are_eligible() -> None:
    service, factory = _fixture(item_status=UnpackItemStatus.REVIEW_REQUIRED)
    now = utc_now()
    with factory() as session:
        session.add(
            UnpackExecutionItem(
                id="different-status",
                execution_id="execution-1",
                source_object_key="source-different",
                source_snapshot={},
                media_identity={"title": "Not selected"},
                status=UnpackItemStatus.NO_MATCH.value,
                candidate_generation=2,
                retry_count=0,
                version=1,
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()
    result = service.retry_eligible_items(
        "execution-1",
        expected_execution_version=1,
        selected_statuses=(UnpackItemStatus.REVIEW_REQUIRED,),
    )
    assert result.retried_count == 1
    with factory() as session:
        chosen = session.get(UnpackExecutionItem, "item-1")
        not_chosen = session.get(UnpackExecutionItem, "different-status")
        assert chosen is not None and not_chosen is not None
        assert chosen.status == UnpackItemStatus.MATCH_PENDING.value
        assert not_chosen.status == UnpackItemStatus.NO_MATCH.value
        assert not_chosen.candidate_generation == 2


@pytest.mark.parametrize(
    "selected",
    (
        (),
        (UnpackItemStatus.NO_MATCH, UnpackItemStatus.NO_MATCH),
        (UnpackItemStatus.COMPLETED,),
        (UnpackItemStatus.MATCHING,),
    ),
)
def test_bulk_retry_rejects_empty_duplicate_or_unsafe_status_selection(
    selected: tuple[UnpackItemStatus, ...],
) -> None:
    service, factory = _fixture(item_status=UnpackItemStatus.NO_MATCH)
    with pytest.raises(ApplicationError) as caught:
        service.retry_eligible_items(
            "execution-1", expected_execution_version=1, selected_statuses=selected
        )
    assert caught.value.status == 422
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None and item.status == UnpackItemStatus.NO_MATCH.value
        assert item.candidate_generation == 2
