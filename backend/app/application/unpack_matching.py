from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import PurePosixPath
from typing import Any, Protocol

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.errors import ApplicationError
from backend.app.application.sites import EnabledSiteAdapter
from backend.app.application.unpack_item_actions import _reset_item_for_match_retry
from backend.app.domain.candidate_scoring import candidate_search_relevant
from backend.app.domain.media_matching import (
    EpisodeIdentity,
    MediaDescriptor,
    MediaFileSummary,
    parse_media_name,
)
from backend.app.domain.site_search import CandidateMeta, SearchQuery
from backend.app.domain.task_units import (
    TaskUnit,
    TaskUnitKind,
    apply_episode_directory_context,
)
from backend.app.domain.unpack import (
    UnpackExecutionStatus,
    UnpackItemStatus,
)
from backend.app.domain.unpack_matching import (
    UnpackCandidateAssessment,
    assess_unpack_candidate,
    build_unpack_search_queries,
)
from backend.app.infrastructure.adapters.site_errors import SiteAdapterError
from backend.app.infrastructure.persistence.database import begin_immediate_write
from backend.app.infrastructure.persistence.models import (
    UnpackDefinition,
    UnpackExecution,
    UnpackExecutionItem,
    UnpackMatchCandidate,
    new_uuid,
    utc_now,
)

_search_diagnostics = logging.getLogger("packbreaker.unpack.search_diagnostics")


def _diagnostic_query_stage(query: SearchQuery, descriptor: MediaDescriptor) -> str:
    """Stable stage labels only: no raw search terms or external IDs in logs."""
    if query.external_ids:
        return "EXTERNAL_ID"
    if any(any("\u4e00" <= ch <= "\u9fff" for ch in token) for token in query.keywords):
        return "CHINESE_TITLE"
    if query.keywords in descriptor.alias_tokens:
        return "ENGLISH_TITLE"
    return "RELEASE_FALLBACK"


class UnpackMatchSiteProvider(Protocol):
    def enabled_adapters(self) -> tuple[EnabledSiteAdapter, ...]: ...


@dataclass(frozen=True, slots=True)
class UnpackMatchReport:
    execution_id: str
    processed_count: int
    matched_auto_count: int
    review_count: int
    no_match_count: int
    timeout_count: int
    error_count: int
    execution_status: UnpackExecutionStatus


@dataclass(frozen=True, slots=True)
class _ClaimedItem:
    item_id: str
    generation: int
    unit: TaskUnit
    site_config_ids: tuple[str, ...]
    auto_match_threshold_bps: int


@dataclass(frozen=True, slots=True)
class _SearchFailure:
    site_config_id: str
    code: str
    retryable: bool


@dataclass(frozen=True, slots=True)
class _FoundCandidate:
    binding: EnabledSiteAdapter
    candidate: CandidateMeta
    assessment: UnpackCandidateAssessment


class UnpackMatchCoordinator:
    """只读搜索站点并持久化候选；不下载 torrent，不产生任何外部写副作用。"""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        site_provider: UnpackMatchSiteProvider,
        *,
        max_site_concurrency: int = 4,
        max_queries_per_site: int = 4,
        site_timeout_seconds: float = 30.0,
        max_candidates_per_item: int = 50,
    ) -> None:
        if max_site_concurrency < 1 or max_site_concurrency > 32:
            raise ValueError("站点并发必须位于 1..32")
        if max_queries_per_site < 1 or max_queries_per_site > 8:
            raise ValueError("单站查询次数必须位于 1..8")
        if site_timeout_seconds <= 0:
            raise ValueError("站点匹配超时必须大于 0")
        if max_candidates_per_item < 1 or max_candidates_per_item > 500:
            raise ValueError("单影片候选上限必须位于 1..500")
        self._session_factory = session_factory
        self._site_provider = site_provider
        self._max_site_concurrency = max_site_concurrency
        self._max_queries_per_site = max_queries_per_site
        self._site_timeout_seconds = site_timeout_seconds
        self._max_candidates_per_item = max_candidates_per_item

    def list_matching_execution_ids(self, *, limit: int = 20) -> tuple[str, ...]:
        if limit < 1 or limit > 500:
            raise self._invalid("待匹配 execution 查询数量必须位于 1 到 500 之间")
        with self._session_factory() as session:
            return tuple(
                session.scalars(
                    select(UnpackExecution.id)
                    .where(UnpackExecution.status == UnpackExecutionStatus.MATCHING.value)
                    .where(
                        select(UnpackExecutionItem.id)
                        .where(UnpackExecutionItem.execution_id == UnpackExecution.id)
                        .where(UnpackExecutionItem.status == UnpackItemStatus.MATCH_PENDING.value)
                        .exists()
                    )
                    .order_by(UnpackExecution.created_at, UnpackExecution.id)
                    .limit(limit)
                ).all()
            )

    async def match_next_batch(
        self,
        execution_id: str,
        *,
        limit: int = 20,
    ) -> UnpackMatchReport:
        if limit < 1 or limit > 200:
            raise self._invalid("匹配批次必须位于 1 到 200 之间")
        processed = 0
        for _ in range(limit):
            claimed = self._claim_next_item(execution_id)
            if claimed is None:
                break
            await self._match_claimed(claimed)
            processed += 1
        return self._report(execution_id, processed_count=processed)

    def _claim_next_item(self, execution_id: str) -> _ClaimedItem | None:
        with self._session_factory() as session:
            begin_immediate_write(session)
            execution = session.get(UnpackExecution, execution_id)
            if execution is None:
                raise self._not_found()
            if execution.status != UnpackExecutionStatus.MATCHING.value:
                return None

            item = session.scalar(
                select(UnpackExecutionItem)
                .where(UnpackExecutionItem.execution_id == execution_id)
                .where(UnpackExecutionItem.status == UnpackItemStatus.MATCH_PENDING.value)
                .where(
                    or_(
                        UnpackExecutionItem.auxiliary_state["auto_retry_not_before"]
                        .as_string()
                        .is_(None),
                        UnpackExecutionItem.auxiliary_state["auto_retry_not_before"].as_string()
                        <= utc_now().isoformat(),
                    )
                )
                .order_by(UnpackExecutionItem.id)
                .limit(1)
            )
            if item is None:
                self._refresh_execution_state(session, execution)
                session.commit()
                return None

            try:
                unit = _task_unit_from_item(item)
            except ApplicationError as exc:
                now = utc_now()
                item.status = UnpackItemStatus.MATCH_ERROR.value
                item.last_error_code = exc.code
                item.last_error_message = exc.detail
                item.match_started_at = now
                item.match_finished_at = now
                item.updated_at = now
                item.version += 1
                session.flush()
                self._refresh_execution_state(session, execution)
                session.commit()
                return None
            snapshot = dict(execution.config_snapshot)
            site_ids = snapshot.get("site_ids")
            matching = snapshot.get("matching")
            if not isinstance(site_ids, list) or any(
                not isinstance(value, str) for value in site_ids
            ):
                raise self._snapshot_invalid("execution 扫描站点快照无效")
            if not isinstance(matching, dict):
                raise self._snapshot_invalid("execution 匹配配置快照无效")
            threshold = matching.get("auto_match_threshold_bps")
            if (
                not isinstance(threshold, int)
                or isinstance(threshold, bool)
                or threshold < 0
                or threshold > 10_000
            ):
                raise self._snapshot_invalid("execution 自动匹配阈值无效")

            generation = item.candidate_generation + 1
            now = utc_now()
            item.status = UnpackItemStatus.MATCHING.value
            item.candidate_generation = generation
            item.media_identity = _descriptor_payload(unit.descriptor)
            item.selected_candidate_id = None
            item.last_error_code = None
            item.last_error_message = None
            item.match_started_at = now
            item.match_finished_at = None
            item.updated_at = now
            item.version += 1
            session.commit()
            return _ClaimedItem(
                item_id=item.id,
                generation=generation,
                unit=unit,
                site_config_ids=tuple(dict.fromkeys(site_ids)),
                auto_match_threshold_bps=threshold,
            )

    async def _match_claimed(self, claimed: _ClaimedItem) -> None:
        try:
            bindings, missing_site_ids = self._selected_bindings(claimed.site_config_ids)
            candidates, failures = await self._search_sites(
                claimed.unit, bindings, item_id=claimed.item_id, generation=claimed.generation
            )
            failures = (
                *failures,
                *(
                    _SearchFailure(
                        site_id,
                        "UNPACK_MATCH_SITE_UNAVAILABLE",
                        False,
                    )
                    for site_id in missing_site_ids
                ),
            )
        except Exception as exc:
            self._finalize_unexpected_error(claimed, exc)
            return
        self._finalize_search(claimed, candidates=candidates, failures=failures)

    def _selected_bindings(
        self,
        selected_ids: tuple[str, ...],
    ) -> tuple[tuple[EnabledSiteAdapter, ...], tuple[str, ...]]:
        by_id = {item.config_id: item for item in self._site_provider.enabled_adapters()}
        bindings = tuple(by_id[site_id] for site_id in selected_ids if site_id in by_id)
        missing = tuple(site_id for site_id in selected_ids if site_id not in by_id)
        return bindings, missing

    async def _search_sites(
        self,
        unit: TaskUnit,
        bindings: tuple[EnabledSiteAdapter, ...],
        *,
        item_id: str,
        generation: int,
    ) -> tuple[tuple[_FoundCandidate, ...], tuple[_SearchFailure, ...]]:
        semaphore = asyncio.Semaphore(self._max_site_concurrency)

        async def search_one(
            binding: EnabledSiteAdapter,
        ) -> tuple[list[_FoundCandidate], list[_SearchFailure]]:
            async with semaphore:
                active_query_index = 0
                active_query_stage = "CAPABILITIES"
                try:
                    async with asyncio.timeout(self._site_timeout_seconds):
                        capabilities = await binding.adapter.capabilities()
                        queries = build_unpack_search_queries(
                            unit.descriptor,
                            unit_kind=unit.kind,
                            capabilities=capabilities,
                            max_queries=self._max_queries_per_site,
                        )
                        # The domain query builder already orders the fallback
                        # tiers: external ID > English title > Chinese title.
                        # Resorting by token count would discard that priority.
                        interval = capabilities.min_request_interval_seconds
                        found: dict[str, _FoundCandidate] = {}
                        failures: list[_SearchFailure] = []
                        previous_page_identity: tuple[int | None, tuple[str, ...]] | None = None
                        for query_index, query in enumerate(queries):
                            if query_index and interval > 0:
                                await asyncio.sleep(interval)
                            query_stage = _diagnostic_query_stage(query, unit.descriptor)
                            active_query_index = query_index + 1
                            active_query_stage = query_stage
                            started = time.perf_counter()
                            try:
                                page = await binding.adapter.search(query)
                            except SiteAdapterError as exc:
                                _search_diagnostics.info(
                                    "影片站点搜索阶段失败",
                                    extra={
                                        "fields": {
                                            "item_id": item_id,
                                            "generation": generation,
                                            "site_kind": binding.site_id,
                                            "query_index": query_index + 1,
                                            "query_stage": query_stage,
                                            "requested_limit": query.page_size,
                                            "outcome": "SITE_ERROR",
                                            "error_code": exc.code,
                                            "duration_ms": round(
                                                (time.perf_counter() - started) * 1000, 2
                                            ),
                                        }
                                    },
                                )
                                failures.append(
                                    _SearchFailure(
                                        binding.config_id,
                                        exc.code,
                                        exc.retryable,
                                    )
                                )
                                # Alternate title keywords cannot fix a
                                # transport error, access denial or a failed
                                # site request. Retry a transient failure only
                                # in a *later* worker pass with the configured
                                # backoff, rather than multiplying requests
                                # within this single search attempt.
                                break
                            if page.site_id != binding.site_id:
                                _search_diagnostics.warning(
                                    "影片站点搜索身份校验失败",
                                    extra={
                                        "fields": {
                                            "item_id": item_id,
                                            "generation": generation,
                                            "site_kind": binding.site_id,
                                            "query_index": query_index + 1,
                                            "query_stage": query_stage,
                                            "requested_limit": query.page_size,
                                            "outcome": "SITE_IDENTITY_MISMATCH",
                                            "returned_count": len(page.items),
                                        }
                                    },
                                )
                                failures.append(
                                    _SearchFailure(
                                        binding.config_id,
                                        "SITE_IDENTITY_MISMATCH",
                                        False,
                                    )
                                )
                                continue
                            identity_filtered = 0
                            title_filtered = 0
                            hard_conflicts = 0
                            selectable = 0
                            new_candidates = 0
                            for candidate in page.items:
                                if candidate.site_id != binding.site_id:
                                    identity_filtered += 1
                                    failures.append(
                                        _SearchFailure(
                                            binding.config_id,
                                            "SITE_IDENTITY_MISMATCH",
                                            False,
                                        )
                                    )
                                    continue
                                if not candidate_search_relevant(
                                    unit.descriptor, candidate.descriptor
                                ):
                                    title_filtered += 1
                                    continue
                                assessment = assess_unpack_candidate(
                                    unit.descriptor, candidate.descriptor
                                )
                                if assessment.rejected:
                                    hard_conflicts += 1
                                else:
                                    selectable += 1
                                if candidate.torrent_id not in found:
                                    new_candidates += 1
                                found.setdefault(
                                    candidate.torrent_id,
                                    _FoundCandidate(binding, candidate, assessment),
                                )
                            _search_diagnostics.info(
                                "影片站点搜索阶段统计",
                                extra={
                                    "fields": {
                                        "item_id": item_id,
                                        "generation": generation,
                                        "site_kind": binding.site_id,
                                        "query_index": query_index + 1,
                                        "query_stage": query_stage,
                                        "requested_limit": query.page_size,
                                        "outcome": "OK",
                                        "returned_count": len(page.items),
                                        "reported_total": page.total_hint,
                                        "has_more": page.has_more,
                                        "identity_filtered_count": identity_filtered,
                                        "title_filtered_count": title_filtered,
                                        "hard_conflict_count": hard_conflicts,
                                        "selectable_count": selectable,
                                        "new_candidate_count": new_candidates,
                                        "duration_ms": round(
                                            (time.perf_counter() - started) * 1000, 2
                                        ),
                                    }
                                },
                            )
                            page_identity = (
                                page.total_hint,
                                tuple(candidate.torrent_id for candidate in page.items),
                            )
                            # A tracker returning the identical broad 100-result
                            # list for *different* English/Chinese keywords is
                            # likely ignoring the query filter. Do not mislabel
                            # this as the movie having no match; stop wasteful
                            # follow-up requests and surface a site query error.
                            if (
                                binding.site_id == "rousi_pro"
                                and previous_page_identity == page_identity
                                and len(page.items) >= 30
                                and page.total_hint is not None
                                and page.total_hint >= 5 * len(page.items)
                                and selectable == 0
                            ):
                                failures.append(
                                    _SearchFailure(
                                        binding.config_id,
                                        "SITE_SEARCH_FILTER_IGNORED_SUSPECTED",
                                        False,
                                    )
                                )
                                _search_diagnostics.warning(
                                    "站点疑似忽略搜索过滤条件",
                                    extra={
                                        "fields": {
                                            "item_id": item_id,
                                            "generation": generation,
                                            "site_kind": binding.site_id,
                                            "query_index": query_index + 1,
                                            "query_stage": query_stage,
                                            "outcome": "QUERY_FILTER_IGNORED_SUSPECTED",
                                            "requested_limit": query.page_size,
                                            "error_code": "SITE_SEARCH_FILTER_IGNORED_SUSPECTED",
                                            "returned_count": len(page.items),
                                            "reported_total": page.total_hint,
                                        }
                                    },
                                )
                                break
                            previous_page_identity = page_identity
                            # A title-related result is not necessarily a
                            # plausible cross-seed candidate: some trackers
                            # return other encodes with very different sizes.
                            # Preserve the Chinese/English fallback unless a
                            # candidate has both matching title evidence and
                            # near-exact source size. The later torrent hash
                            # verification gate is unchanged.
                            if interval > 0 and any(
                                not item.assessment.rejected
                                and item.assessment.evidence["exact"]["title"]
                                and item.assessment.evidence["exact"]["size"]
                                for item in found.values()
                            ):
                                break
                        return list(found.values()), failures
                except TimeoutError:
                    _search_diagnostics.warning(
                        "影片站点搜索阶段超时",
                        extra={
                            "fields": {
                                "item_id": item_id,
                                "generation": generation,
                                "site_kind": binding.site_id,
                                "query_index": active_query_index,
                                "query_stage": active_query_stage,
                                "outcome": "TIMEOUT",
                                "error_code": "UNPACK_MATCH_TIMEOUT",
                            }
                        },
                    )
                    return [], [
                        _SearchFailure(
                            binding.config_id,
                            "UNPACK_MATCH_TIMEOUT",
                            True,
                        )
                    ]
                except SiteAdapterError as exc:
                    _search_diagnostics.warning(
                        "影片站点搜索能力获取失败",
                        extra={
                            "fields": {
                                "item_id": item_id,
                                "generation": generation,
                                "site_kind": binding.site_id,
                                "query_index": active_query_index,
                                "query_stage": active_query_stage,
                                "outcome": "SITE_ERROR",
                                "error_code": exc.code,
                            }
                        },
                    )
                    return [], [
                        _SearchFailure(
                            binding.config_id,
                            exc.code,
                            exc.retryable,
                        )
                    ]

        results = await asyncio.gather(*(search_one(binding) for binding in bindings))
        candidates: list[_FoundCandidate] = []
        failures: list[_SearchFailure] = []
        for found, site_failures in results:
            candidates.extend(found)
            failures.extend(site_failures)
        candidates.sort(
            key=lambda item: (
                item.assessment.rejected,
                -item.assessment.score_bps,
                item.binding.config_id,
                item.candidate.torrent_id,
            )
        )
        return (
            tuple(candidates[: self._max_candidates_per_item]),
            tuple(failures),
        )

    def _finalize_search(
        self,
        claimed: _ClaimedItem,
        *,
        candidates: tuple[_FoundCandidate, ...],
        failures: tuple[_SearchFailure, ...],
    ) -> None:
        with self._session_factory() as session:
            begin_immediate_write(session)
            item = session.get(UnpackExecutionItem, claimed.item_id)
            if item is None:
                raise self._item_not_found()
            if (
                item.status != UnpackItemStatus.MATCHING.value
                or item.candidate_generation != claimed.generation
            ):
                raise self._conflict("匹配结果已过期，拒绝覆盖较新的候选代次")
            execution = session.get(UnpackExecution, item.execution_id)
            if execution is None:
                raise self._not_found()

            session.execute(
                delete(UnpackMatchCandidate)
                .where(UnpackMatchCandidate.item_id == item.id)
                .where(UnpackMatchCandidate.generation == claimed.generation)
            )
            persisted: list[tuple[UnpackMatchCandidate, _FoundCandidate]] = []
            now = utc_now()
            for found in candidates:
                record = UnpackMatchCandidate(
                    id=new_uuid(),
                    item_id=item.id,
                    generation=claimed.generation,
                    site_id=found.binding.config_id,
                    candidate_key=found.candidate.torrent_id,
                    title=found.candidate.display_name,
                    size_bytes=found.candidate.total_size,
                    imdb_id=_external_id(found.candidate.descriptor, "imdb"),
                    douban_id=_external_id(found.candidate.descriptor, "douban"),
                    seeders=found.candidate.seeders,
                    score_bps=found.assessment.score_bps,
                    is_exact_match=found.assessment.is_exact_match,
                    evidence={
                        **found.assessment.evidence,
                        "adapter_site_id": found.binding.site_id,
                        "site_config_version": found.binding.config_version,
                    },
                    verification_status="NOT_CHECKED",
                    raw_ref={
                        "torrent_id": found.candidate.torrent_id,
                        "adapter_site_id": found.binding.site_id,
                        "site_config_id": found.binding.config_id,
                        "site_config_version": found.binding.config_version,
                    },
                    created_at=now,
                )
                session.add(record)
                persisted.append((record, found))
            session.flush()

            selectable = [pair for pair in persisted if not pair[1].assessment.rejected]
            top = selectable[0] if selectable else None
            item.match_finished_at = now
            item.updated_at = now
            item.last_error_code = None
            item.last_error_message = None
            if top is not None:
                top_record, top_found = top
                if top_found.assessment.score_bps >= claimed.auto_match_threshold_bps:
                    item.status = UnpackItemStatus.MATCHED_AUTO.value
                    item.selected_candidate_id = top_record.id
                    item.match_origin = "AUTO"
                else:
                    item.status = UnpackItemStatus.REVIEW_REQUIRED.value
                    item.selected_candidate_id = None
                    item.match_origin = None
            elif failures:
                item.selected_candidate_id = None
                item.match_origin = None
                # Only transient failures may consume automatic retries.
                # A permanent auth/permission/configuration error must stay
                # actionable rather than repeatedly re-requesting the site.
                retryable_failure = any(failure.retryable for failure in failures)
                # Retryability is not synonymous with timeout: HTTP 503 and
                # network unavailability must retain the real site error code
                # after the bounded retry budget is exhausted.
                is_timeout = any(
                    failure.code in {"UNPACK_MATCH_TIMEOUT", "SITE_TIMEOUT"} for failure in failures
                )
                if retryable_failure and self._schedule_automatic_retry(
                    session, execution, item, now
                ):
                    item.last_error_code = None
                    item.last_error_message = None
                elif is_timeout:
                    item.status = UnpackItemStatus.MATCH_TIMEOUT.value
                    item.last_error_code = "UNPACK_MATCH_TIMEOUT"
                    item.last_error_message = "站点匹配超时，自动重试已结束，请人工处理"
                else:
                    item.status = UnpackItemStatus.MATCH_ERROR.value
                    item.last_error_code = failures[0].code
                    item.last_error_message = (
                        "Rousi 搜索接口疑似忽略关键词，重复返回未过滤的种子列表；"
                        "请核对接口参数或 API Key 搜索权限"
                        if any(
                            failure.code == "SITE_SEARCH_FILTER_IGNORED_SUSPECTED"
                            for failure in failures
                        )
                        else "站点匹配失败，自动重试已结束，请人工处理"
                    )
            else:
                item.status = UnpackItemStatus.NO_MATCH.value
                item.selected_candidate_id = None
                item.match_origin = None
                item.last_error_message = (
                    "站点返回了候选，但均存在年份或身份冲突，未找到可选影片"
                    if persisted
                    else "站点搜索完成，未找到符合影片名称的相关候选"
                )
            item.auxiliary_state = {
                **(item.auxiliary_state or {}),
                "search_failures": [
                    {
                        "site_config_id": failure.site_config_id,
                        "code": failure.code,
                        "retryable": failure.retryable,
                    }
                    for failure in failures
                ],
                "candidate_count": len(persisted),
                "selectable_candidate_count": len(selectable),
            }
            item.version += 1
            session.flush()
            self._refresh_execution_state(session, execution)
            session.commit()

    @staticmethod
    def _schedule_automatic_retry(
        session: Session,
        execution: UnpackExecution,
        item: UnpackExecutionItem,
        now: datetime,
    ) -> bool:
        definition = session.get(UnpackDefinition, execution.definition_id)
        if (
            definition is None
            or not definition.retry_enabled
            or item.retry_count >= definition.max_retries
        ):
            return False
        wait_seconds = min(30, 2 ** min(item.retry_count + 1, 4))
        _reset_item_for_match_retry(
            item,
            now,
            automatic=True,
            retry_not_before=now + timedelta(seconds=wait_seconds),
        )
        return True

    def _finalize_unexpected_error(self, claimed: _ClaimedItem, exc: Exception) -> None:
        with self._session_factory() as session:
            begin_immediate_write(session)
            item = session.get(UnpackExecutionItem, claimed.item_id)
            if item is None:
                return
            if (
                item.status != UnpackItemStatus.MATCHING.value
                or item.candidate_generation != claimed.generation
            ):
                return
            execution = session.get(UnpackExecution, item.execution_id)
            if execution is None:
                return
            now = utc_now()
            if not self._schedule_automatic_retry(session, execution, item, now):
                item.status = UnpackItemStatus.MATCH_ERROR.value
                item.last_error_code = "UNPACK_MATCH_ERROR"
                item.last_error_message = "匹配阶段发生内部错误，自动重试已结束，请人工处理"
                item.match_finished_at = now
                item.updated_at = now
                item.version += 1
            item.auxiliary_state = {
                **(item.auxiliary_state or {}),
                "error_type": type(exc).__name__,
            }
            session.flush()
            self._refresh_execution_state(session, execution)
            session.commit()

    @staticmethod
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
            for status in (
                UnpackItemStatus.MATCH_PENDING,
                UnpackItemStatus.MATCHING,
                UnpackItemStatus.MATCHED_AUTO,
                UnpackItemStatus.REVIEW_REQUIRED,
                UnpackItemStatus.NO_MATCH,
                UnpackItemStatus.MATCH_TIMEOUT,
                UnpackItemStatus.MATCH_ERROR,
            )
        }
        execution.matched_auto_count = counts[UnpackItemStatus.MATCHED_AUTO]
        execution.review_count = counts[UnpackItemStatus.REVIEW_REQUIRED]
        execution.timeout_count = counts[UnpackItemStatus.MATCH_TIMEOUT]
        execution.error_count = counts[UnpackItemStatus.MATCH_ERROR]
        now = utc_now()
        if counts[UnpackItemStatus.MATCH_PENDING] or counts[UnpackItemStatus.MATCHING]:
            execution.status = UnpackExecutionStatus.MATCHING.value
            execution.finished_at = None
        elif counts[UnpackItemStatus.REVIEW_REQUIRED]:
            execution.status = UnpackExecutionStatus.REVIEW_REQUIRED.value
            execution.finished_at = None
        elif counts[UnpackItemStatus.MATCHED_AUTO]:
            execution.status = UnpackExecutionStatus.CONTENT_VERIFYING.value
            execution.finished_at = None
        else:
            failed_count = sum(
                counts[status]
                for status in (
                    UnpackItemStatus.NO_MATCH,
                    UnpackItemStatus.MATCH_TIMEOUT,
                    UnpackItemStatus.MATCH_ERROR,
                )
            )
            if execution.total_count > 0 and failed_count == execution.total_count:
                execution.status = UnpackExecutionStatus.FAILED.value
            elif failed_count:
                execution.status = UnpackExecutionStatus.COMPLETED_WITH_ERRORS.value
            else:
                execution.status = UnpackExecutionStatus.COMPLETED.value
            execution.finished_at = now
        execution.updated_at = now
        execution.version += 1

    def _report(self, execution_id: str, *, processed_count: int) -> UnpackMatchReport:
        with self._session_factory() as session:
            execution = session.get(UnpackExecution, execution_id)
            if execution is None:
                raise self._not_found()
            statuses = {
                status: int(
                    session.scalar(
                        select(func.count(UnpackExecutionItem.id))
                        .where(UnpackExecutionItem.execution_id == execution_id)
                        .where(UnpackExecutionItem.status == status.value)
                    )
                    or 0
                )
                for status in (
                    UnpackItemStatus.MATCHED_AUTO,
                    UnpackItemStatus.REVIEW_REQUIRED,
                    UnpackItemStatus.NO_MATCH,
                    UnpackItemStatus.MATCH_TIMEOUT,
                    UnpackItemStatus.MATCH_ERROR,
                )
            }
            return UnpackMatchReport(
                execution_id=execution_id,
                processed_count=processed_count,
                matched_auto_count=statuses[UnpackItemStatus.MATCHED_AUTO],
                review_count=statuses[UnpackItemStatus.REVIEW_REQUIRED],
                no_match_count=statuses[UnpackItemStatus.NO_MATCH],
                timeout_count=statuses[UnpackItemStatus.MATCH_TIMEOUT],
                error_count=statuses[UnpackItemStatus.MATCH_ERROR],
                execution_status=UnpackExecutionStatus(execution.status),
            )

    @staticmethod
    def _invalid(detail: str) -> ApplicationError:
        return ApplicationError(
            code="UNPACK_MATCH_INVALID",
            status=422,
            title="数据拆包匹配参数无效",
            detail=detail,
        )

    @staticmethod
    def _not_found() -> ApplicationError:
        return ApplicationError(
            code="UNPACK_EXECUTION_NOT_FOUND",
            status=404,
            title="数据拆包执行不存在",
            detail="未找到指定数据拆包执行",
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
    def _conflict(detail: str) -> ApplicationError:
        return ApplicationError(
            code="UNPACK_MATCH_CONFLICT",
            status=409,
            title="数据拆包匹配状态冲突",
            detail=detail,
        )

    @staticmethod
    def _snapshot_invalid(detail: str) -> ApplicationError:
        return ApplicationError(
            code="UNPACK_EXECUTION_SNAPSHOT_INVALID",
            status=409,
            title="数据拆包执行快照无效",
            detail=detail,
        )


def _task_unit_from_item(item: UnpackExecutionItem) -> TaskUnit:
    snapshot = dict(item.source_snapshot)
    raw_path = snapshot.get("relative_path") or snapshot.get("path")
    size = snapshot.get("size")
    if not isinstance(raw_path, str) or not raw_path:
        raise ApplicationError(
            code="UNPACK_MEDIA_IDENTITY_INVALID",
            status=409,
            title="影片身份无法解析",
            detail="影片项缺少来源文件名",
        )
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise ApplicationError(
            code="UNPACK_MEDIA_IDENTITY_INVALID",
            status=409,
            title="影片身份无法解析",
            detail="影片项缺少有效文件大小",
        )
    normalized_path = PurePosixPath(raw_path.replace("\\", "/"))
    relative = normalized_path.name
    display_name = PurePosixPath(relative).stem
    aliases: tuple[str, ...] = ()
    # A downloaded film may use the English filename, while its immediate
    # parent is "九品芝麻官.Hail.the.Judge.1994...". Never trust an arbitrary
    # parent/library label: it must end with this exact filename stem.
    full_source_path = snapshot.get("path")
    if isinstance(full_source_path, str):
        parent_name = PurePosixPath(full_source_path.replace(chr(92), "/")).parent.name
        if parent_name.casefold().endswith(display_name.casefold()):
            prefix = parent_name[: -len(display_name)].strip(" ._-")
            if prefix and any("\u4e00" <= char <= "\u9fff" for char in prefix):
                aliases = (prefix,)
    descriptor = parse_media_name(
        display_name,
        aliases=aliases,
        total_size=size,
        files=(MediaFileSummary(relative, size),),
    )
    descriptor = apply_episode_directory_context(
        descriptor,
        display_name=display_name,
        episode_context=normalized_path.parent.as_posix(),
    )
    if not descriptor.title_tokens:
        raise ApplicationError(
            code="UNPACK_MEDIA_IDENTITY_INVALID",
            status=409,
            title="影片身份无法解析",
            detail="来源对象无法解析出有效影片标题",
        )
    return TaskUnit(
        normalized_unit_key=item.source_object_key,
        kind=(TaskUnitKind.EPISODE if descriptor.episode is not None else TaskUnitKind.MOVIE),
        source_relative_path=relative,
        length=size,
        descriptor=descriptor,
    )


def _descriptor_payload(descriptor: MediaDescriptor) -> dict[str, Any]:
    return {
        "raw_name": descriptor.raw_name,
        "title_tokens": list(descriptor.title_tokens),
        "aliases": [list(value) for value in descriptor.alias_tokens],
        "year": descriptor.year,
        "episode": _episode_payload(descriptor.episode),
        "resolution": descriptor.resolution,
        "release_source": descriptor.release_source,
        "codec": descriptor.codec,
        "hdr": descriptor.hdr,
        "audio": descriptor.audio,
        "language": descriptor.language,
        "release_group": descriptor.release_group,
        "version": descriptor.version,
        "external_ids": [
            {"namespace": item.namespace, "value": item.value} for item in descriptor.external_ids
        ],
        "total_size": descriptor.total_size,
        "files": [{"basename": item.basename, "length": item.length} for item in descriptor.files],
    }


def _episode_payload(episode: EpisodeIdentity | None) -> dict[str, Any] | None:
    if episode is None:
        return None
    return {
        "kind": episode.kind.value,
        "season": episode.season,
        "start": episode.start,
        "end": episode.end,
    }


def _external_id(descriptor: MediaDescriptor, namespace: str) -> str | None:
    return next(
        (item.value for item in descriptor.external_ids if item.namespace == namespace),
        None,
    )