from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.errors import ApplicationError
from backend.app.application.unpack_acceptance import assert_unmarked_execution_for_write
from backend.app.application.unpack_deletion_audit import archive_deleted_item_operations
from backend.app.application.unpack_lifecycle import refresh_unpack_execution_state
from backend.app.domain.operation import OperationStatus
from backend.app.domain.unpack import (
    BULK_REMATCHABLE_ITEM_STATUSES,
    REVIEWABLE_ITEM_STATUSES,
    UnpackCandidateVerificationStatus,
    UnpackExecutionStatus,
    UnpackItemStatus,
    UnpackReviewDecision,
    item_transition_allowed,
)
from backend.app.domain.unpack_execution_plan import unpack_execution_plan_from_payload
from backend.app.infrastructure.persistence.database import begin_immediate_write
from backend.app.infrastructure.persistence.models import (
    UnpackDefinition,
    UnpackExecution,
    UnpackExecutionItem,
    UnpackExternalOperationJournal,
    UnpackMatchCandidate,
    UnpackReviewDecisionModel,
    new_uuid,
    utc_now,
)

RetryCapability = Literal["MATCH", "VERIFICATION", "PLAN", "RESUME", "RECONCILE", "NONE"]


@dataclass(frozen=True, slots=True)
class UnpackReviewResult:
    decision_id: str
    item_id: str
    decision: UnpackReviewDecision
    candidate_id: str | None
    generation: int
    item_version: int
    item_status: UnpackItemStatus
    decided_at: datetime
    replayed: bool


@dataclass(frozen=True, slots=True)
class UnpackBulkRetryResult:
    execution_id: str
    retried_count: int
    skipped_unsafe_count: int


@dataclass(frozen=True, slots=True)
class UnpackRetryResult:
    item_id: str
    generation: int
    retry_count: int
    item_version: int
    item_status: UnpackItemStatus


class UnpackItemActionService:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def review(
        self,
        item_id: str,
        *,
        decision: UnpackReviewDecision,
        candidate_id: str | None,
        generation: int,
        expected_item_version: int,
        idempotency_key: str,
    ) -> UnpackReviewResult:
        key = idempotency_key.strip()
        if not key or len(key) > 128:
            raise self._invalid("Idempotency-Key 长度必须位于 1 到 128 个字符")
        if generation < 0:
            raise self._invalid("candidate generation 不能小于 0")
        if expected_item_version < 1:
            raise self._invalid("If-Match item version 必须大于等于 1")
        if decision is UnpackReviewDecision.APPROVE and not candidate_id:
            raise self._invalid("批准候选时必须提供 candidate_id")
        if decision is UnpackReviewDecision.NO_MATCH and candidate_id is not None:
            raise self._invalid("无匹配决策不能绑定 candidate_id")

        with self._session_factory() as session:
            begin_immediate_write(session)
            existing = session.scalar(
                select(UnpackReviewDecisionModel).where(
                    UnpackReviewDecisionModel.idempotency_key == key
                )
            )
            if existing is not None:
                return self._replay_review(
                    session,
                    existing,
                    item_id=item_id,
                    decision=decision,
                    candidate_id=candidate_id,
                    generation=generation,
                    expected_item_version=expected_item_version,
                )

            item = session.get(UnpackExecutionItem, item_id)
            if item is None:
                raise self._item_not_found()
            if item.version != expected_item_version:
                raise self._version_conflict(item.version)
            if item.candidate_generation != generation:
                raise self._conflict("候选代次已经变化，请刷新审核弹窗后重试")

            current = UnpackItemStatus(item.status)
            if current not in REVIEWABLE_ITEM_STATUSES:
                raise self._conflict("影片项当前状态不允许提交人工审核")
            has_external_operation = session.scalar(
                select(UnpackExternalOperationJournal.id)
                .where(UnpackExternalOperationJournal.item_id == item.id)
                .limit(1)
            )
            if has_external_operation is not None:
                raise ApplicationError(
                    code="UNPACK_REVIEW_EXTERNAL_EFFECT_STARTED",
                    status=409,
                    title="影片项已进入外部副作用阶段",
                    detail="该影片已经产生下载器或文件系统外部操作记录，禁止直接更换匹配候选",
                )
            target = (
                UnpackItemStatus.MATCHED_MANUAL
                if decision is UnpackReviewDecision.APPROVE
                else UnpackItemStatus.NO_MATCH
            )
            if not item_transition_allowed(current, target):
                raise self._conflict("当前影片状态不允许执行该审核决策")

            candidate: UnpackMatchCandidate | None = None
            if decision is UnpackReviewDecision.APPROVE:
                candidate = session.get(UnpackMatchCandidate, candidate_id)
                if (
                    candidate is None
                    or candidate.item_id != item.id
                    or candidate.generation != generation
                ):
                    raise self._conflict("候选不属于当前影片或已过期")
                hard_conflicts = candidate.evidence.get("hard_conflicts")
                if isinstance(hard_conflicts, list) and hard_conflicts:
                    raise ApplicationError(
                        code="UNPACK_REVIEW_CANDIDATE_BLOCKED",
                        status=409,
                        title="候选存在硬冲突",
                        detail="存在硬冲突的候选不能由人工审核直接批准",
                    )

            execution = session.get(UnpackExecution, item.execution_id)
            if execution is None:
                raise self._execution_not_found()

            before_version = item.version
            now = utc_now()
            row = UnpackReviewDecisionModel(
                id=new_uuid(),
                item_id=item.id,
                generation=generation,
                decision=decision.value,
                candidate_id=candidate.id if candidate is not None else None,
                item_version_before=before_version,
                idempotency_key=key,
                decided_at=now,
            )
            session.add(row)
            item.selected_candidate_id = candidate.id if candidate is not None else None
            item.match_origin = "MANUAL" if candidate is not None else None
            item.status = target.value
            item.last_error_code = None
            item.last_error_message = None
            item.content_verification_level = None
            item.torrent_metainfo_digest = None
            item.auxiliary_state = None
            item.execution_plan = None
            item.execution_plan_digest = None
            item.execution_plan_created_at = None
            item.execution_state = None
            item.updated_at = now
            item.version += 1
            session.flush()
            _refresh_execution_state(session, execution)
            session.commit()
            return UnpackReviewResult(
                decision_id=row.id,
                item_id=item.id,
                decision=decision,
                candidate_id=row.candidate_id,
                generation=row.generation,
                item_version=item.version,
                item_status=UnpackItemStatus(item.status),
                decided_at=row.decided_at,
                replayed=False,
            )

    def retry_match(
        self,
        item_id: str,
        *,
        expected_item_version: int,
    ) -> UnpackRetryResult:
        if expected_item_version < 1:
            raise self._invalid("If-Match item version 必须大于等于 1")
        with self._session_factory() as session:
            begin_immediate_write(session)
            item = session.get(UnpackExecutionItem, item_id)
            if item is None:
                raise self._item_not_found()
            if item.version != expected_item_version:
                raise self._version_conflict(item.version)
            current = UnpackItemStatus(item.status)
            if current not in {
                UnpackItemStatus.NO_MATCH,
                UnpackItemStatus.MATCH_TIMEOUT,
                UnpackItemStatus.MATCH_ERROR,
                UnpackItemStatus.EXECUTION_ERROR,
            }:
                raise self._conflict("只有已失败或无匹配的影片才允许重试")

            execution = session.get(UnpackExecution, item.execution_id)
            if execution is None:
                raise self._execution_not_found()
            definition = session.get(UnpackDefinition, execution.definition_id)
            if definition is None:
                raise ApplicationError(
                    code="UNPACK_DEFINITION_NOT_FOUND",
                    status=404,
                    title="数据拆包任务不存在",
                    detail="执行绑定的任务定义已不存在",
                )
            now = utc_now()
            if current is UnpackItemStatus.EXECUTION_ERROR:
                _retry_failed_execution_item(session, item, now)
            else:
                # Explicit human retry retains the existing auxiliary-resume rules.
                if not item_transition_allowed(current, UnpackItemStatus.MATCH_PENDING):
                    raise self._conflict("当前影片状态不允许重新进入匹配队列")
                _queue_failed_item(session, item, now)
            session.flush()
            _refresh_execution_state(session, execution)
            session.commit()
            return UnpackRetryResult(
                item_id=item.id,
                generation=item.candidate_generation,
                retry_count=item.retry_count,
                item_version=item.version,
                item_status=UnpackItemStatus(item.status),
            )

    def retry_content_verification(
        self,
        item_id: str,
        *,
        expected_item_version: int,
    ) -> UnpackRetryResult:
        """Retry content verification, rematching only when the selected candidate is stale."""
        if expected_item_version < 1:
            raise self._invalid("If-Match item version 必须大于等于 1")
        with self._session_factory() as session:
            begin_immediate_write(session)
            item = session.get(UnpackExecutionItem, item_id)
            if item is None:
                raise self._item_not_found()
            if item.version != expected_item_version:
                raise self._version_conflict(item.version)
            if item.status != UnpackItemStatus.CONTENT_VERIFY_ERROR.value:
                raise self._conflict("只有内容校验错误的影片才允许重试内容校验")

            external_operation = session.scalar(
                select(UnpackExternalOperationJournal.id)
                .where(UnpackExternalOperationJournal.item_id == item.id)
                .limit(1)
            )
            if external_operation is not None:
                raise self._conflict("影片已有外部操作记录，不能重新执行内容校验")

            execution = session.get(UnpackExecution, item.execution_id)
            if execution is None:
                raise self._execution_not_found()
            candidate = (
                session.get(UnpackMatchCandidate, item.selected_candidate_id)
                if item.selected_candidate_id is not None
                else None
            )
            candidate_is_current = (
                item.match_origin in {"AUTO", "MANUAL"}
                and candidate is not None
                and candidate.item_id == item.id
                and candidate.generation == item.candidate_generation
            )
            now = utc_now()
            if not candidate_is_current:
                if not item_transition_allowed(
                    UnpackItemStatus.CONTENT_VERIFY_ERROR,
                    UnpackItemStatus.MATCH_PENDING,
                ):
                    raise self._conflict("当前影片状态不允许重新进入匹配队列")
                # A missing or superseded candidate cannot be fetched safely.
                # Start a fresh match generation instead of retaining a dead ID.
                _reset_item_for_match_retry(item, now)
                session.flush()
                _refresh_execution_state(session, execution)
                session.commit()
                return UnpackRetryResult(
                    item_id=item.id,
                    generation=item.candidate_generation,
                    retry_count=item.retry_count,
                    item_version=item.version,
                    item_status=UnpackItemStatus(item.status),
                )

            assert candidate is not None
            target_status = (
                UnpackItemStatus.MATCHED_AUTO
                if item.match_origin == "AUTO"
                else UnpackItemStatus.MATCHED_MANUAL
            )
            if not item_transition_allowed(UnpackItemStatus.CONTENT_VERIFY_ERROR, target_status):
                raise self._conflict("当前影片状态不允许重新进入内容校验")

            candidate.verification_status = UnpackCandidateVerificationStatus.NOT_CHECKED.value
            candidate.verification_level = None
            candidate.verification_error_code = None
            candidate.metainfo_digest = None
            evidence = dict(candidate.evidence or {})
            evidence.pop("content_verification", None)
            candidate.evidence = evidence
            item.status = target_status.value
            item.content_verification_level = None
            item.torrent_metainfo_digest = None
            item.last_error_code = None
            item.last_error_message = None
            item.updated_at = now
            item.version += 1
            session.flush()
            _refresh_execution_state(session, execution)
            session.commit()
            return UnpackRetryResult(
                item_id=item.id,
                generation=item.candidate_generation,
                retry_count=item.retry_count,
                item_version=item.version,
                item_status=UnpackItemStatus(item.status),
            )

    def retry_eligible_items(
        self,
        execution_id: str,
        *,
        expected_execution_version: int,
        selected_statuses: tuple[UnpackItemStatus, ...],
    ) -> UnpackBulkRetryResult:
        """Retry unmatched, review, and failed items without touching external effects.

        One atomic transaction; current in-progress matching and successful items
        are preserved. Any external operation journal makes an item ineligible
        for this *bulk rematch* action, even when a separate guarded one-item
        recovery would be possible.
        """
        if expected_execution_version < 1:
            raise self._invalid("If-Match execution version 必须大于等于 1")
        if not selected_statuses:
            raise self._invalid("请至少选择一种拆包状态")
        if len(selected_statuses) != len(set(selected_statuses)):
            raise self._invalid("拆包状态不能重复选择")
        if any(status not in BULK_REMATCHABLE_ITEM_STATUSES for status in selected_statuses):
            raise self._invalid("包含不能批量重试的拆包状态")
        retryable = tuple(status.value for status in selected_statuses)
        with self._session_factory() as session:
            begin_immediate_write(session)
            execution = session.get(UnpackExecution, execution_id)
            if execution is None:
                raise self._execution_not_found()
            if execution.version != expected_execution_version:
                raise ApplicationError(
                    code="UNPACK_EXECUTION_VERSION_CONFLICT",
                    status=409,
                    title="执行记录已发生变化",
                    detail="请刷新任务列表后重试",
                )
            if session.get(UnpackDefinition, execution.definition_id) is None:
                raise self._execution_not_found()
            items = session.scalars(
                select(UnpackExecutionItem)
                .where(
                    UnpackExecutionItem.execution_id == execution.id,
                    UnpackExecutionItem.status.in_(retryable),
                )
                .order_by(UnpackExecutionItem.id)
            ).all()
            if not items:
                return UnpackBulkRetryResult(
                    execution_id=execution.id,
                    retried_count=0,
                    skipped_unsafe_count=0,
                )
            item_ids = tuple(item.id for item in items)
            unsafe_item_ids = set(
                session.scalars(
                    select(UnpackExternalOperationJournal.item_id)
                    .where(UnpackExternalOperationJournal.item_id.in_(item_ids))
                    .distinct()
                ).all()
            )
            now = utc_now()
            retried = 0
            for item in items:
                if item.id in unsafe_item_ids:
                    continue
                if item.status == UnpackItemStatus.EXECUTION_ERROR.value:
                    _retry_failed_execution_item(session, item, now)
                else:
                    # A fresh matching generation safely discards earlier candidate
                    # selections. It does NOT erase journal history or media files.
                    _reset_item_for_match_retry(item, now)
                retried += 1
            if retried:
                session.flush()
                _refresh_execution_state(session, execution)
            session.commit()
            return UnpackBulkRetryResult(
                execution_id=execution.id,
                retried_count=retried,
                skipped_unsafe_count=len(unsafe_item_ids),
            )

    def delete_item(self, item_id: str, *, expected_item_version: int) -> None:
        """Forget an eligible execution-list entry, never media or external journals."""
        if expected_item_version < 1:
            raise self._invalid("If-Match item version 必须大于等于 1")
        with self._session_factory() as session:
            begin_immediate_write(session)
            item = session.get(UnpackExecutionItem, item_id)
            if item is None:
                raise self._item_not_found()
            if item.version != expected_item_version:
                raise self._version_conflict(item.version)
            execution = session.get(UnpackExecution, item.execution_id)
            if execution is None:
                raise self._execution_not_found()
            if item.status in {
                UnpackItemStatus.MATCH_PENDING.value,
                UnpackItemStatus.MATCHING.value,
            }:
                raise ApplicationError(
                    code="UNPACK_ITEM_DELETE_ACTIVE",
                    status=409,
                    title="影片仍在待匹配或匹配中",
                    detail="请等待影片匹配结束后再删除",
                )
            # Other item states can be removed while their historical external
            # journal is atomically archived to an independent audit table.
            archive_deleted_item_operations(
                session, definition_id=execution.definition_id, item=item
            )
            session.execute(
                delete(UnpackExternalOperationJournal).where(
                    UnpackExternalOperationJournal.item_id == item.id
                )
            )
            session.execute(
                delete(UnpackReviewDecisionModel).where(
                    UnpackReviewDecisionModel.item_id == item.id
                )
            )
            session.execute(
                delete(UnpackMatchCandidate).where(UnpackMatchCandidate.item_id == item.id)
            )
            session.delete(item)
            session.flush()
            execution.total_count = int(
                session.scalar(
                    select(func.count(UnpackExecutionItem.id)).where(
                        UnpackExecutionItem.execution_id == execution.id
                    )
                )
                or 0
            )
            if execution.total_count == 0:
                execution.matched_auto_count = 0
                execution.review_count = 0
                execution.content_verified_count = 0
                execution.content_mismatch_count = 0
                execution.timeout_count = 0
                execution.error_count = 0
                execution.completed_count = 0
                execution.status = UnpackExecutionStatus.CANCELLED.value
                execution.finished_at = utc_now()
                execution.updated_at = utc_now()
                execution.version += 1
            else:
                _refresh_execution_state(session, execution)
            session.commit()

    def _replay_review(
        self,
        session: Session,
        existing: UnpackReviewDecisionModel,
        *,
        item_id: str,
        decision: UnpackReviewDecision,
        candidate_id: str | None,
        generation: int,
        expected_item_version: int,
    ) -> UnpackReviewResult:
        if (
            existing.item_id != item_id
            or existing.decision != decision.value
            or existing.candidate_id != candidate_id
            or existing.generation != generation
            or existing.item_version_before != expected_item_version
        ):
            raise ApplicationError(
                code="UNPACK_REVIEW_IDEMPOTENCY_CONFLICT",
                status=409,
                title="审核幂等键冲突",
                detail="同一个 Idempotency-Key 已绑定不同审核意图",
            )
        item = session.get(UnpackExecutionItem, existing.item_id)
        if item is None:
            raise self._item_not_found()
        expected_status = (
            UnpackItemStatus.MATCHED_MANUAL
            if decision is UnpackReviewDecision.APPROVE
            else UnpackItemStatus.NO_MATCH
        )
        if (
            item.version < existing.item_version_before + 1
            or UnpackItemStatus(item.status) is not expected_status
            or item.candidate_generation != existing.generation
            or item.selected_candidate_id != existing.candidate_id
        ):
            raise ApplicationError(
                code="UNPACK_REVIEW_REPLAY_STALE",
                status=409,
                title="审核重放状态已变化",
                detail="审核曾经成功，但影片项随后已经变化，请刷新当前状态",
            )
        return UnpackReviewResult(
            decision_id=existing.id,
            item_id=existing.item_id,
            decision=UnpackReviewDecision(existing.decision),
            candidate_id=existing.candidate_id,
            generation=existing.generation,
            item_version=item.version,
            item_status=UnpackItemStatus(item.status),
            decided_at=existing.decided_at,
            replayed=True,
        )

    @staticmethod
    def _invalid(detail: str) -> ApplicationError:
        return ApplicationError(
            code="UNPACK_ITEM_ACTION_INVALID",
            status=422,
            title="数据拆包影片操作参数无效",
            detail=detail,
        )

    @staticmethod
    def _item_not_found() -> ApplicationError:
        return ApplicationError(
            code="UNPACK_ITEM_NOT_FOUND",
            status=404,
            title="数据拆包影片项不存在",
            detail="未找到指定影片项",
        )

    @staticmethod
    def _execution_not_found() -> ApplicationError:
        return ApplicationError(
            code="UNPACK_EXECUTION_NOT_FOUND",
            status=404,
            title="数据拆包执行不存在",
            detail="影片项绑定的执行已不存在",
        )

    @staticmethod
    def _version_conflict(current_version: int) -> ApplicationError:
        return ApplicationError(
            code="UNPACK_ITEM_VERSION_CONFLICT",
            status=409,
            title="影片项版本冲突",
            detail=f"影片项已经更新，当前版本为 {current_version}",
        )

    @staticmethod
    def _conflict(detail: str) -> ApplicationError:
        return ApplicationError(
            code="UNPACK_ITEM_ACTION_CONFLICT",
            status=409,
            title="数据拆包影片状态冲突",
            detail=detail,
        )


_AUXILIARY_STARTED_OPERATIONS = frozenset(
    {
        "UNPACK_AUX_STAGING_DIR",
        "UNPACK_AUX_TORRENT_ADD",
        "UNPACK_AUX_FILE_SELECTION",
        "UNPACK_AUX_TORRENT_START",
    }
)


# Only a stage before final client START can be safely resumed under the
# original frozen plan, with every past external effect proven APPLIED.
_SAFE_SEED_RETRY_ERRORS = frozenset(
    {
        "SITE_INVALID_RESPONSE",
        "SITE_UNAVAILABLE",
        "SITE_TIMEOUT",
        "SITE_RATE_LIMITED",
        "SITE_CIRCUIT_OPEN",
        "UNPACK_SEED_SKIP_CHECK_NOT_COMPLETE",
        "DOWNLOADER_UNAVAILABLE",
        "DOWNLOADER_CONNECTION_FAILED",
    }
)
_MATERIALIZED_OPERATIONS = frozenset(
    {
        "UNPACK_EXEC_CREATE_DIRECTORY",
        "UNPACK_EXEC_MATERIALIZE_HARDLINK",
        "UNPACK_EXEC_MATERIALIZE_SYMLINK",
        "UNPACK_EXEC_MATERIALIZE_COPY",
        "UNPACK_EXEC_REUSE_EXISTING",
    }
)
_ADD_OPERATIONS = frozenset({"UNPACK_EXEC_QBITTORRENT_ADD", "UNPACK_EXEC_TRANSMISSION_ADD"})


def _plan_retry_allowed(session: Session, item: UnpackExecutionItem) -> bool:
    if (
        item.content_verification_level != "FULL_VERIFIED"
        or item.selected_candidate_id is None
        or not isinstance(item.torrent_metainfo_digest, str)
        or item.execution_plan is not None
        or item.execution_plan_digest is not None
    ):
        return False
    checkpoint = dict(item.execution_state or {})
    if checkpoint.get("stage") not in {None, "PLAN_PENDING"}:
        return False
    candidate = session.get(UnpackMatchCandidate, item.selected_candidate_id)
    return bool(
        candidate is not None
        and candidate.item_id == item.id
        and candidate.generation == item.candidate_generation
        and candidate.verification_status == UnpackCandidateVerificationStatus.VERIFIED.value
        and candidate.verification_level == "FULL_VERIFIED"
        and candidate.metainfo_digest == item.torrent_metainfo_digest
    )


def _materialization_resume_allowed(item: UnpackExecutionItem) -> bool:
    if (
        item.content_verification_level != "FULL_VERIFIED"
        or item.selected_candidate_id is None
        or not isinstance(item.execution_plan, dict)
        or not isinstance(item.execution_plan_digest, str)
    ):
        return False
    checkpoint = dict(item.execution_state or {})
    if checkpoint.get("stage") not in {None, "PLAN_PENDING"}:
        return False
    try:
        plan = unpack_execution_plan_from_payload(item.execution_plan)
    except ValueError:
        return False
    return bool(
        plan.ready
        and plan.plan_digest == item.execution_plan_digest
        and plan.item_id == item.id
        and plan.candidate_id == item.selected_candidate_id
        and plan.candidate_generation == item.candidate_generation
        and plan.metainfo_digest == item.torrent_metainfo_digest
    )


def _safe_seed_resume_allowed(
    item: UnpackExecutionItem,
    journals: Sequence[UnpackExternalOperationJournal],
) -> bool:
    checkpoint = dict(item.execution_state or {})
    seen_ops = {row.operation_type for row in journals}
    return bool(
        item.last_error_code in _SAFE_SEED_RETRY_ERRORS
        and checkpoint.get("stage") in {"FILES_MATERIALIZED", "ADD_RESUME_CHECKING"}
        and isinstance(item.execution_plan, dict)
        and isinstance(item.execution_plan_digest, str)
        and checkpoint.get("plan_digest") == item.execution_plan_digest
        and item.selected_candidate_id is not None
        and item.torrent_metainfo_digest is not None
        and journals
        and all(row.status == OperationStatus.APPLIED.value for row in journals)
        and len({row.idempotency_key for row in journals}) == len(journals)
        and bool(seen_ops & _MATERIALIZED_OPERATIONS)
        and seen_ops.issubset(_MATERIALIZED_OPERATIONS | _ADD_OPERATIONS)
        and len(seen_ops & _ADD_OPERATIONS) <= 1
    )


def _auxiliary_resume_allowed(
    session: Session,
    item: UnpackExecutionItem,
    operations: Sequence[UnpackExternalOperationJournal],
) -> bool:
    state = dict(item.auxiliary_state or {})
    candidate = (
        session.get(UnpackMatchCandidate, item.selected_candidate_id)
        if item.selected_candidate_id
        else None
    )
    by_type = {operation.operation_type: operation for operation in operations}
    started_intents_complete = all(
        operation.status == OperationStatus.APPLIED.value
        for name, operation in by_type.items()
        if name in _AUXILIARY_STARTED_OPERATIONS
    ) and _AUXILIARY_STARTED_OPERATIONS.issubset(by_type)
    ordinary_resume = item.last_error_code in {
        "DOWNLOADER_UNAVAILABLE",
        "DOWNLOADER_CONNECTION_FAILED",
        "SITE_UNAVAILABLE",
        "SITE_TIMEOUT",
        "SITE_RATE_LIMITED",
        "SITE_CIRCUIT_OPEN",
    } and len(operations) == len(_AUXILIARY_STARTED_OPERATIONS)
    stop_reconciliation_resume = (
        item.last_error_code == "UNPACK_AUXILIARY_CONFLICT"
        and len(operations) == len(_AUXILIARY_STARTED_OPERATIONS) + 1
        and set(by_type) == _AUXILIARY_STARTED_OPERATIONS | {"UNPACK_AUX_TORRENT_STOP"}
        and (stop := by_type.get("UNPACK_AUX_TORRENT_STOP")) is not None
        and stop.status == OperationStatus.RECONCILE_REQUIRED.value
        and stop.last_error_code == "UNPACK_AUX_STOP_NOT_OBSERVED"
    )
    return bool(
        item.status == UnpackItemStatus.MATCH_ERROR.value
        and (ordinary_resume or stop_reconciliation_resume)
        and state.get("state") == "DOWNLOADING"
        and candidate is not None
        and candidate.item_id == item.id
        and candidate.generation == item.candidate_generation
        and item.torrent_metainfo_digest is not None
        and all(
            isinstance(state.get(key), str) and bool(state[key])
            for key in (
                "torrent_hash",
                "binding_digest",
                "remote_save_path",
                "staging_path",
                "ownership_tag",
                "target_downloader_id",
            )
        )
        and isinstance(state.get("downloader_version"), int)
        and not isinstance(state.get("downloader_version"), bool)
        and started_intents_complete
        and len(by_type) == len(operations)
    )


def unpack_item_retry_capability(
    session: Session,
    item: UnpackExecutionItem,
    journals: Sequence[UnpackExternalOperationJournal] | None = None,
) -> tuple[RetryCapability, str | None]:
    """Return the single backend-authoritative retry decision and its safe reason."""
    operations = journals
    if operations is None:
        operations = session.scalars(
            select(UnpackExternalOperationJournal).where(
                UnpackExternalOperationJournal.item_id == item.id
            )
        ).all()
    status = UnpackItemStatus(item.status)
    if status is UnpackItemStatus.CONTENT_VERIFY_ERROR:
        return (
            ("VERIFICATION", None)
            if not operations
            else (
                "RECONCILE",
                "内容校验失败的影片已经存在外部操作记录，不能重新校验或重新匹配；请先查看执行明细。",
            )
        )
    if status in {UnpackItemStatus.NO_MATCH, UnpackItemStatus.MATCH_TIMEOUT}:
        return (
            ("MATCH", None)
            if not operations
            else (
                "RECONCILE",
                "该影片存在外部操作记录，不能按普通匹配任务重试；请先查看执行明细。",
            )
        )
    if status is UnpackItemStatus.MATCH_ERROR:
        if not operations:
            return "MATCH", None
        if _auxiliary_resume_allowed(session, item, operations):
            return "RESUME", None
        return (
            "RECONCILE",
            "辅助文件补齐的下载器操作未全部得到可验证的结果，不能自动重新添加或继续；请先查看执行明细。",
        )
    if status is UnpackItemStatus.EXECUTION_ERROR:
        if not operations:
            if _plan_retry_allowed(session, item):
                return "PLAN", None
            if _materialization_resume_allowed(item):
                return "RESUME", None
            return "MATCH", None
        if _safe_seed_resume_allowed(item, operations):
            return "RESUME", None
        return (
            "RECONCILE",
            "存在未确认或不完整的文件/下载器操作记录，无法安全自动续跑。请先在“执行明细”中核对操作结果；不会清空历史记录或重复添加种子。",
        )
    return "NONE", None


def _retry_failed_execution_item(
    session: Session, item: UnpackExecutionItem, now: datetime
) -> None:
    """Safely resume the failed pipeline stage without throwing away evidence.

    No reusable candidate or plan: restart matching. FULL_VERIFIED without a
    frozen plan: rebuild only the execution plan. A frozen plan with no external
    effects: resume materialization. Proven APPLIED file operations: resume the
    same plan and generation; the seeding worker revalidates every identity.
    Unknown, pending, conflicting or later-stage effects fail closed.
    """
    journals = session.scalars(
        select(UnpackExternalOperationJournal).where(
            UnpackExternalOperationJournal.item_id == item.id
        )
    ).all()
    capability, reason = unpack_item_retry_capability(session, item, journals)
    if capability == "MATCH":
        _reset_item_for_match_retry(item, now)
        return
    if capability not in {"PLAN", "RESUME"}:
        raise ApplicationError(
            code="UNPACK_RETRY_EXTERNAL_RECONCILE_REQUIRED",
            status=409,
            title="需要先核对已执行的文件或下载器操作",
            detail=reason or "当前执行证据不足以安全恢复，请先在“执行明细”中核对操作记录。",
        )

    assert_unmarked_execution_for_write(session, item.execution_id)
    if capability == "PLAN":
        # The candidate and FULL_VERIFIED evidence remain intact. The plan worker
        # will rebuild only the frozen plan; matching and Piece Hash are not repeated.
        item.status = UnpackItemStatus.CONTENT_VERIFIED.value
        item.execution_state = {
            **dict(item.execution_state or {}),
            "stage": "PLAN_PENDING",
            "candidate_id": item.selected_candidate_id,
            "candidate_generation": item.candidate_generation,
            "metainfo_digest": item.torrent_metainfo_digest,
        }
    elif journals:
        # The same frozen plan and APPLIED materialization journals are retained.
        # The seeding worker rechecks all file identities and downloader ownership.
        item.status = UnpackItemStatus.EXECUTING.value
    else:
        # A frozen plan exists but no external operation was recorded; resume
        # materialization from the plan rather than throwing away the match.
        item.status = UnpackItemStatus.PLAN_PENDING.value
    item.last_error_code = None
    item.last_error_message = None
    item.updated_at = now
    item.version += 1


def _queue_failed_item(session: Session, item: UnpackExecutionItem, now: datetime) -> None:
    operations = session.scalars(
        select(UnpackExternalOperationJournal).where(
            UnpackExternalOperationJournal.item_id == item.id
        )
    ).all()
    if not operations:
        _reset_item_for_match_retry(item, now)
        return

    if _auxiliary_resume_allowed(session, item, operations):
        # Keep the original candidate, generation, remote torrent ownership,
        # hashes and journal. On STOP reconciliation, the worker must observe
        # the owned remote torrent already stopped before marking STOP applied;
        # a running torrent remains blocked without a second stop request.
        item.status = UnpackItemStatus.AUXILIARY_FETCHING.value
        item.last_error_code = None
        item.last_error_message = None
        item.updated_at = now
        item.version += 1
        return

    raise ApplicationError(
        code="UNPACK_RETRY_EXTERNAL_RECONCILE_REQUIRED",
        status=409,
        title="影片存在尚未对账的外部操作",
        detail="不能清除已发生的下载器或文件系统操作记录并重新匹配，请先对账",
    )


def _reset_item_for_match_retry(
    item: UnpackExecutionItem,
    now: datetime,
    *,
    automatic: bool = False,
    retry_not_before: datetime | None = None,
) -> None:
    item.status = UnpackItemStatus.MATCH_PENDING.value
    if automatic:
        item.retry_count += 1
    else:
        # A human restart begins a new automatic retry budget. In earlier
        # versions this field also counted manual retries; reset the legacy
        # mixed value instead of blocking subsequent manual actions.
        item.retry_count = 0
    item.candidate_generation += 1
    item.selected_candidate_id = None
    item.match_origin = None
    item.content_verification_level = None
    item.torrent_metainfo_digest = None
    item.auxiliary_state = (
        {"auto_retry_not_before": retry_not_before.isoformat()}
        if retry_not_before is not None
        else None
    )
    item.execution_plan = None
    item.execution_plan_digest = None
    item.execution_plan_created_at = None
    item.execution_state = None
    item.last_error_code = None
    item.last_error_message = None
    item.match_started_at = None
    item.match_finished_at = None
    item.updated_at = now
    item.version += 1


def _refresh_execution_state(session: Session, execution: UnpackExecution) -> None:
    refresh_unpack_execution_state(session, execution)
