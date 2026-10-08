from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.errors import ApplicationError
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
            if current not in {UnpackItemStatus.MATCH_TIMEOUT, UnpackItemStatus.MATCH_ERROR}:
                raise self._conflict("只有匹配超时或匹配错误的影片才允许重试")

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
            if not definition.retry_enabled:
                raise ApplicationError(
                    code="UNPACK_MATCH_RETRY_DISABLED",
                    status=409,
                    title="自动重试已关闭",
                    detail="当前任务没有启用匹配重试",
                )
            if item.retry_count >= definition.max_retries:
                raise ApplicationError(
                    code="UNPACK_MATCH_RETRY_EXHAUSTED",
                    status=409,
                    title="匹配重试次数已用完",
                    detail=f"当前任务最多允许重试 {definition.max_retries} 次",
                )
            if not item_transition_allowed(current, UnpackItemStatus.MATCH_PENDING):
                raise self._conflict("当前影片状态不允许重新进入匹配队列")

            now = utc_now()
            item.status = UnpackItemStatus.MATCH_PENDING.value
            item.retry_count += 1
            item.candidate_generation += 1
            item.selected_candidate_id = None
            item.match_origin = None
            item.content_verification_level = None
            item.torrent_metainfo_digest = None
            item.auxiliary_state = None
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
