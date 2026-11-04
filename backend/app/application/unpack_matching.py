from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from math import ceil, isfinite
from pathlib import PurePosixPath
from typing import Any, Protocol, TypeVar

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.errors import ApplicationError
from backend.app.application.media_metadata import read_adjacent_media_metadata
from backend.app.application.sites import (
    SITE_RISK_CONTROL_ERROR_CODES,
    EnabledSiteAdapter,
    SiteConfigReference,
    SiteRiskControlOutcome,
)
from backend.app.application.unpack_item_actions import _reset_item_for_match_retry
from backend.app.application.unpack_templates import _digest, descriptor_from_payload
from backend.app.domain.candidate_scoring import candidate_search_relevant
from backend.app.domain.errors import DomainViolation
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
    UnpackTaskKind,
)
from backend.app.domain.unpack_matching import (
    UnpackCandidateAssessment,
    _episode_token,
    assess_unpack_candidate,
    build_unpack_search_queries,
)
from backend.app.infrastructure.adapters.site_errors import SiteAdapterError
from backend.app.infrastructure.persistence.database import begin_immediate_write
from backend.app.infrastructure.persistence.models import (
    Site,
    UnpackDefinition,
    UnpackExecution,
    UnpackExecutionItem,
    UnpackMatchCandidate,
    UnpackTemplateMatchAcceptance,
    UnpackTemplateSeed,
    UnpackTemplateVersion,
    UnpackTorrentMetadata,
    new_uuid,
    utc_now,
)
from backend.app.infrastructure.torrent_parser import parse_torrent

MIN_AUTO_MATCH_EVIDENCE_COVERAGE_BPS = 6500
_MAX_TEMPLATE_METADATA_HYDRATIONS_PER_ITEM = 3
_VIDEO_FILE_SUFFIXES = frozenset(
    {
        ".mkv",
        ".mp4",
        ".avi",
        ".m4v",
        ".mov",
        ".wmv",
        ".ts",
        ".m2ts",
        ".mpg",
        ".mpeg",
        ".webm",
        ".flv",
        ".vob",
        ".ogv",
        ".3gp",
        ".rmvb",
    }
)
_search_diagnostics = logging.getLogger("packbreaker.unpack.search_diagnostics")


_ResultT = TypeVar("_ResultT")


async def _await_site_request(
    operation: Awaitable[_ResultT], *, timeout_seconds: float
) -> _ResultT:
    """Bound a single adapter operation, excluding inter-query delay."""
    async with asyncio.timeout(timeout_seconds):
        return await operation


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
    execution_id: str
    item_id: str
    generation: int
    unit: TaskUnit
    site_config_ids: tuple[str, ...]
    auto_match_enabled: bool
    auto_match_threshold_bps: int
    task_kind: UnpackTaskKind = UnpackTaskKind.MANUAL
    template_id: str | None = None
    template_version: int | None = None
    source_root_name: str | None = None
    source_relative_path: str | None = None


@dataclass(frozen=True, slots=True)
class _SearchFailure:
    site_config_id: str
    code: str
    retryable: bool
    retry_after_seconds: float | None = None
    risk_controlled: bool = False
    risk_count: int | None = None
    risk_limit: int | None = None
    risk_retry_interval_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class _FoundCandidate:
    binding: EnabledSiteAdapter | None
    candidate: CandidateMeta
    assessment: UnpackCandidateAssessment
    site_config_id: str | None = None
    adapter_site_id: str | None = None
    site_config_version: int | None = None
    source_mapping: dict[str, Any] | None = None
    template_seed_id: str | None = None
    template_mapping_authoritative: bool = False
    template_mapping_ambiguous: bool = False

    @property
    def local_site_config_id(self) -> str:
        if self.binding is not None:
            return self.binding.config_id
        return self.site_config_id or ""

    @property
    def stable_site_key(self) -> str:
        if self.binding is not None:
            return self.binding.site_id
        return self.adapter_site_id or self.candidate.site_id

    @property
    def local_config_version(self) -> int:
        if self.binding is not None:
            return self.binding.config_version
        return self.site_config_version or 0


class UnpackMatchCoordinator:
    """只读匹配站点与模板；缺少文件级大小证据时可读取 torrent 元信息，但不触发下载器或媒体写入。"""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        site_provider: UnpackMatchSiteProvider,
        *,
        max_site_concurrency: int = 4,
        max_queries_per_site: int = 8,
        site_timeout_seconds: float = 30.0,
        max_candidates_per_item: int = 50,
        max_pages_per_query: int = 3,
        extra_exclude_names: tuple[str, ...] = (),
        pause_after_match: bool = False,
    ) -> None:
        if max_site_concurrency < 1 or max_site_concurrency > 32:
            raise ValueError("站点并发必须位于 1..32")
        if max_queries_per_site < 1 or max_queries_per_site > 8:
            raise ValueError("单站查询次数必须位于 1..8")
        if max_pages_per_query < 1 or max_pages_per_query > 5:
            raise ValueError("单个关键词分页次数必须位于 1..5")
        if site_timeout_seconds <= 0:
            raise ValueError("站点匹配超时必须大于 0")
        if max_candidates_per_item < 1 or max_candidates_per_item > 500:
            raise ValueError("单影片候选上限必须位于 1..500")
        self._session_factory = session_factory
        self._site_provider = site_provider
        self._max_site_concurrency = max_site_concurrency
        self._max_queries_per_site = max_queries_per_site
        self._max_pages_per_query = max_pages_per_query
        self._extra_exclude_names = tuple(
            value.strip().casefold() for value in extra_exclude_names if value.strip()
        )
        self._site_timeout_seconds = site_timeout_seconds
        self._max_candidates_per_item = max_candidates_per_item
        self._pause_after_match = pause_after_match

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

            while True:
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

                snapshot = dict(execution.config_snapshot)
                site_ids = snapshot.get("site_ids")
                matching = snapshot.get("matching")
                raw_task_kind = snapshot.get("task_kind", UnpackTaskKind.MANUAL.value)
                try:
                    task_kind = UnpackTaskKind(str(raw_task_kind))
                except ValueError as exc:
                    raise self._snapshot_invalid("execution 任务类型快照无效") from exc
                template_id = snapshot.get("template_id")
                template_version = snapshot.get("template_version")
                if task_kind is UnpackTaskKind.TEMPLATE and (
                    not isinstance(template_id, str)
                    or not template_id
                    or not isinstance(template_version, int)
                    or isinstance(template_version, bool)
                    or template_version < 1
                ):
                    raise self._snapshot_invalid("模板拆包 execution 缺少模板版本快照")
                if not isinstance(site_ids, list) or any(
                    not isinstance(value, str) for value in site_ids
                ):
                    raise self._snapshot_invalid("execution 扫描站点快照无效")
                if not isinstance(matching, dict):
                    raise self._snapshot_invalid("execution 匹配配置快照无效")
                auto_match_enabled = matching.get("auto_match_enabled", True)
                if not isinstance(auto_match_enabled, bool):
                    raise self._snapshot_invalid("execution 自动匹配开关无效")
                threshold = matching.get("auto_match_threshold_bps")
                if (
                    not isinstance(threshold, int)
                    or isinstance(threshold, bool)
                    or threshold < 0
                    or threshold > 10_000
                ):
                    raise self._snapshot_invalid("execution 自动匹配阈值无效")

                risk_disabled_sites = (
                    list(
                        session.scalars(
                            select(Site)
                            .where(Site.id.in_(site_ids))
                            .where(Site.enabled.is_(False))
                            .where(Site.risk_disabled_at.is_not(None))
                        )
                    )
                    if site_ids and task_kind is not UnpackTaskKind.TEMPLATE
                    else []
                )
                if risk_disabled_sites:
                    cooldown_seconds = max(
                        site.risk_retry_interval_seconds for site in risk_disabled_sites
                    )
                    self._defer_pending_items(
                        session,
                        execution.id,
                        now=utc_now(),
                        not_before=utc_now() + timedelta(seconds=cooldown_seconds),
                        site_config_ids=tuple(site.id for site in risk_disabled_sites),
                    )
                    self._refresh_execution_state(session, execution)
                    session.commit()
                    return None

                source_config = snapshot.get("source_config")
                source_root = (
                    source_config.get("directory_path") if isinstance(source_config, dict) else None
                )
                source_snapshot = dict(item.source_snapshot)
                relative_path = source_snapshot.get("relative_path") or source_snapshot.get("path")
                excluded_pattern = _matching_exclusion_pattern(
                    relative_path,
                    snapshot.get("file_filter"),
                    self._extra_exclude_names,
                )
                if excluded_pattern is not None:
                    now = utc_now()
                    item.status = UnpackItemStatus.CANCELLED.value
                    item.selected_candidate_id = None
                    item.match_origin = None
                    item.last_error_code = "UNPACK_MEDIA_EXCLUDED_BY_FILTER"
                    item.last_error_message = (
                        f"已按附加短片排除规则跳过站点匹配（命中规则：{excluded_pattern}）"
                    )
                    item.match_started_at = now
                    item.match_finished_at = now
                    item.updated_at = now
                    item.auxiliary_state = {
                        "state": "SKIPPED_BY_EXCLUSION",
                        "reason_code": "UNPACK_MEDIA_EXCLUDED_BY_FILTER",
                        "excluded_pattern": excluded_pattern,
                    }
                    item.version += 1
                    _search_diagnostics.info(
                        "影片按名称排除规则跳过匹配",
                        extra={
                            "fields": {
                                "item_id": item.id,
                                "generation": item.candidate_generation,
                                "outcome": "SKIPPED_BY_EXCLUSION",
                                "reason_code": "UNPACK_MEDIA_EXCLUDED_BY_FILTER",
                                "excluded_pattern": excluded_pattern,
                            }
                        },
                    )
                    session.flush()
                    continue

                try:
                    unit = _task_unit_from_item(
                        item,
                        source_root=source_root if isinstance(source_root, str) else None,
                    )
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
                    execution_id=execution.id,
                    item_id=item.id,
                    generation=generation,
                    unit=unit,
                    site_config_ids=tuple(dict.fromkeys(site_ids)),
                    auto_match_enabled=auto_match_enabled,
                    auto_match_threshold_bps=threshold,
                    task_kind=task_kind,
                    template_id=template_id if isinstance(template_id, str) else None,
                    template_version=template_version
                    if isinstance(template_version, int)
                    else None,
                    source_root_name=(
                        PurePosixPath(source_root).name
                        if isinstance(source_root, str) and source_root.strip()
                        else None
                    ),
                    source_relative_path=(
                        str(source_snapshot.get("relative_path"))
                        if isinstance(source_snapshot.get("relative_path"), str)
                        else None
                    ),
                )

    @staticmethod
    def _defer_pending_items(
        session: Session,
        execution_id: str,
        *,
        now: datetime,
        not_before: datetime,
        site_config_ids: tuple[str, ...],
        risk_count: int | None = None,
        risk_limit: int | None = None,
        requires_manual_reenable: bool = True,
    ) -> None:
        """Keep the execution parked while a required site is auto-disabled."""
        items = session.scalars(
            select(UnpackExecutionItem)
            .where(UnpackExecutionItem.execution_id == execution_id)
            .where(UnpackExecutionItem.status == UnpackItemStatus.MATCH_PENDING.value)
        ).all()
        for pending in items:
            state = dict(pending.auxiliary_state or {})
            effective_not_before = not_before
            previous = state.get("auto_retry_not_before")
            if isinstance(previous, str):
                try:
                    parsed = datetime.fromisoformat(previous)
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=UTC)
                    if parsed > effective_not_before:
                        effective_not_before = parsed
                except ValueError:
                    pass
            state["auto_retry_not_before"] = effective_not_before.isoformat()
            state["risk_control_wait"] = {
                "site_config_ids": list(site_config_ids),
                "not_before": effective_not_before.isoformat(),
                "requires_manual_reenable": requires_manual_reenable,
                "risk_count": risk_count,
                "risk_limit": risk_limit,
            }
            pending.auxiliary_state = state
            pending.updated_at = now
            pending.version += 1

    async def _match_claimed(self, claimed: _ClaimedItem) -> None:
        candidates: tuple[_FoundCandidate, ...]
        failures: tuple[_SearchFailure, ...]
        try:
            if claimed.task_kind is UnpackTaskKind.TEMPLATE:
                if self._reuse_accepted_template_match(claimed):
                    return
                candidates, failures = await self._search_template(claimed)
            else:
                if claimed.site_config_ids:
                    bindings, missing_site_ids = self._selected_bindings(
                        claimed.site_config_ids, execution_id=claimed.execution_id
                    )
                else:
                    # Do not load/decrypt any unrelated enabled site for an empty
                    # or tampered historical execution snapshot.
                    bindings, missing_site_ids = (), ()
                if not claimed.site_config_ids or missing_site_ids:
                    # A saved execution cannot silently run against fewer sites
                    # after one is disabled, removed, or fails Registry checks.
                    candidates = ()
                    failures = tuple(
                        _SearchFailure(site_id, "UNPACK_MATCH_SITE_UNAVAILABLE", False)
                        for site_id in (missing_site_ids or ("",))
                    )
                else:
                    candidates, failures = await self._search_sites(
                        claimed.unit,
                        bindings,
                        execution_id=claimed.execution_id,
                        item_id=claimed.item_id,
                        generation=claimed.generation,
                    )
        except Exception as exc:
            self._finalize_unexpected_error(claimed, exc)
            return
        self._finalize_search(claimed, candidates=candidates, failures=failures)

    def _reuse_accepted_template_match(self, claimed: _ClaimedItem) -> bool:
        """Reuse a user-accepted 100% template match without querying the site again.

        The cache is intentionally distinct from torrent metainfo verification: it
        preserves the prior match decision, while candidate verification stays in
        its original state. Reuse is limited to the same template/site/path and an
        unchanged regular file identity, and the accepted torrent must still exist
        in the execution's pinned template version.
        """
        if (
            claimed.template_id is None
            or claimed.template_version is None
            or len(claimed.site_config_ids) != 1
            or not claimed.source_root_name
            or not isinstance(claimed.source_relative_path, str)
        ):
            return False

        normalized_path = claimed.source_relative_path.replace("\\", "/")
        relative_path = PurePosixPath(normalized_path)
        if relative_path.is_absolute() or any(
            part in {"", ".", ".."} for part in relative_path.parts
        ):
            return False

        with self._session_factory() as session:
            begin_immediate_write(session)
            item = session.get(UnpackExecutionItem, claimed.item_id)
            execution = session.get(UnpackExecution, claimed.execution_id)
            if (
                item is None
                or execution is None
                or item.execution_id != claimed.execution_id
                or item.status != UnpackItemStatus.MATCHING.value
                or item.candidate_generation != claimed.generation
            ):
                return False

            source_snapshot = dict(item.source_snapshot or {})
            if (
                source_snapshot.get("relative_path") != normalized_path
                or source_snapshot.get("file_type") != "regular"
                or source_snapshot.get("size") != claimed.unit.descriptor.total_size
            ):
                return False

            acceptance = session.scalar(
                select(UnpackTemplateMatchAcceptance)
                .where(UnpackTemplateMatchAcceptance.template_id == claimed.template_id)
                .where(UnpackTemplateMatchAcceptance.site_config_id == claimed.site_config_ids[0])
                .where(
                    UnpackTemplateMatchAcceptance.collection_root_name == claimed.source_root_name
                )
                .where(UnpackTemplateMatchAcceptance.source_relative_path == normalized_path)
            )
            if acceptance is None:
                return False
            site_config = session.get(Site, acceptance.site_config_id)
            if (
                site_config is None
                or not site_config.enabled
                or str(site_config.type).casefold() != acceptance.site_key.casefold()
            ):
                return False

            source_device = source_snapshot.get("device")
            source_inode = source_snapshot.get("inode")
            source_size = source_snapshot.get("size")
            source_mtime_ns = source_snapshot.get("mtime_ns")
            if (
                not isinstance(source_device, int)
                or isinstance(source_device, bool)
                or not isinstance(source_inode, int)
                or isinstance(source_inode, bool)
                or not isinstance(source_size, int)
                or isinstance(source_size, bool)
                or not isinstance(source_mtime_ns, str)
                or source_device != acceptance.source_device
                or source_inode != acceptance.source_inode
                or source_size != acceptance.source_size_bytes
                or source_mtime_ns != acceptance.source_mtime_ns
                or acceptance.accepted_score_bps != 10_000
                or acceptance.acceptance_kind != "USER_CONFIRMED_PREVIOUS_100_PERCENT"
            ):
                return False

            version = session.scalar(
                select(UnpackTemplateVersion)
                .where(UnpackTemplateVersion.template_id == claimed.template_id)
                .where(UnpackTemplateVersion.version == claimed.template_version)
            )
            if (
                version is None
                or version.status != "READY"
                or acceptance.site_key not in (version.site_keys or [])
            ):
                return False
            current_seed_id = session.scalar(
                select(UnpackTemplateSeed.id)
                .join(
                    UnpackTorrentMetadata,
                    UnpackTorrentMetadata.id == UnpackTemplateSeed.metadata_id,
                )
                .where(UnpackTemplateSeed.template_version_id == version.id)
                .where(UnpackTorrentMetadata.site_key == acceptance.site_key)
                .where(UnpackTorrentMetadata.remote_torrent_id == acceptance.candidate_remote_id)
                .limit(1)
            )
            if current_seed_id is None:
                return False

            candidate_snapshot = dict(acceptance.candidate_snapshot or {})
            evidence = candidate_snapshot.get("evidence")
            raw_ref = candidate_snapshot.get("raw_ref")
            numeric_fields = ("size_bytes", "seeders")
            string_fields = ("imdb_id", "douban_id", "verification_error_code", "metainfo_digest")
            if (
                candidate_snapshot.get("candidate_key") != acceptance.candidate_remote_id
                or candidate_snapshot.get("score_bps") != 10_000
                or candidate_snapshot.get("title") != acceptance.candidate_title
                or not isinstance(candidate_snapshot.get("is_exact_match"), bool)
                or not isinstance(evidence, dict)
                or not isinstance(raw_ref, dict)
                or any(
                    candidate_snapshot.get(name) is not None
                    and (
                        not isinstance(candidate_snapshot.get(name), int)
                        or isinstance(candidate_snapshot.get(name), bool)
                    )
                    for name in numeric_fields
                )
                or any(
                    candidate_snapshot.get(name) is not None
                    and not isinstance(candidate_snapshot.get(name), str)
                    for name in string_fields
                )
            ):
                return False

            now = utc_now()
            accepted_at = acceptance.accepted_at.isoformat()
            reuse_details = {
                "acceptance_id": acceptance.id,
                "acceptance_kind": acceptance.acceptance_kind,
                "accepted_at": accepted_at,
                "accepted_score_bps": acceptance.accepted_score_bps,
                "source_execution_id": acceptance.source_execution_id,
                "source_item_id": acceptance.source_item_id,
                "source_candidate_id": acceptance.source_candidate_id,
                "source_template_version": acceptance.source_template_version,
            }
            copied_evidence = dict(evidence)
            source_mapping = copied_evidence.get("source_mapping")
            if isinstance(source_mapping, dict):
                copied_evidence["source_mapping"] = {
                    **source_mapping,
                    "mapping_status": "USER_ACCEPTED_PREVIOUS_AUTO_MATCH",
                    "match_mode": "REUSED_ACCEPTED_MATCH",
                    "accepted_match_id": acceptance.id,
                }
            copied_evidence["accepted_match_reuse"] = reuse_details
            copied_raw_ref = {
                **raw_ref,
                "torrent_id": acceptance.candidate_remote_id,
                "site_config_id": acceptance.site_config_id,
                "adapter_site_id": acceptance.site_key,
                "accepted_match_id": acceptance.id,
            }
            verification_status = candidate_snapshot.get("verification_status")
            if verification_status not in {
                "NOT_CHECKED",
                "VERIFYING",
                "VERIFIED",
                "MISMATCH",
                "UNAVAILABLE",
            }:
                verification_status = "NOT_CHECKED"
            verification_level = candidate_snapshot.get("verification_level")
            if verification_level not in {
                None,
                "FULL_VERIFIED",
                "CLIENT_CHECK_REQUIRED",
                "BLOCKED",
            }:
                verification_level = None

            session.execute(
                delete(UnpackMatchCandidate)
                .where(UnpackMatchCandidate.item_id == item.id)
                .where(UnpackMatchCandidate.generation == claimed.generation)
            )
            reused_candidate = UnpackMatchCandidate(
                id=new_uuid(),
                item_id=item.id,
                generation=claimed.generation,
                site_id=acceptance.site_config_id,
                candidate_key=acceptance.candidate_remote_id,
                title=acceptance.candidate_title,
                size_bytes=candidate_snapshot.get("size_bytes"),
                imdb_id=candidate_snapshot.get("imdb_id"),
                douban_id=candidate_snapshot.get("douban_id"),
                seeders=candidate_snapshot.get("seeders"),
                score_bps=acceptance.accepted_score_bps,
                is_exact_match=bool(candidate_snapshot.get("is_exact_match")),
                evidence=copied_evidence,
                verification_status=verification_status,
                verification_level=verification_level,
                verification_error_code=candidate_snapshot.get("verification_error_code"),
                metainfo_digest=candidate_snapshot.get("metainfo_digest"),
                raw_ref=copied_raw_ref,
                created_at=now,
            )
            session.add(reused_candidate)
            item.status = UnpackItemStatus.MATCHED_AUTO.value
            item.selected_candidate_id = reused_candidate.id
            item.match_origin = "AUTO"
            item.last_error_code = None
            item.last_error_message = None
            item.match_finished_at = now
            item.updated_at = now
            item.auxiliary_state = {
                **(item.auxiliary_state or {}),
                "accepted_match_reuse": reuse_details,
            }
            item.version += 1
            session.flush()
            self._refresh_execution_state(session, execution)
            session.commit()
            _search_diagnostics.info(
                "复用已接受的模板匹配结果",
                extra={
                    "fields": {
                        "item_id": item.id,
                        "generation": claimed.generation,
                        "acceptance_id": acceptance.id,
                        "source_execution_id": acceptance.source_execution_id,
                        "candidate_id": acceptance.candidate_remote_id,
                        "outcome": "REUSED_ACCEPTED_MATCH",
                    }
                },
            )
            return True

    async def _search_template(
        self,
        claimed: _ClaimedItem,
        *,
        _allow_metadata_hydration: bool = True,
    ) -> tuple[tuple[_FoundCandidate, ...], tuple[_SearchFailure, ...]]:
        if claimed.template_id is None or claimed.template_version is None:
            return (), (_SearchFailure("", "UNPACK_TEMPLATE_NOT_FOUND", False),)
        with self._session_factory() as session:
            version = session.scalar(
                select(UnpackTemplateVersion)
                .where(UnpackTemplateVersion.template_id == claimed.template_id)
                .where(UnpackTemplateVersion.version == claimed.template_version)
            )
            if version is None or version.status != "READY":
                return (), (_SearchFailure("", "UNPACK_TEMPLATE_NOT_READY", False),)
            bound_site_keys = tuple(
                sorted(item for item in (version.site_keys or []) if isinstance(item, str) and item)
            )
            if len(bound_site_keys) != 1 or len(claimed.site_config_ids) != 1:
                return (), (_SearchFailure("", "UNPACK_TEMPLATE_SITE_BINDING_INVALID", False),)
            bound_site_key = bound_site_keys[0]
            rows = session.execute(
                select(UnpackTemplateSeed, UnpackTorrentMetadata)
                .join(
                    UnpackTorrentMetadata,
                    UnpackTorrentMetadata.id == UnpackTemplateSeed.metadata_id,
                )
                .where(UnpackTemplateSeed.template_version_id == version.id)
                .order_by(UnpackTemplateSeed.sort_order, UnpackTemplateSeed.id)
            ).all()

        config_references_for_site_ids = getattr(
            self._site_provider, "config_references_for_site_ids", None
        )
        if not callable(config_references_for_site_ids):
            return (), (_SearchFailure("", "UNPACK_TEMPLATE_SITE_BINDING_INVALID", False),)
        references = config_references_for_site_ids(claimed.site_config_ids)
        by_site_config = {item.config_id: item for item in references}
        missing = tuple(
            site_id
            for site_id in claimed.site_config_ids
            if site_id not in by_site_config or not by_site_config[site_id].enabled
        )
        failures: list[_SearchFailure] = [
            _SearchFailure(site_id, "UNPACK_MATCH_SITE_UNAVAILABLE", False) for site_id in missing
        ]
        active_refs = tuple(
            by_site_config[site_id]
            for site_id in claimed.site_config_ids
            if site_id in by_site_config and by_site_config[site_id].enabled
        )
        if active_refs and any(reference.site_id != bound_site_key for reference in active_refs):
            return (), (_SearchFailure("", "UNPACK_TEMPLATE_SITE_BINDING_INVALID", False),)
        if any(metadata.site_key != bound_site_key for _seed, metadata in rows):
            return (), (_SearchFailure("", "UNPACK_TEMPLATE_SITE_BINDING_INVALID", False),)
        if not rows:
            return (), tuple(failures or [_SearchFailure("", "UNPACK_TEMPLATE_EMPTY", False)])

        found: dict[tuple[str, str], _FoundCandidate] = {}
        hydrated_any = False
        hydrated_seed_ids: set[str] = set()
        hydration_count = 0
        relative_directory = _mapped_source_directory(claimed.source_relative_path)
        source_mappings_by_seed = {
            seed.id: _matching_seed_source_mapping(
                seed.mapping_rules,
                collection_root_name=claimed.source_root_name,
                relative_directory=relative_directory,
                source_relative_path=claimed.source_relative_path,
            )
            for seed, _metadata in rows
        }
        mapped_seed_ids = {
            seed_id for seed_id, mapping in source_mappings_by_seed.items() if mapping is not None
        }
        unique_mapped_seed_id = next(iter(mapped_seed_ids)) if len(mapped_seed_ids) == 1 else None
        ambiguous_template_mapping = len(mapped_seed_ids) > 1
        exact_file_match = _unique_template_file_match(
            tuple((seed.id, metadata.file_summary) for seed, metadata in rows),
            source_relative_path=claimed.source_relative_path,
            source_size=claimed.unit.descriptor.total_size,
        )
        # A stale directory mapping must not block a stronger, unique file-level
        # identity. If another seed has a competing directory mapping, fail
        # closed rather than silently preferring one torrent over another.
        allow_file_identity_fallback = exact_file_match is not None and (
            not mapped_seed_ids or mapped_seed_ids == {exact_file_match[0]}
        )
        for seed, metadata in rows:
            source_mapping = source_mappings_by_seed.get(seed.id)
            has_matching_source_directory_mapping = _has_matching_seed_directory_mapping(
                seed.mapping_rules,
                collection_root_name=claimed.source_root_name,
                relative_directory=relative_directory,
                source_relative_path=claimed.source_relative_path,
            )
            if (
                allow_file_identity_fallback
                and exact_file_match is not None
                and seed.id == exact_file_match[0]
                and isinstance(claimed.source_relative_path, str)
            ):
                source_path = PurePosixPath(claimed.source_relative_path.replace("\\", "/"))
                configured_mapping = source_mapping
                source_mapping = {
                    **(configured_mapping or {}),
                    "collection_root_name": claimed.source_root_name,
                    "relative_directory": relative_directory,
                    "relative_file_name": source_path.name,
                    "media_title": source_path.stem,
                    "confidence_bps": 10_000,
                    "mapping_status": "EXACT_FILE_METADATA",
                    "match_mode": "UNIQUE_FILE_NAME_AND_SIZE",
                }
                if configured_mapping is not None:
                    source_mapping["configured_media_title"] = configured_mapping.get("media_title")
                    source_mapping["configured_relative_directory"] = configured_mapping.get(
                        "relative_directory"
                    )
                    source_mapping["configured_relative_file_name"] = configured_mapping.get(
                        "relative_file_name"
                    )
            for reference in active_refs:
                if reference.site_id != metadata.site_key:
                    continue
                metadata_descriptor_payload = dict(metadata.descriptor or {})
                metadata_total_size = metadata.total_size
                metadata_file_summary = [
                    dict(item) for item in (metadata.file_summary or []) if isinstance(item, dict)
                ]
                has_metainfo_paths = any(
                    isinstance(item.get("path"), str) and item["path"]
                    for item in metadata_file_summary
                )
                descriptor = descriptor_from_payload(
                    metadata_descriptor_payload,
                    display_name=metadata.display_name,
                    total_size=metadata_total_size,
                )
                if (
                    not _template_candidate_relevant_for_source(
                        claimed.unit.descriptor, descriptor, metadata.display_name
                    )
                    and source_mapping is None
                    and not has_matching_source_directory_mapping
                ):
                    continue
                assessment = assess_unpack_candidate(claimed.unit.descriptor, descriptor)
                if source_mapping is not None:
                    confidence = source_mapping.get("confidence_bps", 0)
                    confidence_bps = (
                        confidence
                        if isinstance(confidence, int) and not isinstance(confidence, bool)
                        else 0
                    )
                    mapped_file: dict[str, Any] | None
                    if (
                        allow_file_identity_fallback
                        and exact_file_match is not None
                        and seed.id == exact_file_match[0]
                    ):
                        mapped_file = {
                            **exact_file_match[1],
                            "match_mode": "UNIQUE_FILE_NAME_AND_SIZE",
                        }
                    else:
                        mapped_file = _mapped_torrent_file_size(
                            metadata_file_summary,
                            claimed.source_relative_path,
                            source_mapping,
                        )
                    mapped_descriptor = _mapped_template_descriptor(
                        str(source_mapping["media_title"]),
                        candidate_descriptor=descriptor,
                        source_descriptor=claimed.unit.descriptor,
                        candidate_video_file_count=len(metadata_file_summary),
                    )
                    mapped_assessment = assess_unpack_candidate(
                        claimed.unit.descriptor, mapped_descriptor
                    )
                    hydration_error: str | None = None
                    should_hydrate = (
                        _allow_metadata_hydration
                        and hydration_count < _MAX_TEMPLATE_METADATA_HYDRATIONS_PER_ITEM
                        and str(metadata.id) not in hydrated_seed_ids
                        and _should_hydrate_mapped_template_candidate(
                            source_descriptor=claimed.unit.descriptor,
                            mapped_descriptor=mapped_descriptor,
                            source_size=claimed.unit.descriptor.total_size,
                            has_metainfo_paths=has_metainfo_paths,
                            has_mapped_file=mapped_file is not None,
                            confidence_bps=confidence_bps,
                            assessment=mapped_assessment,
                        )
                    )
                    if should_hydrate:
                        hydrated_seed_ids.add(str(metadata.id))
                        hydration_count += 1
                        hydrated, hydration_error = await self._hydrate_template_metadata(
                            reference, metadata
                        )
                        if hydrated is not None:
                            hydrated_any = True
                            metadata_descriptor_payload = hydrated["descriptor"]
                            metadata_total_size = hydrated["total_size"]
                            metadata_file_summary = hydrated["file_summary"]
                            descriptor = descriptor_from_payload(
                                metadata_descriptor_payload,
                                display_name=metadata.display_name,
                                total_size=metadata_total_size,
                            )
                            assessment = assess_unpack_candidate(
                                claimed.unit.descriptor, descriptor
                            )
                            mapped_file = _mapped_torrent_file_size(
                                metadata_file_summary,
                                claimed.source_relative_path,
                                source_mapping,
                            )

                    if mapped_file is not None:
                        mapped_file_size = int(mapped_file["length"])
                        mapped_file_summary = MediaFileSummary(
                            str(mapped_file["basename"]), mapped_file_size
                        )
                        file_level_descriptor = replace(
                            descriptor,
                            total_size=mapped_file_size,
                            files=(mapped_file_summary,),
                        )
                        file_level_assessment = assess_unpack_candidate(
                            claimed.unit.descriptor, file_level_descriptor
                        )
                        mapped_descriptor = _mapped_template_descriptor(
                            str(source_mapping["media_title"]),
                            candidate_descriptor=descriptor,
                            source_descriptor=claimed.unit.descriptor,
                            candidate_video_file_count=len(metadata_file_summary),
                            total_size=mapped_file_size,
                            files=(mapped_file_summary,),
                        )
                        mapped_assessment = assess_unpack_candidate(
                            claimed.unit.descriptor, mapped_descriptor
                        )
                        if (
                            file_level_assessment.rejected
                            or not candidate_search_relevant(claimed.unit.descriptor, descriptor)
                            or (
                                confidence_bps >= 9_000
                                and (
                                    mapped_assessment.score_bps > file_level_assessment.score_bps
                                    or (
                                        mapped_assessment.score_bps
                                        == file_level_assessment.score_bps
                                        and int(
                                            mapped_assessment.evidence.get(
                                                "evidence_coverage_bps", 0
                                            )
                                        )
                                        > int(
                                            file_level_assessment.evidence.get(
                                                "evidence_coverage_bps", 0
                                            )
                                        )
                                    )
                                )
                            )
                        ):
                            assessment = mapped_assessment
                        else:
                            assessment = file_level_assessment
                        source_mapping = {
                            **source_mapping,
                            "size_comparison": {
                                "status": "MATCHED_FILE",
                                "mode": mapped_file.get("match_mode", "UNIQUE_BASENAME"),
                                "source_size_bytes": claimed.unit.descriptor.total_size,
                                "candidate_file_size_bytes": mapped_file_size,
                                "candidate_file_path": mapped_file.get("path"),
                            },
                        }
                    else:
                        # A collection's total torrent size is not the size of one movie.
                        # Without a unique file-level match, keep size/file-list evidence
                        # unavailable and let the evidence-coverage gate require review.
                        size_unknown_descriptor = replace(descriptor, total_size=None, files=())
                        size_unknown_assessment = assess_unpack_candidate(
                            claimed.unit.descriptor, size_unknown_descriptor
                        )
                        if (
                            size_unknown_assessment.rejected
                            or not candidate_search_relevant(claimed.unit.descriptor, descriptor)
                            or (
                                confidence_bps >= 9_000
                                and (
                                    mapped_assessment.score_bps > size_unknown_assessment.score_bps
                                    or (
                                        mapped_assessment.score_bps
                                        == size_unknown_assessment.score_bps
                                        and int(
                                            mapped_assessment.evidence.get(
                                                "evidence_coverage_bps", 0
                                            )
                                        )
                                        > int(
                                            size_unknown_assessment.evidence.get(
                                                "evidence_coverage_bps", 0
                                            )
                                        )
                                    )
                                )
                            )
                        ):
                            assessment = mapped_assessment
                        else:
                            assessment = size_unknown_assessment
                        source_mapping = {
                            **source_mapping,
                            "size_comparison": {
                                "status": "UNAVAILABLE",
                                "reason_code": hydration_error or "MAPPED_TORRENT_FILE_NOT_FOUND",
                                "source_size_bytes": claimed.unit.descriptor.total_size,
                            },
                        }
                elif (
                    _allow_metadata_hydration
                    and hydration_count < _MAX_TEMPLATE_METADATA_HYDRATIONS_PER_ITEM
                    and str(metadata.id) not in hydrated_seed_ids
                    and _should_hydrate_unmapped_template_candidate(
                        source_descriptor=claimed.unit.descriptor,
                        candidate_descriptor=descriptor,
                        candidate_title=metadata.display_name,
                        source_size=claimed.unit.descriptor.total_size,
                        has_metainfo_paths=has_metainfo_paths,
                        has_matching_source_directory_mapping=has_matching_source_directory_mapping,
                        assessment=assessment,
                    )
                ):
                    hydrated_seed_ids.add(str(metadata.id))
                    hydration_count += 1
                    hydrated, hydration_error = await self._hydrate_template_metadata(
                        reference, metadata
                    )
                    if hydrated is not None:
                        hydrated_any = True
                    elif hydration_error:
                        _search_diagnostics.warning(
                            "template seed metadata hydration failed",
                            extra={
                                "execution_id": claimed.execution_id,
                                "item_id": claimed.item_id,
                                "site_id": reference.site_id,
                                "torrent_id": metadata.remote_torrent_id,
                                "reason_code": hydration_error,
                            },
                        )
                candidate = CandidateMeta(
                    site_id=metadata.site_key,
                    torrent_id=metadata.remote_torrent_id,
                    display_name=metadata.display_name,
                    descriptor=descriptor,
                    total_size=metadata_total_size,
                    file_summary=tuple(
                        MediaFileSummary(str(item["basename"]), int(item["length"]))
                        for item in metadata_file_summary
                        if isinstance(item.get("basename"), str)
                        and isinstance(item.get("length"), int)
                        and not isinstance(item.get("length"), bool)
                        and item.get("length", -1) >= 0
                    ),
                )
                found[(reference.config_id, candidate.torrent_id)] = _FoundCandidate(
                    None,
                    candidate,
                    assessment,
                    site_config_id=reference.config_id,
                    adapter_site_id=reference.site_id,
                    site_config_version=reference.config_version,
                    source_mapping=dict(source_mapping) if source_mapping is not None else None,
                    template_seed_id=seed.id,
                    template_mapping_authoritative=seed.id == unique_mapped_seed_id,
                    template_mapping_ambiguous=(
                        ambiguous_template_mapping and seed.id in mapped_seed_ids
                    ),
                )
        if hydrated_any and _allow_metadata_hydration:
            return await self._search_template(claimed, _allow_metadata_hydration=False)
        candidates = sorted(
            found.values(),
            key=lambda item: (
                not (item.template_mapping_authoritative or item.template_mapping_ambiguous),
                item.assessment.rejected,
                item.source_mapping is None,
                -item.assessment.score_bps,
                item.local_site_config_id,
                item.candidate.torrent_id,
            ),
        )
        return tuple(candidates[: self._max_candidates_per_item]), tuple(failures)

    async def _hydrate_template_metadata(
        self,
        reference: SiteConfigReference,
        metadata: UnpackTorrentMetadata,
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Read one mapped seed's metainfo only when file-size evidence is missing.

        The torrent payload is never added to a downloader or written to media storage.
        Only safe metadata (video paths, sizes and info hash) is cached for later matching.
        """
        adapter_factory = getattr(self._site_provider, "adapter_for_config", None)
        if not callable(adapter_factory):
            return None, "SITE_ADAPTER_UNAVAILABLE"
        try:
            binding = adapter_factory(reference.config_id)
        except Exception:
            return None, "SITE_ADAPTER_UNAVAILABLE"
        if (
            binding.config_id != reference.config_id
            or binding.config_version != reference.config_version
            or binding.site_id != reference.site_id
            or binding.site_id != metadata.site_key
        ):
            return None, "SITE_CONFIG_CHANGED"

        timeout_seconds = (
            max(15.0, float(binding.request_timeout_seconds))
            + float(binding.search_interval_seconds)
            + 1.0
        )
        try:
            payload = await _await_site_request(
                binding.adapter.fetch_torrent(metadata.remote_torrent_id),
                timeout_seconds=timeout_seconds,
            )
            if (
                payload.site_id != binding.site_id
                or payload.torrent_id != metadata.remote_torrent_id
            ):
                return None, "TORRENT_IDENTITY_MISMATCH"
            torrent_meta = parse_torrent(payload.content)
        except SiteAdapterError as exc:
            return None, exc.code
        except DomainViolation as exc:
            return None, exc.code.value
        except TimeoutError:
            return None, "SITE_TIMEOUT"
        except (TypeError, ValueError):
            return None, "TORRENT_METADATA_INVALID"

        parsed_hashes = {
            value.casefold()
            for value in (torrent_meta.v1_info_hash, torrent_meta.v2_info_hash)
            if isinstance(value, str) and value
        }
        if metadata.info_hash and metadata.info_hash.casefold() not in parsed_hashes:
            return None, "TORRENT_INFO_HASH_MISMATCH"

        file_summary = [
            {
                "basename": PurePosixPath(file.path).name,
                "length": file.length,
                "path": PurePosixPath(file.path).as_posix(),
            }
            for file in torrent_meta.files
            if not file.padding
            and not file.zero_length
            and PurePosixPath(file.path).suffix.casefold() in _VIDEO_FILE_SUFFIXES
        ]
        if not file_summary:
            return None, "TORRENT_VIDEO_FILE_LIST_EMPTY"
        total_size = sum(file.length for file in torrent_meta.files)
        descriptor_payload = dict(metadata.descriptor or {})
        descriptor_payload["total_size"] = total_size
        descriptor_payload["files"] = [
            {"basename": item["basename"], "length": item["length"]} for item in file_summary
        ]
        external_ids = list(metadata.external_ids or [])
        metadata_payload = {
            "site_key": metadata.site_key,
            "remote_torrent_id": metadata.remote_torrent_id,
            "display_name": metadata.display_name,
            "descriptor": descriptor_payload,
            "total_size": total_size,
            "file_summary": file_summary,
            "external_ids": external_ids,
        }
        digest = _digest(metadata_payload)
        now = utc_now()

        with self._session_factory() as session:
            begin_immediate_write(session)
            record = session.get(UnpackTorrentMetadata, metadata.id)
            if (
                record is None
                or record.site_key != metadata.site_key
                or record.remote_torrent_id != metadata.remote_torrent_id
            ):
                session.rollback()
                return None, "TEMPLATE_METADATA_CHANGED"
            available_hashes = {
                value.casefold()
                for value in (torrent_meta.v1_info_hash, torrent_meta.v2_info_hash)
                if isinstance(value, str) and value
            }
            if record.info_hash and record.info_hash.casefold() not in available_hashes:
                session.rollback()
                return None, "TORRENT_INFO_HASH_MISMATCH"
            current_files = [
                dict(item) for item in (record.file_summary or []) if isinstance(item, dict)
            ]
            has_paths = any(
                isinstance(item.get("path"), str) and item["path"] for item in current_files
            )
            if not has_paths or record.total_size is None:
                record.total_size = total_size
                record.file_summary = file_summary
                record.descriptor = descriptor_payload
                record.info_hash = (
                    record.info_hash or torrent_meta.v1_info_hash or torrent_meta.v2_info_hash
                )
                record.metadata_digest = digest
                record.fetched_at = now
                record.source_provenance = {
                    **dict(record.source_provenance or {}),
                    "source": "site_detail_api+torrent_metainfo",
                    "site_key": metadata.site_key,
                    "metainfo_digest": torrent_meta.metainfo_digest,
                    "video_file_count": len(file_summary),
                }
                record.updated_at = now
            session.commit()
            return {
                "descriptor": dict(record.descriptor or {}),
                "total_size": record.total_size,
                "file_summary": [
                    dict(item) for item in (record.file_summary or []) if isinstance(item, dict)
                ],
            }, None

    def _selected_bindings(
        self,
        selected_ids: tuple[str, ...],
        *,
        execution_id: str,
    ) -> tuple[tuple[EnabledSiteAdapter, ...], tuple[str, ...]]:
        resolver = getattr(self._site_provider, "bindings_for_unpack_execution", None)
        active = (
            resolver(execution_id) if callable(resolver) else self._site_provider.enabled_adapters()
        )
        by_id = {item.config_id: item for item in active}
        bindings = tuple(by_id[site_id] for site_id in selected_ids if site_id in by_id)
        missing = tuple(site_id for site_id in selected_ids if site_id not in by_id)
        return bindings, missing

    def _search_failure_for_site(
        self,
        *,
        binding: EnabledSiteAdapter,
        execution_id: str,
        item_id: str,
        error: SiteAdapterError,
    ) -> _SearchFailure:
        outcome: SiteRiskControlOutcome | None = None
        if error.code in SITE_RISK_CONTROL_ERROR_CODES:
            recorder = getattr(self._site_provider, "record_unpack_risk_event", None)
            if callable(recorder):
                result = recorder(
                    execution_id=execution_id,
                    item_id=item_id,
                    site_config_id=binding.config_id,
                    error_code=error.code,
                )
                if isinstance(result, SiteRiskControlOutcome):
                    outcome = result
        retry_after = error.retry_after_seconds
        if outcome is not None:
            if retry_after is None:
                retry_after = float(outcome.retry_interval_seconds)
            elif (
                isinstance(retry_after, (int, float))
                and not isinstance(retry_after, bool)
                and isfinite(retry_after)
                and 0 <= retry_after <= 86_400
            ):
                retry_after = max(
                    float(retry_after),
                    float(outcome.retry_interval_seconds),
                )
            # Preserve invalid/unbounded server Retry-After values so the
            # existing retry gate can require manual intervention.
        return _SearchFailure(
            site_config_id=binding.config_id,
            code=error.code,
            retryable=error.retryable,
            retry_after_seconds=retry_after,
            risk_controlled=bool(outcome and outcome.threshold_reached),
            risk_count=outcome.risk_count if outcome is not None else None,
            risk_limit=outcome.risk_limit if outcome is not None else None,
            risk_retry_interval_seconds=(
                outcome.retry_interval_seconds if outcome is not None else None
            ),
        )

    async def _search_sites(
        self,
        unit: TaskUnit,
        bindings: tuple[EnabledSiteAdapter, ...],
        *,
        execution_id: str,
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
                active_page_number = 1
                try:
                    # Global 30s timeout used to include intentional waits.
                    # Time individual calls with a deadline that accounts for
                    # configured request time and one rate-limit spacing wait.
                    call_timeout = (
                        max(
                            self._site_timeout_seconds,
                            float(binding.request_timeout_seconds),
                        )
                        + float(binding.search_interval_seconds)
                        + 1.0
                    )
                    capabilities = await _await_site_request(
                        binding.adapter.capabilities(),
                        timeout_seconds=call_timeout,
                    )
                    queries = build_unpack_search_queries(
                        unit.descriptor,
                        unit_kind=unit.kind,
                        capabilities=capabilities,
                        max_queries=self._max_queries_per_site,
                    )
                    # The query builder preserves the intended order:
                    # external IDs > English title > Chinese title > release fallback.
                    interval = capabilities.min_request_interval_seconds
                    empty_page_retry_used = False
                    found: dict[str, _FoundCandidate] = {}
                    empty_queries: list[SearchQuery] = []
                    failures: list[_SearchFailure] = []
                    previous_query_first_page_identity: (
                        tuple[int | None, tuple[str, ...]] | None
                    ) = None

                    for query_index, base_query in enumerate(queries):
                        query_stage = _diagnostic_query_stage(base_query, unit.descriptor)
                        active_query_index = query_index + 1
                        active_query_stage = query_stage
                        first_page_identity: tuple[int | None, tuple[str, ...]] | None = None
                        query_failed = False
                        query_identity_mismatch = False
                        query_filter_ignored = False
                        stop_for_exact_candidate = False
                        query_had_results = False

                        for page_number in range(1, self._max_pages_per_query + 1):
                            if (query_index > 0 or page_number > 1) and interval > 0:
                                await asyncio.sleep(interval)

                            query = replace(base_query, page=page_number)
                            active_page_number = page_number
                            started = time.perf_counter()
                            try:
                                page = await _await_site_request(
                                    binding.adapter.search(query), timeout_seconds=call_timeout
                                )
                                if (
                                    page_number == 1
                                    and not page.items
                                    and not page.has_more
                                    and not empty_page_retry_used
                                ):
                                    # A transient empty first page is not evidence
                                    # that the title has no match. Retry one page per
                                    # site before moving through the query variants.
                                    empty_page_retry_used = True
                                    await asyncio.sleep(max(interval, 0.25))
                                    try:
                                        page = await _await_site_request(
                                            binding.adapter.search(query),
                                            timeout_seconds=call_timeout,
                                        )
                                    except SiteAdapterError as retry_exc:
                                        failures.append(
                                            self._search_failure_for_site(
                                                binding=binding,
                                                execution_id=execution_id,
                                                item_id=item_id,
                                                error=retry_exc,
                                            )
                                        )
                                        query_failed = True
                                        break
                                if page.items:
                                    query_had_results = True
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
                                            "page_number": page_number,
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
                                    self._search_failure_for_site(
                                        binding=binding,
                                        execution_id=execution_id,
                                        item_id=item_id,
                                        error=exc,
                                    )
                                )
                                # A transport/access error is not repaired by
                                # changing keywords or requesting another page.
                                query_failed = True
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
                                            "page_number": page_number,
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
                                query_identity_mismatch = True
                                break

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

                            page_identity = (
                                page.total_hint,
                                tuple(candidate.torrent_id for candidate in page.items),
                            )
                            if page_number == 1:
                                first_page_identity = page_identity

                            _search_diagnostics.info(
                                "影片站点搜索阶段统计",
                                extra={
                                    "fields": {
                                        "item_id": item_id,
                                        "generation": generation,
                                        "site_kind": binding.site_id,
                                        "query_index": query_index + 1,
                                        "query_stage": query_stage,
                                        "page_number": page_number,
                                        "returned_page_number": page.page,
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
                                        "unique_candidate_count": len(found),
                                        "duration_ms": round(
                                            (time.perf_counter() - started) * 1000, 2
                                        ),
                                    }
                                },
                            )

                            # Rousi can return the same broad result page for
                            # different keywords when its search filter is ignored.
                            # Compare only page 1 across queries, never page 1 to page 2.
                            if (
                                page_number == 1
                                and binding.site_id == "rousi_pro"
                                and previous_query_first_page_identity == page_identity
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
                                            "page_number": page_number,
                                            "outcome": "QUERY_FILTER_IGNORED_SUSPECTED",
                                            "requested_limit": query.page_size,
                                            "error_code": ("SITE_SEARCH_FILTER_IGNORED_SUSPECTED"),
                                            "returned_count": len(page.items),
                                            "reported_total": page.total_hint,
                                        }
                                    },
                                )
                                query_filter_ignored = True
                                break

                            # A near-exact title+size result is sufficient to
                            # stop extra requests on rate-limited trackers.
                            if interval > 0 and any(
                                not item.assessment.rejected
                                and item.assessment.evidence["exact"]["title"]
                                and item.assessment.evidence["exact"]["size"]
                                for item in found.values()
                            ):
                                stop_for_exact_candidate = True
                                break

                            if not page.has_more:
                                break
                            if page_number == self._max_pages_per_query:
                                _search_diagnostics.info(
                                    "影片站点搜索分页达到上限",
                                    extra={
                                        "fields": {
                                            "item_id": item_id,
                                            "generation": generation,
                                            "site_kind": binding.site_id,
                                            "query_index": query_index + 1,
                                            "query_stage": query_stage,
                                            "page_number": page_number,
                                            "max_pages_per_query": self._max_pages_per_query,
                                            "outcome": "PAGE_LIMIT_REACHED",
                                            "returned_count": len(page.items),
                                            "reported_total": page.total_hint,
                                            "has_more": True,
                                            "unique_candidate_count": len(found),
                                        }
                                    },
                                )
                                break

                        if (
                            not query_failed
                            and not query_identity_mismatch
                            and not query_had_results
                        ):
                            empty_queries.append(base_query)
                        if query_failed:
                            break
                        if query_identity_mismatch:
                            continue
                        if first_page_identity is not None:
                            previous_query_first_page_identity = first_page_identity
                        if query_filter_ignored or stop_for_exact_candidate:
                            break

                    has_usable_candidate = any(
                        not item.assessment.rejected for item in found.values()
                    )
                    if (
                        not has_usable_candidate
                        and empty_queries
                        and not query_failed
                        and not query_identity_mismatch
                        and not query_filter_ignored
                    ):
                        title_empty_queries = [
                            query for query in empty_queries if not query.external_ids
                        ]
                        retry_pool = title_empty_queries or empty_queries
                        year_token = (
                            str(unit.descriptor.year) if unit.descriptor.year is not None else None
                        )
                        episode_token = _episode_token(unit.descriptor)
                        retry_query = next(
                            (
                                query
                                for query in reversed(retry_pool)
                                if (year_token is not None and year_token in query.keywords)
                                or (episode_token is not None and episode_token in query.keywords)
                            ),
                            retry_pool[-1],
                        )
                        active_query_index = next(
                            (
                                index + 1
                                for index, query in enumerate(queries)
                                if query == retry_query
                            ),
                            len(queries),
                        )
                        active_query_stage = _diagnostic_query_stage(retry_query, unit.descriptor)
                        active_page_number = 1
                        await asyncio.sleep(max(interval, 0.25))
                        retry_started = time.perf_counter()
                        try:
                            retry_page = await _await_site_request(
                                binding.adapter.search(replace(retry_query, page=1)),
                                timeout_seconds=call_timeout,
                            )
                        except SiteAdapterError as retry_exc:
                            failures.append(
                                _SearchFailure(
                                    binding.config_id,
                                    retry_exc.code,
                                    retry_exc.retryable,
                                    retry_exc.retry_after_seconds,
                                )
                            )
                            _search_diagnostics.warning(
                                "站点空响应兜底重试失败",
                                extra={
                                    "fields": {
                                        "item_id": item_id,
                                        "generation": generation,
                                        "site_kind": binding.site_id,
                                        "query_index": active_query_index,
                                        "query_stage": active_query_stage,
                                        "outcome": "EMPTY_FALLBACK_RETRY_ERROR",
                                        "error_code": retry_exc.code,
                                    }
                                },
                            )
                        else:
                            identity_filtered = 0
                            title_filtered = 0
                            hard_conflicts = 0
                            selectable = 0
                            new_candidates = 0
                            if retry_page.site_id != binding.site_id:
                                failures.append(
                                    _SearchFailure(
                                        binding.config_id,
                                        "SITE_IDENTITY_MISMATCH",
                                        False,
                                    )
                                )
                            else:
                                for candidate in retry_page.items:
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
                                "站点空响应兜底重试完成",
                                extra={
                                    "fields": {
                                        "item_id": item_id,
                                        "generation": generation,
                                        "site_kind": binding.site_id,
                                        "query_index": active_query_index,
                                        "query_stage": active_query_stage,
                                        "outcome": "EMPTY_FALLBACK_RETRY_COMPLETED",
                                        "returned_count": len(retry_page.items),
                                        "identity_filtered_count": identity_filtered,
                                        "title_filtered_count": title_filtered,
                                        "hard_conflict_count": hard_conflicts,
                                        "selectable_count": selectable,
                                        "new_candidate_count": new_candidates,
                                        "unique_candidate_count": len(found),
                                        "duration_ms": round(
                                            (time.perf_counter() - retry_started) * 1000, 2
                                        ),
                                    }
                                },
                            )
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
                                "page_number": active_page_number,
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
                                "page_number": active_page_number,
                                "outcome": "SITE_ERROR",
                                "error_code": exc.code,
                            }
                        },
                    )
                    return [], [
                        self._search_failure_for_site(
                            binding=binding,
                            execution_id=execution_id,
                            item_id=item_id,
                            error=exc,
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
                item.source_mapping is None,
                -item.assessment.score_bps,
                item.site_config_id or (item.binding.config_id if item.binding is not None else ""),
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
                    site_id=found.local_site_config_id,
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
                        "adapter_site_id": found.stable_site_key,
                        "site_config_version": found.local_config_version,
                        **(
                            {
                                "source_mapping": {
                                    "matched": True,
                                    "mapping_status": found.source_mapping.get(
                                        "mapping_status", "SUGGESTED"
                                    ),
                                    "collection_root_name": found.source_mapping.get(
                                        "collection_root_name"
                                    ),
                                    "relative_directory": found.source_mapping.get(
                                        "relative_directory"
                                    ),
                                    "relative_file_name": found.source_mapping.get(
                                        "relative_file_name"
                                    ),
                                    "media_title": found.source_mapping.get("media_title"),
                                    "confidence_bps": found.source_mapping.get("confidence_bps", 0),
                                    "match_mode": found.source_mapping.get(
                                        "match_mode", "SAVED_DIRECTORY_MAPPING"
                                    ),
                                    **(
                                        {
                                            "configured_media_title": found.source_mapping[
                                                "configured_media_title"
                                            ]
                                        }
                                        if isinstance(
                                            found.source_mapping.get("configured_media_title"), str
                                        )
                                        else {}
                                    ),
                                    **(
                                        {"size_comparison": found.source_mapping["size_comparison"]}
                                        if isinstance(
                                            found.source_mapping.get("size_comparison"), dict
                                        )
                                        else {}
                                    ),
                                }
                            }
                            if found.source_mapping is not None
                            else {}
                        ),
                        **(
                            {
                                "template_seed_mapping": {
                                    "template_seed_id": found.template_seed_id,
                                    "status": "AMBIGUOUS"
                                    if found.template_mapping_ambiguous
                                    else "UNIQUE",
                                    "decision": "REVIEW_REQUIRED"
                                    if found.template_mapping_ambiguous
                                    else (
                                        "AUTO_MATCH"
                                        if claimed.auto_match_enabled
                                        else "AUTO_MATCH_DISABLED"
                                    ),
                                    "scope": "FILE"
                                    if isinstance(
                                        (found.source_mapping or {}).get("relative_file_name"), str
                                    )
                                    else "DIRECTORY",
                                    "binding_policy": (
                                        "EXISTING_TEMPLATE_SEED_MAPPING_IS_AUTHORITATIVE"
                                    ),
                                }
                            }
                            if (
                                found.template_mapping_authoritative
                                or found.template_mapping_ambiguous
                            )
                            else {}
                        ),
                    },
                    verification_status="NOT_CHECKED",
                    raw_ref={
                        "torrent_id": found.candidate.torrent_id,
                        "adapter_site_id": found.stable_site_key,
                        "site_config_id": found.local_site_config_id,
                        "site_config_version": found.local_config_version,
                    },
                    created_at=now,
                )
                session.add(record)
                persisted.append((record, found))
            session.flush()

            selectable = [pair for pair in persisted if not pair[1].assessment.rejected]
            mapped_candidates = [
                pair for pair in persisted if pair[1].template_mapping_authoritative
            ]
            mapping_ambiguous = (
                any(found.template_mapping_ambiguous for _record, found in persisted)
                or len(mapped_candidates) > 1
            )
            if mapping_ambiguous:
                top = None
            elif len(mapped_candidates) == 1:
                # A unique persisted template-to-seed relation is authoritative for
                # candidate selection; score/size evidence remains recorded and is
                # still consumed by the later content-verification stage.
                top = mapped_candidates[0]
            else:
                top = selectable[0] if selectable else None
            selectable_candidate_count = len(selectable)
            if len(mapped_candidates) == 1 and mapped_candidates[0] not in selectable:
                selectable_candidate_count += 1
            risk_failures = [failure for failure in failures if failure.risk_count is not None]
            risk_failure = next(
                (failure for failure in risk_failures if failure.risk_controlled),
                risk_failures[0] if risk_failures else None,
            )
            risk_not_before: datetime | None = None
            if risk_failure is not None:
                cooldown_seconds = max(
                    max(
                        failure.risk_retry_interval_seconds or 60,
                        ceil(
                            failure.retry_after_seconds
                            if isinstance(failure.retry_after_seconds, (int, float))
                            and not isinstance(failure.retry_after_seconds, bool)
                            and isfinite(failure.retry_after_seconds)
                            and 0 <= failure.retry_after_seconds <= 86_400
                            else 0
                        ),
                    )
                    for failure in risk_failures
                )
                risk_not_before = now + timedelta(seconds=cooldown_seconds)
                self._defer_pending_items(
                    session,
                    execution.id,
                    now=now,
                    not_before=risk_not_before,
                    site_config_ids=tuple(
                        dict.fromkeys(failure.site_config_id for failure in risk_failures)
                    ),
                    risk_count=risk_failure.risk_count,
                    risk_limit=risk_failure.risk_limit,
                    requires_manual_reenable=bool(risk_failure.risk_controlled),
                )
            item.match_finished_at = now
            item.updated_at = now
            item.last_error_code = None
            item.last_error_message = None
            if top is not None:
                top_record, top_found = top
                evidence_coverage_bps = int(
                    top_found.assessment.evidence.get("evidence_coverage_bps", 0)
                )
                if claimed.auto_match_enabled and (
                    top_found.template_mapping_authoritative
                    or (
                        top_found.assessment.score_bps >= claimed.auto_match_threshold_bps
                        and (
                            claimed.auto_match_threshold_bps <= 0
                            or evidence_coverage_bps >= MIN_AUTO_MATCH_EVIDENCE_COVERAGE_BPS
                        )
                    )
                ):
                    item.status = UnpackItemStatus.MATCHED_AUTO.value
                    item.selected_candidate_id = top_record.id
                    item.match_origin = "AUTO"
                else:
                    item.status = UnpackItemStatus.REVIEW_REQUIRED.value
                    item.selected_candidate_id = None
                    item.match_origin = None
            elif mapping_ambiguous:
                item.status = UnpackItemStatus.REVIEW_REQUIRED.value
                item.selected_candidate_id = None
                item.match_origin = None
                item.last_error_code = "UNPACK_TEMPLATE_MAPPING_AMBIGUOUS"
                item.last_error_message = (
                    "模板中存在多条影片目录到站点种子的映射关系，无法唯一确定，请人工审核"
                )
            elif failures:
                item.selected_candidate_id = None
                item.match_origin = None
                if risk_failure is not None and risk_failure.risk_controlled:
                    not_before = risk_not_before or now + timedelta(
                        seconds=risk_failure.risk_retry_interval_seconds or 60
                    )
                    item.status = UnpackItemStatus.MATCH_PENDING.value
                    item.last_error_code = "SITE_RISK_CONTROL_LIMIT_REACHED"
                    observed_count = (
                        risk_failure.risk_count
                        if risk_failure.risk_count is not None
                        else risk_failure.risk_limit
                    )
                    item.last_error_message = (
                        f"站点在10分钟内触发风控 {observed_count}/"
                        f"{risk_failure.risk_limit} 次，已自动停用。影片任务已延后；"
                        "请确认站点恢复正常后手动重新启用，系统不会自动启用站点。"
                    )
                    item.match_finished_at = now
                    state = dict(item.auxiliary_state or {})
                    state["auto_retry_not_before"] = not_before.isoformat()
                    state["risk_control_wait"] = {
                        "site_config_ids": [risk_failure.site_config_id],
                        "not_before": not_before.isoformat(),
                        "requires_manual_reenable": True,
                        "risk_count": risk_failure.risk_count,
                        "risk_limit": risk_failure.risk_limit,
                    }
                    item.auxiliary_state = state
                elif risk_failure is not None:
                    if self._schedule_automatic_retry(
                        session,
                        execution,
                        item,
                        now,
                        retry_after_seconds=tuple(
                            failure.retry_after_seconds for failure in failures if failure.retryable
                        ),
                    ):
                        item.last_error_code = "SITE_RISK_CONTROL_COOLDOWN"
                        item.last_error_message = (
                            f"站点在10分钟内触发风控 {risk_failure.risk_count}/"
                            f"{risk_failure.risk_limit} 次；任务已按配置延后，"
                            "间隔结束后会继续尝试。达到次数上限时将自动停用站点。"
                        )
                        state = dict(item.auxiliary_state or {})
                        state["risk_control_wait"] = {
                            "site_config_ids": [
                                failure.site_config_id for failure in risk_failures
                            ],
                            "not_before": state.get("auto_retry_not_before"),
                            "requires_manual_reenable": False,
                            "risk_count": risk_failure.risk_count,
                            "risk_limit": risk_failure.risk_limit,
                        }
                        item.auxiliary_state = state
                    else:
                        item.status = UnpackItemStatus.MATCH_ERROR.value
                        item.last_error_code = risk_failure.code
                        item.last_error_message = (
                            f"站点在10分钟内触发风控 {risk_failure.risk_count}/"
                            f"{risk_failure.risk_limit} 次，但自动重试额度已用完；"
                            "其他待匹配影片已按配置延后，请检查站点后手动重试。"
                        )
                else:
                    # Only transient failures may consume automatic retries.
                    # A permanent auth/permission/configuration error must stay
                    # actionable rather than repeatedly re-requesting the site.
                    retryable_failure = any(failure.retryable for failure in failures)
                    # Retryability is not synonymous with timeout: HTTP 503 and
                    # network unavailability must retain the real site error code
                    # after the bounded retry budget is exhausted.
                    is_timeout = any(
                        failure.code in {"UNPACK_MATCH_TIMEOUT", "SITE_TIMEOUT"}
                        for failure in failures
                    )
                    if retryable_failure and self._schedule_automatic_retry(
                        session,
                        execution,
                        item,
                        now,
                        retry_after_seconds=tuple(
                            failure.retry_after_seconds for failure in failures if failure.retryable
                        ),
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
                            else (
                                "执行快照指定的站点尚未启用或不可用于匹配；"
                                "请在站点配置中启用该站点后重试"
                                if any(f.code == "UNPACK_MATCH_SITE_UNAVAILABLE" for f in failures)
                                else "站点匹配失败，自动重试已结束，请人工处理"
                            )
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
                "selectable_candidate_count": selectable_candidate_count,
            }
            item.version += 1
            session.flush()
            self._refresh_execution_state(session, execution)
            session.commit()

    def _schedule_automatic_retry(
        self,
        session: Session,
        execution: UnpackExecution,
        item: UnpackExecutionItem,
        now: datetime,
        *,
        retry_after_seconds: tuple[float | None, ...] = (),
    ) -> bool:
        if self._pause_after_match:
            return False
        definition = session.get(UnpackDefinition, execution.definition_id)
        if (
            definition is None
            or not definition.retry_enabled
            or item.retry_count >= definition.max_retries
        ):
            return False
        wait_seconds = min(30, 2 ** min(item.retry_count + 1, 4))
        for requested in retry_after_seconds:
            if requested is None:
                continue
            # Never turn an invalid or unbounded server cooldown into rapid
            # retries or an overflowing timedelta. Require human intervention
            # when the server requests more than a day of silence.
            if (
                isinstance(requested, bool)
                or not isinstance(requested, (int, float))
                or not isfinite(requested)
                or requested < 0
                or requested > 86_400
            ):
                return False
            wait_seconds = max(wait_seconds, ceil(requested))
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

    def _refresh_execution_state(self, session: Session, execution: UnpackExecution) -> None:
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
        if self._pause_after_match:
            execution.status = UnpackExecutionStatus.MATCHING.value
            execution.finished_at = None
            execution.updated_at = now
            execution.version += 1
            return
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


def _matching_exclusion_pattern(
    relative_path: Any,
    file_filter: Any,
    extra_exclude_names: tuple[str, ...],
) -> str | None:
    if not isinstance(relative_path, str) or not relative_path:
        return None
    normalized_path = relative_path.replace("\\", "/")
    folded_path = normalized_path.casefold()
    configured = file_filter.get("exclude_names", ()) if isinstance(file_filter, dict) else ()
    if isinstance(configured, (list, tuple)):
        for value in configured:
            if isinstance(value, str):
                pattern = value.strip().casefold()
                if pattern and pattern in folded_path:
                    return pattern
    basename = PurePosixPath(normalized_path).name.casefold()
    for value in extra_exclude_names:
        pattern = value.strip().casefold()
        if pattern and pattern in basename:
            return pattern
    return None


def _mapped_source_directory(source_relative_path: str | None) -> str | None:
    if not isinstance(source_relative_path, str) or not source_relative_path.strip():
        return None
    normalized = source_relative_path.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or any(part in {".", ".."} for part in path.parts):
        return None
    parent = path.parent.as_posix()
    return parent if parent not in {"", "."} else None


def _mapped_torrent_file_size(
    file_summary: object,
    source_relative_path: str | None,
    source_mapping: dict[str, Any],
) -> dict[str, Any] | None:
    if not isinstance(file_summary, list) or not isinstance(source_relative_path, str):
        return None
    source_path = PurePosixPath(source_relative_path.replace("\\", "/"))
    if source_path.is_absolute() or any(part in {".", ".."} for part in source_path.parts):
        return None
    normalized_source = source_path.as_posix().casefold()
    mapped_file_name = source_mapping.get("relative_file_name")
    target_basename = (
        PurePosixPath(mapped_file_name.replace("\\", "/")).name
        if isinstance(mapped_file_name, str) and mapped_file_name.strip()
        else source_path.name
    )
    target_basename = target_basename.casefold()
    path_matches: list[dict[str, Any]] = []
    basename_matches: list[dict[str, Any]] = []
    for item in file_summary:
        if not isinstance(item, dict):
            continue
        basename = item.get("basename")
        length = item.get("length")
        if (
            not isinstance(basename, str)
            or not isinstance(length, int)
            or isinstance(length, bool)
            or length < 0
        ):
            continue
        candidate = {"basename": basename, "length": length, "path": item.get("path")}
        raw_path = item.get("path")
        if isinstance(raw_path, str) and raw_path.strip():
            path = PurePosixPath(raw_path.replace("\\", "/"))
            if not path.is_absolute() and not any(part in {".", ".."} for part in path.parts):
                normalized_candidate = path.as_posix().casefold()
                if normalized_candidate == normalized_source or normalized_candidate.endswith(
                    "/" + normalized_source
                ):
                    candidate["match_mode"] = "RELATIVE_PATH"
                    path_matches.append(candidate)
        if basename.casefold() == target_basename:
            candidate["match_mode"] = "UNIQUE_BASENAME"
            basename_matches.append(candidate)
    if len(path_matches) == 1:
        return path_matches[0]
    if len(path_matches) > 1:
        return None
    if len(basename_matches) == 1:
        return basename_matches[0]
    video_files = [
        item
        for item in file_summary
        if isinstance(item, dict)
        and isinstance(item.get("basename"), str)
        and PurePosixPath(item["basename"]).suffix.casefold() in _VIDEO_FILE_SUFFIXES
        and isinstance(item.get("length"), int)
        and not isinstance(item.get("length"), bool)
        and item.get("length", -1) >= 0
    ]
    if not basename_matches and len(video_files) == 1:
        only = video_files[0]
        if (
            isinstance(only.get("basename"), str)
            and isinstance(only.get("length"), int)
            and not isinstance(only.get("length"), bool)
            and only.get("length", -1) >= 0
        ):
            return {
                "basename": only["basename"],
                "length": only["length"],
                "path": only.get("path"),
                "match_mode": "SINGLE_VIDEO_FILE",
            }
    return None


def _unique_template_file_match(
    seed_file_summaries: tuple[tuple[str, object], ...],
    *,
    source_relative_path: str | None,
    source_size: int | None,
) -> tuple[str, dict[str, Any]] | None:
    """Return only a globally unique exact filename+size hit in template metadata.

    This is a candidate hint, not content verification. Repeated exact
    filename+size hits across seeds or within one torrent fail closed.
    """
    if (
        not isinstance(source_relative_path, str)
        or not source_relative_path.strip()
        or not isinstance(source_size, int)
        or isinstance(source_size, bool)
        or source_size < 0
    ):
        return None
    source_path = PurePosixPath(source_relative_path.replace("\\", "/"))
    if (
        source_path.is_absolute()
        or any(part in {".", ".."} for part in source_path.parts)
        or source_path.suffix.casefold() not in _VIDEO_FILE_SUFFIXES
    ):
        return None

    target_basename = source_path.name.casefold()
    matches: list[tuple[str, dict[str, Any]]] = []
    ambiguous = False
    for seed_id, raw_file_summary in seed_file_summaries:
        if not isinstance(seed_id, str) or not seed_id or not isinstance(raw_file_summary, list):
            continue
        seed_matches: list[dict[str, Any]] = []
        for raw_file in raw_file_summary:
            if not isinstance(raw_file, dict):
                continue
            raw_basename = raw_file.get("basename")
            if not isinstance(raw_basename, str) or not raw_basename.strip():
                raw_path = raw_file.get("path") or raw_file.get("name")
                if not isinstance(raw_path, str) or not raw_path.strip():
                    continue
                raw_basename = PurePosixPath(raw_path.replace("\\", "/")).name
            else:
                raw_basename = PurePosixPath(raw_basename.replace("\\", "/")).name
            length = raw_file.get("length")
            if (
                raw_basename.casefold() != target_basename
                or not isinstance(length, int)
                or isinstance(length, bool)
                or length != source_size
            ):
                continue
            seed_matches.append(
                {
                    "basename": raw_basename,
                    "length": length,
                    "path": raw_file.get("path"),
                }
            )
        if len(seed_matches) > 1:
            ambiguous = True
        elif len(seed_matches) == 1:
            matches.append((seed_id, seed_matches[0]))

    if ambiguous or len(matches) != 1:
        return None
    return matches[0]


def _mapped_template_descriptor(
    media_title: str,
    *,
    candidate_descriptor: MediaDescriptor,
    source_descriptor: MediaDescriptor,
    candidate_video_file_count: int,
    total_size: int | None = None,
    files: tuple[MediaFileSummary, ...] = (),
) -> MediaDescriptor:
    """Build a per-file descriptor without losing reliable single-movie IDs.

    A single-video torrent's external IDs describe that movie and must survive
    mapped-title parsing. Multi-video collections may carry collection-level IDs,
    so those IDs must not be attached to an individual movie in the pack.
    """
    mapped = parse_media_name(media_title, total_size=total_size, files=files)
    if candidate_video_file_count == 1 and source_descriptor.episode is None:
        return replace(mapped, external_ids=candidate_descriptor.external_ids)
    return mapped


def _source_year_matches_title_range(source_year: int | None, title: str) -> bool:
    if not isinstance(source_year, int) or isinstance(source_year, bool):
        return False
    for start_text, end_text in re.findall(
        r"(?<!\d)((?:19|20)\d{2})\s*[-‐‑‒–—]\s*((?:19|20)\d{2})(?!\d)",
        title,
        flags=re.IGNORECASE,
    ):
        start, end = int(start_text), int(end_text)
        if min(start, end) <= source_year <= max(start, end):
            return True
    return False


def _template_candidate_relevant_for_source(
    source_descriptor: MediaDescriptor,
    candidate_descriptor: MediaDescriptor,
    candidate_title: str,
) -> bool:
    if candidate_search_relevant(source_descriptor, candidate_descriptor):
        return True
    return _source_year_matches_title_range(source_descriptor.year, candidate_title) and bool(
        set(source_descriptor.title_tokens) & set(candidate_descriptor.title_tokens)
    )


def _has_matching_seed_directory_mapping(
    mapping_rules: object,
    *,
    collection_root_name: str | None,
    relative_directory: str | None,
    source_relative_path: str | None,
) -> bool:
    if (
        not collection_root_name
        or not relative_directory
        or not isinstance(source_relative_path, str)
        or not isinstance(mapping_rules, dict)
    ):
        return False
    source_path = PurePosixPath(source_relative_path.replace("\\", "/"))
    if source_path.is_absolute() or any(part in {".", ".."} for part in source_path.parts):
        return False
    source_mappings = mapping_rules.get("source_mappings")
    if not isinstance(source_mappings, list):
        return False
    for mapping in source_mappings:
        if not isinstance(mapping, dict):
            continue
        if (
            mapping.get("collection_root_name") != collection_root_name
            or mapping.get("relative_directory") != relative_directory
            or not isinstance(mapping.get("media_title"), str)
        ):
            continue
        mapped_file_name = mapping.get("relative_file_name")
        if isinstance(mapped_file_name, str) and mapped_file_name != source_path.name:
            continue
        return True
    return False


def _should_hydrate_mapped_template_candidate(
    *,
    source_descriptor: MediaDescriptor,
    mapped_descriptor: MediaDescriptor,
    source_size: int | None,
    has_metainfo_paths: bool,
    has_mapped_file: bool,
    confidence_bps: int,
    assessment: UnpackCandidateAssessment,
) -> bool:
    """Hydrate a high-confidence mapped seed whenever file-level evidence is missing.

    A mapped title is derived from the source directory, so its title/quality score can
    look complete even while the actual torrent file list and size are still unknown.
    That synthetic score must never suppress the metainfo read needed to verify the file.
    """
    return (
        source_size is not None
        and not has_metainfo_paths
        and not has_mapped_file
        and confidence_bps >= 9_000
        and candidate_search_relevant(source_descriptor, mapped_descriptor)
        and not assessment.rejected
    )


def _should_hydrate_unmapped_template_candidate(
    *,
    source_descriptor: MediaDescriptor,
    candidate_descriptor: MediaDescriptor,
    candidate_title: str,
    source_size: int | None,
    has_metainfo_paths: bool,
    has_matching_source_directory_mapping: bool,
    assessment: UnpackCandidateAssessment,
) -> bool:
    if source_size is None or has_metainfo_paths:
        return False
    # A directory mapping can nominate a saved seed for read-only metadata
    # inspection. Automatic candidate selection is handled separately from this
    # hydration heuristic; content verification remains a later execution stage.
    if has_matching_source_directory_mapping:
        return True
    if not _template_candidate_relevant_for_source(
        source_descriptor, candidate_descriptor, candidate_title
    ):
        return False
    if not assessment.rejected:
        return True
    hard_conflicts = {str(value) for value in assessment.evidence.get("hard_conflicts", [])}
    return (
        bool(hard_conflicts)
        and hard_conflicts <= {"YEAR_CONFLICT", "EXTERNAL_ID_CONFLICT"}
        and _source_year_matches_title_range(source_descriptor.year, candidate_title)
    )


def _matching_seed_source_mapping(
    mapping_rules: object,
    *,
    collection_root_name: str | None,
    relative_directory: str | None,
    source_relative_path: str | None,
) -> dict[str, Any] | None:
    if (
        not collection_root_name
        or not relative_directory
        or not isinstance(source_relative_path, str)
        or not isinstance(mapping_rules, dict)
    ):
        return None
    normalized_path = source_relative_path.replace("\\", "/")
    path = PurePosixPath(normalized_path)
    if (
        path.is_absolute()
        or any(part in {".", ".."} for part in path.parts)
        or path.parent.as_posix() != relative_directory
    ):
        return None
    source_mappings = mapping_rules.get("source_mappings")
    if not isinstance(source_mappings, list):
        return None

    # The saved template relation is authoritative for this exact collection
    # root and relative movie directory. A file-level relation is narrower and
    # must match the basename exactly; directory-level relations match the
    # explicitly mapped movie directory. Conflicting seed IDs are handled by
    # the caller as ambiguous and never auto-selected.
    for prefer_file_mapping in (True, False):
        for mapping in source_mappings:
            if not isinstance(mapping, dict):
                continue
            mapped_file_name = mapping.get("relative_file_name")
            if "relative_file_name" in mapping and (
                not isinstance(mapped_file_name, str) or not mapped_file_name
            ):
                continue
            is_file_mapping = isinstance(mapped_file_name, str)
            if is_file_mapping != prefer_file_mapping:
                continue
            if (
                mapping.get("collection_root_name") != collection_root_name
                or mapping.get("relative_directory") != relative_directory
                or not isinstance(mapping.get("media_title"), str)
                or not mapping.get("media_title", "").strip()
            ):
                continue
            if is_file_mapping and mapped_file_name != path.name:
                continue
            return mapping
    return None


def _task_unit_from_item(
    item: UnpackExecutionItem,
    *,
    source_root: str | None = None,
) -> TaskUnit:
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
    metadata = None
    if isinstance(full_source_path, str) and source_root:
        metadata = read_adjacent_media_metadata(
            full_source_path,
            source_root,
            expected_device=snapshot.get("device"),
            expected_inode=snapshot.get("inode"),
            expected_size=size,
            expected_mtime_ns=snapshot.get("mtime_ns"),
            descriptor=descriptor,
        )
    if metadata is not None and metadata.titles:
        aliases = tuple(dict.fromkeys((*aliases, *metadata.titles)))
        descriptor = parse_media_name(
            display_name,
            aliases=aliases,
            total_size=size,
            files=(MediaFileSummary(relative, size),),
        )
    if metadata is not None and metadata.external_ids:
        merged_ids = list(descriptor.external_ids)
        known_by_namespace = {value.namespace: value.value for value in merged_ids}
        for external_id in metadata.external_ids:
            existing = known_by_namespace.get(external_id.namespace)
            if existing is not None:
                # Do not allow sidecar metadata to override a conflicting ID
                # explicitly present in the media release name.
                if existing != external_id.value:
                    continue
                continue
            merged_ids.append(external_id)
            known_by_namespace[external_id.namespace] = external_id.value
        descriptor = replace(
            descriptor,
            external_ids=tuple(
                sorted(merged_ids, key=lambda value: (value.namespace, value.value))
            ),
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
