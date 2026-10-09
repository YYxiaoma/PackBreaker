from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.errors import ApplicationError
from backend.app.domain.operation import OperationStatus
from backend.app.domain.unpack import (
    REVIEWABLE_ITEM_STATUSES,
    UnpackExecutionStatus,
    UnpackItemStatus,
    UnpackReviewDecision,
    item_transition_allowed,
)
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
            }:
                raise self._conflict("只有无匹配、匹配超时或匹配错误的影片才允许重试")

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
            # This endpoint is a deliberate human action, not the task's
            # automatic retry policy. NO_MATCH never consumes auto retries.
            if not item_transition_allowed(current, UnpackItemStatus.MATCH_PENDING):
                raise self._conflict("当前影片状态不允许重新进入匹配队列")

            now = utc_now()
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

    def retry_failed_matches(self, execution_id: str, *, expected_execution_version: int) -> int:
        """Atomically requeue all eligible failed searches in one execution."""
        if expected_execution_version < 1:
            raise self._invalid("If-Match execution version 必须大于等于 1")
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
                    detail="请刷新后重新操作",
                )
            if execution.status not in {
                UnpackExecutionStatus.FAILED.value,
                UnpackExecutionStatus.COMPLETED_WITH_ERRORS.value,
            }:
                raise self._conflict("只有已结束且匹配失败的任务允许批量重试")
            definition = session.get(UnpackDefinition, execution.definition_id)
            if definition is None:
                raise self._execution_not_found()
            items = session.scalars(
                select(UnpackExecutionItem).where(
                    UnpackExecutionItem.execution_id == execution.id,
                    UnpackExecutionItem.status.in_(
                        (
                            UnpackItemStatus.NO_MATCH.value,
                            UnpackItemStatus.MATCH_TIMEOUT.value,
                            UnpackItemStatus.MATCH_ERROR.value,
                        )
                    ),
                )
            ).all()
            eligible = list(items)
            if not eligible:
                raise self._conflict("没有可供人工重新匹配的影片")
            now = utc_now()
            for item in eligible:
                _queue_failed_item(session, item, now)
            session.flush()
            _refresh_execution_state(session, execution)
            session.commit()
            return len(eligible)

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
            if execution.status not in {
                UnpackExecutionStatus.COMPLETED.value,
                UnpackExecutionStatus.COMPLETED_WITH_ERRORS.value,
                UnpackExecutionStatus.FAILED.value,
                UnpackExecutionStatus.CANCELLED.value,
            } or item.status not in {
                UnpackItemStatus.NO_MATCH.value,
                UnpackItemStatus.MATCH_ERROR.value,
                UnpackItemStatus.MATCH_TIMEOUT.value,
                UnpackItemStatus.CONTENT_MISMATCH.value,
                UnpackItemStatus.EXECUTION_ERROR.value,
                UnpackItemStatus.COMPLETED.value,
                UnpackItemStatus.CANCELLED.value,
            }:
                raise ApplicationError(
                    code="UNPACK_ITEM_DELETE_ACTIVE",
                    status=409,
                    title="影片仍在执行或待审核",
                    detail="只有已结束的执行中的终态影片才允许删除",
                )
            if (
                session.scalar(
                    select(UnpackExternalOperationJournal.id)
                    .where(UnpackExternalOperationJournal.item_id == item.id)
                    .limit(1)
                )
                is not None
            ):
                raise ApplicationError(
                    code="UNPACK_ITEM_DELETE_EXTERNAL_JOURNAL",
                    status=409,
                    title="影片已有外部操作记录",
                    detail="必须保留辅助下载或辅种操作的对账证据，不能删除该影片记录",
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


def _queue_failed_item(session: Session, item: UnpackExecutionItem, now: datetime) -> None:
    operations = session.scalars(
        select(UnpackExternalOperationJournal).where(
            UnpackExternalOperationJournal.item_id == item.id
        )
    ).all()
    if not operations:
        _reset_item_for_match_retry(item, now)
        return

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
        "SITE_RATE_LIMITED",
    } and len(operations) == len(_AUXILIARY_STARTED_OPERATIONS)
    stop_reconciliation_resume = (
        item.last_error_code == "UNPACK_AUXILIARY_CONFLICT"
        and len(operations) == len(_AUXILIARY_STARTED_OPERATIONS) + 1
        and set(by_type) == _AUXILIARY_STARTED_OPERATIONS | {"UNPACK_AUX_TORRENT_STOP"}
        and (stop := by_type.get("UNPACK_AUX_TORRENT_STOP")) is not None
        and stop.status == OperationStatus.RECONCILE_REQUIRED.value
        and stop.last_error_code == "UNPACK_AUX_STOP_NOT_OBSERVED"
    )
    if (
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
    ):
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
    counts = {
        status: int(
            session.scalar(
                select(func.count(UnpackExecutionItem.id))
                .where(UnpackExecutionItem.execution_id == execution.id)
                .where(UnpackExecutionItem.status == status.value)
            )
            or 0
        )
        for status in UnpackItemStatus
    }
    execution.matched_auto_count = counts[UnpackItemStatus.MATCHED_AUTO]
    execution.review_count = counts[UnpackItemStatus.REVIEW_REQUIRED]
    execution.content_verified_count = counts[UnpackItemStatus.CONTENT_VERIFIED]
    execution.content_mismatch_count = counts[UnpackItemStatus.CONTENT_MISMATCH]
    execution.timeout_count = counts[UnpackItemStatus.MATCH_TIMEOUT]
    execution.error_count = (
        counts[UnpackItemStatus.MATCH_ERROR] + counts[UnpackItemStatus.EXECUTION_ERROR]
    )
    execution.completed_count = counts[UnpackItemStatus.COMPLETED]

    matching_active = counts[UnpackItemStatus.MATCH_PENDING] + counts[UnpackItemStatus.MATCHING]
    content_active = sum(
        counts[status]
        for status in (
            UnpackItemStatus.MATCHED_AUTO,
            UnpackItemStatus.MATCHED_MANUAL,
            UnpackItemStatus.TORRENT_FETCHING,
            UnpackItemStatus.AUXILIARY_FETCHING,
            UnpackItemStatus.CONTENT_VERIFYING,
            UnpackItemStatus.CONTENT_VERIFIED,
        )
    )
    execution_active = counts[UnpackItemStatus.PLAN_PENDING] + counts[UnpackItemStatus.EXECUTING]
    client_active = counts[UnpackItemStatus.CLIENT_VERIFYING]
    now = utc_now()

    if matching_active:
        execution.status = UnpackExecutionStatus.MATCHING.value
        execution.finished_at = None
    elif counts[UnpackItemStatus.REVIEW_REQUIRED]:
        execution.status = UnpackExecutionStatus.REVIEW_REQUIRED.value
        execution.finished_at = None
    elif content_active:
        execution.status = UnpackExecutionStatus.CONTENT_VERIFYING.value
        execution.finished_at = None
    elif execution_active:
        execution.status = UnpackExecutionStatus.EXECUTING.value
        execution.finished_at = None
    elif client_active:
        execution.status = UnpackExecutionStatus.CLIENT_VERIFYING.value
        execution.finished_at = None
    elif execution.completed_count == execution.total_count:
        execution.status = UnpackExecutionStatus.COMPLETED.value
        execution.finished_at = now
    elif (
        execution.total_count > 0
        and sum(
            counts[status]
            for status in (
                UnpackItemStatus.NO_MATCH,
                UnpackItemStatus.MATCH_TIMEOUT,
                UnpackItemStatus.MATCH_ERROR,
                UnpackItemStatus.CONTENT_MISMATCH,
                UnpackItemStatus.EXECUTION_ERROR,
            )
        )
        == execution.total_count
    ):
        execution.status = UnpackExecutionStatus.FAILED.value
        execution.finished_at = now
    elif any(
        counts[status]
        for status in (
            UnpackItemStatus.NO_MATCH,
            UnpackItemStatus.MATCH_TIMEOUT,
            UnpackItemStatus.MATCH_ERROR,
            UnpackItemStatus.CONTENT_MISMATCH,
            UnpackItemStatus.EXECUTION_ERROR,
        )
    ):
        execution.status = UnpackExecutionStatus.COMPLETED_WITH_ERRORS.value
        execution.finished_at = now
    execution.updated_at = now
    execution.version += 1
