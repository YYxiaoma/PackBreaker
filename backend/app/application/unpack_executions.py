from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.errors import ApplicationError
from backend.app.application.unpack_acceptance import READ_ONLY_ACCEPTANCE_MARKER
from backend.app.domain.unpack import (
    REVIEWABLE_ITEM_STATUSES,
    UnpackExecutionStatus,
    UnpackItemStatus,
)
from backend.app.domain.unpack_execution_plan import unpack_execution_plan_from_payload
from backend.app.infrastructure.persistence.models import (
    UnpackExecution,
    UnpackExecutionItem,
    UnpackExternalOperationJournal,
    UnpackMatchCandidate,
)


@dataclass(frozen=True, slots=True)
class UnpackExecutionView:
    id: str
    definition_id: str
    trigger: str
    status: UnpackExecutionStatus
    discovery_complete: bool
    discovery_cursor: str | None
    total_count: int
    matched_auto_count: int
    no_match_count: int
    review_count: int
    content_verified_count: int
    content_mismatch_count: int
    timeout_count: int
    error_count: int
    completed_count: int
    started_at: datetime
    finished_at: datetime | None
    version: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class UnpackExecutionItemView:
    id: str
    execution_id: str
    source_object_key: str
    source_snapshot: dict[str, object]
    media_identity: dict[str, object]
    status: UnpackItemStatus
    selected_candidate_id: str | None
    match_origin: str | None
    candidate_generation: int
    retry_count: int
    content_verification_level: str | None
    torrent_metainfo_digest: str | None
    auxiliary_state: dict[str, object] | None
    review_allowed: bool
    has_external_operations: bool
    last_error_code: str | None
    last_error_message: str | None
    match_started_at: datetime | None
    match_finished_at: datetime | None
    version: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class UnpackExecutionItemPage:
    items: tuple[UnpackExecutionItemView, ...]
    next_cursor: str | None
    has_more: bool


@dataclass(frozen=True, slots=True)
class UnpackMatchCandidateView:
    id: str
    item_id: str
    generation: int
    site_id: str
    candidate_key: str
    title: str
    size_bytes: int | None
    imdb_id: str | None
    douban_id: str | None
    seeders: int | None
    score_bps: int
    is_exact_match: bool
    evidence: dict[str, object]
    verification_status: str
    verification_level: str | None
    verification_error_code: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class UnpackMatchCandidateList:
    item_id: str
    generation: int
    item_version: int
    default_candidate_id: str | None
    candidates: tuple[UnpackMatchCandidateView, ...]


@dataclass(frozen=True, slots=True)
class UnpackOperationDiagnosticView:
    operation_type: str
    status: str
    last_error_code: str | None
    downloader_id: str | None
    torrent_hash: str | None
    target_path: str | None
    last_observed_state: str | None
    recorded_file_type: str | None
    recorded_device: int | None
    recorded_inode: int | None
    recorded_size: int | None
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class UnpackItemDiagnosticsView:
    item_id: str
    status: UnpackItemStatus
    item_version: int
    execution_stage: str | None
    last_error_code: str | None
    plan_status: str
    plan_blocked_reasons: tuple[str, ...]
    acceptance_read_only: bool
    last_error_message: str | None
    requires_manual_reconciliation: bool
    reconciliation_reasons: tuple[str, ...]
    downloader_state_live_checked: bool
    filesystem_state_live_checked: bool
    operations_truncated: bool
    operations: tuple[UnpackOperationDiagnosticView, ...]


class UnpackExecutionQueryService:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def list(
        self,
        *,
        definition_id: str | None = None,
        status: UnpackExecutionStatus | None = None,
        limit: int = 100,
    ) -> tuple[UnpackExecutionView, ...]:
        if limit < 1 or limit > 500:
            raise self._invalid("execution 查询数量必须位于 1 到 500 之间")
        with self._session_factory() as session:
            statement = select(UnpackExecution)
            if definition_id:
                statement = statement.where(UnpackExecution.definition_id == definition_id)
            if status is not None:
                statement = statement.where(UnpackExecution.status == status.value)
            records = session.scalars(
                statement.order_by(
                    UnpackExecution.created_at.desc(),
                    UnpackExecution.id.desc(),
                ).limit(limit)
            ).all()
            no_match_counts = self._no_match_counts(session, tuple(item.id for item in records))
            return tuple(
                self._execution_view(item, no_match_count=no_match_counts.get(item.id, 0))
                for item in records
            )

    def get(self, execution_id: str) -> UnpackExecutionView:
        with self._session_factory() as session:
            execution = self._require_execution(session, execution_id)
            counts = self._no_match_counts(session, (execution.id,))
            return self._execution_view(execution, no_match_count=counts.get(execution.id, 0))

    @staticmethod
    def _no_match_counts(session: Session, execution_ids: tuple[str, ...]) -> dict[str, int]:
        if not execution_ids:
            return {}
        return {
            execution_id: int(count)
            for execution_id, count in session.execute(
                select(
                    UnpackExecutionItem.execution_id,
                    func.count(UnpackExecutionItem.id),
                )
                .where(UnpackExecutionItem.execution_id.in_(execution_ids))
                .where(UnpackExecutionItem.status == UnpackItemStatus.NO_MATCH.value)
                .group_by(UnpackExecutionItem.execution_id)
            )
        }

    def list_items(
        self,
        execution_id: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
        status: UnpackItemStatus | None = None,
        query: str | None = None,
    ) -> UnpackExecutionItemPage:
        if limit < 1 or limit > 200:
            raise self._invalid("影片项分页大小必须位于 1 到 200 之间")
        with self._session_factory() as session:
            self._require_execution(session, execution_id)
            statement = select(UnpackExecutionItem).where(
                UnpackExecutionItem.execution_id == execution_id
            )
            if cursor:
                cursor_item = session.get(UnpackExecutionItem, cursor)
                if cursor_item is None or cursor_item.execution_id != execution_id:
                    raise self._invalid("影片项分页游标无效")
                statement = statement.where(UnpackExecutionItem.id > cursor_item.id)
            if status is not None:
                statement = statement.where(UnpackExecutionItem.status == status.value)
            if query and query.strip():
                pattern = f"%{query.strip().lower()}%"
                statement = statement.where(
                    or_(
                        func.lower(UnpackExecutionItem.source_snapshot["path"].as_string()).like(
                            pattern
                        ),
                        func.lower(
                            UnpackExecutionItem.source_snapshot["relative_path"].as_string()
                        ).like(pattern),
                        func.lower(UnpackExecutionItem.media_identity["raw_name"].as_string()).like(
                            pattern
                        ),
                    )
                )
            records = session.scalars(
                statement.order_by(UnpackExecutionItem.id).limit(limit + 1)
            ).all()
            has_more = len(records) > limit
            visible = records[:limit]
            visible_ids = [item.id for item in visible]
            side_effect_item_ids = (
                set(
                    session.scalars(
                        select(UnpackExternalOperationJournal.item_id).where(
                            UnpackExternalOperationJournal.item_id.in_(visible_ids)
                        )
                    ).all()
                )
                if visible_ids
                else set()
            )
            return UnpackExecutionItemPage(
                items=tuple(
                    self._item_view(
                        item,
                        review_allowed=(
                            UnpackItemStatus(item.status) in REVIEWABLE_ITEM_STATUSES
                            and item.id not in side_effect_item_ids
                        ),
                        has_external_operations=item.id in side_effect_item_ids,
                    )
                    for item in visible
                ),
                next_cursor=visible[-1].id if visible and has_more else None,
                has_more=has_more,
            )

    def item_diagnostics(self, item_id: str) -> UnpackItemDiagnosticsView:
        """Read-only, bounded, allowlisted historical evidence for manual reconciliation.

        Neither downloader RPC nor media filesystem calls occur here. Raw
        external journal intents can contain secrets and must never be returned.
        """
        with self._session_factory() as session:
            item = session.get(UnpackExecutionItem, item_id)
            if item is None:
                raise ApplicationError(
                    code="UNPACK_ITEM_NOT_FOUND",
                    status=404,
                    title="数据拆包影片项不存在",
                    detail="未找到指定影片项",
                )
            rows = session.scalars(
                select(UnpackExternalOperationJournal)
                .where(UnpackExternalOperationJournal.item_id == item.id)
                .order_by(
                    UnpackExternalOperationJournal.created_at,
                    UnpackExternalOperationJournal.id,
                )
                .limit(201)
            ).all()
            operations_truncated = len(rows) > 200
            records = rows[:200]
            # A truncated list must not silently hide older journal conflicts.
            any_reconcile = (
                session.scalar(
                    select(UnpackExternalOperationJournal.id)
                    .where(UnpackExternalOperationJournal.item_id == item.id)
                    .where(UnpackExternalOperationJournal.status == "RECONCILE_REQUIRED")
                    .limit(1)
                )
                is not None
            )
            # A failed task with a still unacknowledged external intent must
            # not be described as clear merely because no row was manually
            # changed to RECONCILE_REQUIRED yet (including truncated history).
            unconfirmed_failed_write = item.status == UnpackItemStatus.EXECUTION_ERROR.value and (
                session.scalar(
                    select(UnpackExternalOperationJournal.id)
                    .where(UnpackExternalOperationJournal.item_id == item.id)
                    .where(UnpackExternalOperationJournal.status == "INTENT_RECORDED")
                    .limit(1)
                )
                is not None
            )
            # Different clients must not both have ADD journals for one item.
            # Count across the full journal history (not the 200 visible rows),
            # and report conflict even if both records claim APPLIED/success.
            distinct_add_clients = session.scalars(
                select(UnpackExternalOperationJournal.operation_type)
                .where(UnpackExternalOperationJournal.item_id == item.id)
                .where(
                    UnpackExternalOperationJournal.operation_type.in_(
                        ("UNPACK_EXEC_QBITTORRENT_ADD", "UNPACK_EXEC_TRANSMISSION_ADD")
                    )
                )
                .distinct()
                .limit(2)
            ).all()
            ambiguous_add_clients = len(distinct_add_clients) > 1
            stage_value = (item.execution_state or {}).get("stage")
            stage = stage_value if isinstance(stage_value, str) else None
            external_risk = item.status == UnpackItemStatus.EXECUTION_ERROR.value and (
                item.last_error_code
                in {
                    "UNPACK_EXEC_MATERIALIZATION_PROOF_MISSING",
                    "UNPACK_EXEC_MATERIALIZATION_TARGET_DRIFT",
                }
                and bool(rows)
            )

            reasons: list[str] = []
            if any_reconcile:
                reasons.append("JOURNAL_RECONCILE_REQUIRED")
            if unconfirmed_failed_write:
                reasons.append("EXTERNAL_WRITE_UNCONFIRMED")
            if external_risk:
                reasons.append("MATERIALIZATION_EVIDENCE_INVALID")
            if ambiguous_add_clients:
                reasons.append("MULTIPLE_DOWNLOADER_ADD_PROOFS")

            def safe_string(value: object, *, length: int = 512) -> str | None:
                return value[:length] if isinstance(value, str) else None

            def safe_int(value: object) -> int | None:
                return value if isinstance(value, int) and not isinstance(value, bool) else None

            def operation(row: UnpackExternalOperationJournal) -> UnpackOperationDiagnosticView:
                target = row.target if isinstance(row.target, dict) else {}
                after = row.after_snapshot if isinstance(row.after_snapshot, dict) else {}
                return UnpackOperationDiagnosticView(
                    operation_type=row.operation_type,
                    status=row.status,
                    last_error_code=row.last_error_code,
                    downloader_id=safe_string(target.get("downloader_id"), length=128),
                    torrent_hash=safe_string(target.get("torrent_hash"), length=80),
                    target_path=safe_string(target.get("path")),
                    last_observed_state=safe_string(after.get("state"), length=80),
                    recorded_file_type=safe_string(after.get("file_type"), length=32),
                    recorded_device=safe_int(after.get("device")),
                    recorded_inode=safe_int(after.get("inode")),
                    recorded_size=safe_int(after.get("size")),
                    updated_at=row.updated_at,
                )

            # Report only the validated, frozen plan's state and enum blocker
            # codes. Never return source paths, torrent content, raw journal
            # intent, download credentials or an assertion of LIVE readiness.
            plan_status = "NOT_CREATED"
            blocked_reasons: tuple[str, ...] = ()
            if item.execution_plan is not None or item.execution_plan_digest is not None:
                plan_status = "INVALID"
                try:
                    plan = unpack_execution_plan_from_payload(item.execution_plan)
                except (TypeError, ValueError):
                    pass
                else:
                    if (
                        plan.plan_digest == item.execution_plan_digest
                        and plan.item_id == item.id
                        and plan.candidate_id == item.selected_candidate_id
                        and plan.candidate_generation == item.candidate_generation
                    ):
                        plan_status = "READY" if plan.ready else "BLOCKED"
                        blocked_reasons = tuple(reason.value for reason in plan.blocked_reasons)
            execution = session.get(UnpackExecution, item.execution_id)
            acceptance_read_only = (
                execution is not None
                and isinstance(execution.config_snapshot, dict)
                and execution.config_snapshot.get("acceptance_mode") == READ_ONLY_ACCEPTANCE_MARKER
            )

            return UnpackItemDiagnosticsView(
                item_id=item.id,
                status=UnpackItemStatus(item.status),
                item_version=item.version,
                execution_stage=stage,
                plan_status=plan_status,
                plan_blocked_reasons=blocked_reasons,
                acceptance_read_only=acceptance_read_only,
                last_error_code=item.last_error_code,
                last_error_message=item.last_error_message,
                requires_manual_reconciliation=bool(reasons),
                reconciliation_reasons=tuple(reasons),
                downloader_state_live_checked=False,
                filesystem_state_live_checked=False,
                operations_truncated=operations_truncated,
                operations=tuple(operation(row) for row in records),
            )

    def list_candidates(self, item_id: str) -> UnpackMatchCandidateList:
        with self._session_factory() as session:
            item = session.get(UnpackExecutionItem, item_id)
            if item is None:
                raise ApplicationError(
                    code="UNPACK_ITEM_NOT_FOUND",
                    status=404,
                    title="数据拆包影片项不存在",
                    detail="未找到指定影片项",
                )
            records = session.scalars(
                select(UnpackMatchCandidate)
                .where(UnpackMatchCandidate.item_id == item.id)
                .where(UnpackMatchCandidate.generation == item.candidate_generation)
            ).all()
            ordered = tuple(
                sorted(
                    records,
                    key=lambda record: (
                        bool(record.evidence.get("hard_conflicts")),
                        -record.score_bps,
                        -(record.seeders or 0),
                        record.site_id,
                        record.candidate_key,
                    ),
                )
            )
            default_candidate_id = item.selected_candidate_id
            if default_candidate_id is None:
                default_candidate_id = next(
                    (record.id for record in ordered if not record.evidence.get("hard_conflicts")),
                    None,
                )
            return UnpackMatchCandidateList(
                item_id=item.id,
                generation=item.candidate_generation,
                item_version=item.version,
                default_candidate_id=default_candidate_id,
                candidates=tuple(self._candidate_view(record) for record in ordered),
            )

    @staticmethod
    def _require_execution(session: Session, execution_id: str) -> UnpackExecution:
        execution = session.get(UnpackExecution, execution_id)
        if execution is None:
            raise ApplicationError(
                code="UNPACK_EXECUTION_NOT_FOUND",
                status=404,
                title="数据拆包执行不存在",
                detail="未找到指定数据拆包执行",
            )
        return execution

    @staticmethod
    def _execution_view(record: UnpackExecution, *, no_match_count: int) -> UnpackExecutionView:
        return UnpackExecutionView(
            id=record.id,
            definition_id=record.definition_id,
            trigger=record.trigger,
            status=UnpackExecutionStatus(record.status),
            discovery_complete=record.discovery_complete,
            discovery_cursor=record.discovery_cursor,
            total_count=record.total_count,
            matched_auto_count=record.matched_auto_count,
            no_match_count=no_match_count,
            review_count=record.review_count,
            content_verified_count=record.content_verified_count,
            content_mismatch_count=record.content_mismatch_count,
            timeout_count=record.timeout_count,
            error_count=record.error_count,
            completed_count=record.completed_count,
            started_at=record.started_at,
            finished_at=record.finished_at,
            version=record.version,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )

    @staticmethod
    def _item_view(
        record: UnpackExecutionItem,
        *,
        review_allowed: bool,
        has_external_operations: bool,
    ) -> UnpackExecutionItemView:
        return UnpackExecutionItemView(
            id=record.id,
            execution_id=record.execution_id,
            source_object_key=record.source_object_key,
            source_snapshot=dict(record.source_snapshot),
            media_identity=dict(record.media_identity),
            status=UnpackItemStatus(record.status),
            selected_candidate_id=record.selected_candidate_id,
            match_origin=record.match_origin,
            candidate_generation=record.candidate_generation,
            retry_count=record.retry_count,
            content_verification_level=record.content_verification_level,
            torrent_metainfo_digest=record.torrent_metainfo_digest,
            auxiliary_state=(
                dict(record.auxiliary_state) if record.auxiliary_state is not None else None
            ),
            review_allowed=review_allowed,
            has_external_operations=has_external_operations,
            last_error_code=record.last_error_code,
            last_error_message=record.last_error_message,
            match_started_at=record.match_started_at,
            match_finished_at=record.match_finished_at,
            version=record.version,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )

    @staticmethod
    def _candidate_view(record: UnpackMatchCandidate) -> UnpackMatchCandidateView:
        return UnpackMatchCandidateView(
            id=record.id,
            item_id=record.item_id,
            generation=record.generation,
            site_id=record.site_id,
            candidate_key=record.candidate_key,
            title=record.title,
            size_bytes=record.size_bytes,
            imdb_id=record.imdb_id,
            douban_id=record.douban_id,
            seeders=record.seeders,
            score_bps=record.score_bps,
            is_exact_match=record.is_exact_match,
            evidence=dict(record.evidence),
            verification_status=record.verification_status,
            verification_level=record.verification_level,
            verification_error_code=record.verification_error_code,
            created_at=record.created_at,
        )

    @staticmethod
    def _invalid(detail: str) -> ApplicationError:
        return ApplicationError(
            code="UNPACK_EXECUTION_QUERY_INVALID",
            status=422,
            title="数据拆包执行查询无效",
            detail=detail,
        )
