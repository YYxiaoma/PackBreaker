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
)
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
            return tuple(self._execution_view(item) for item in records)

    def get(self, execution_id: str) -> UnpackExecutionView:
        with self._session_factory() as session:
            return self._execution_view(self._require_execution(session, execution_id))

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
                    func.lower(UnpackExecutionItem.source_snapshot["path"].as_string()).like(
                        pattern
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
                    )
                    for item in visible
                ),
                next_cursor=visible[-1].id if visible and has_more else None,
                has_more=has_more,
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
    def _execution_view(record: UnpackExecution) -> UnpackExecutionView:
        return UnpackExecutionView(
            id=record.id,
            definition_id=record.definition_id,
            trigger=record.trigger,
            status=UnpackExecutionStatus(record.status),
            discovery_complete=record.discovery_complete,
            discovery_cursor=record.discovery_cursor,
            total_count=record.total_count,
            matched_auto_count=record.matched_auto_count,
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
