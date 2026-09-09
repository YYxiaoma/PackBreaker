from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.errors import ApplicationError
from backend.app.application.sites import EnabledSiteAdapter
from backend.app.domain.candidate_scoring import CandidateScore, rank_candidates, score_candidate
from backend.app.domain.errors import DomainViolation
from backend.app.domain.file_mapping import AutoMappingDecision, SourceFileCandidate, auto_map_files
from backend.app.domain.preflight import (
    CandidatePreflightEvidence,
    PreflightSnapshot,
    SiteAnalysisEvidence,
    search_query_signature,
)
from backend.app.domain.site_search import CandidateMeta, SearchQuery, build_search_queries
from backend.app.domain.task_units import TaskUnit
from backend.app.domain.torrent import TorrentKind, TorrentMeta
from backend.app.domain.verification import VerificationLevel
from backend.app.infrastructure.adapters.site_errors import SiteAdapterError
from backend.app.infrastructure.persistence.preflight_repositories import (
    PreflightSnapshotRepository,
)
from backend.app.infrastructure.persistence.repositories import TaskRepository
from backend.app.infrastructure.piece_verifier import (
    V1FileMapping,
    verify_hybrid,
    verify_v1_pieces,
    verify_v2_files,
)
from backend.app.infrastructure.source_inventory import (
    scan_source_inventory,
    source_inventory_digest,
)
from backend.app.infrastructure.torrent_parser import parse_torrent


@dataclass(frozen=True, slots=True)
class AnalysisPolicy:
    max_queries_per_site: int = 3
    max_candidates_to_verify: int = 5

    def __post_init__(self) -> None:
        if not 1 <= self.max_queries_per_site <= 3:
            raise ValueError("max_queries_per_site 必须位于 1..3")
        if not 1 <= self.max_candidates_to_verify <= 20:
            raise ValueError("max_candidates_to_verify 必须位于 1..20")


class AnalysisSiteProvider(Protocol):
    def enabled_adapters(self) -> tuple[EnabledSiteAdapter, ...]: ...

    def enabled_site_versions(self) -> tuple[tuple[str, int], ...]: ...


class AnalysisService:
    """M2 只读分析编排：搜索、取种、解析、映射、验证、落不可变 preflight。"""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        site_service: AnalysisSiteProvider,
        *,
        policy: AnalysisPolicy | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._site_service = site_service
        self._policy = policy or AnalysisPolicy()

    async def analyze(
        self,
        *,
        task_id: str,
        unit: TaskUnit,
        source_root: Path,
    ) -> PreflightSnapshot:
        task_version = self._task_version(task_id, unit.normalized_unit_key)
        inventory = scan_source_inventory(source_root)
        inventory_digest = source_inventory_digest(inventory)
        bindings = self._site_service.enabled_adapters()
        if not bindings:
            raise ApplicationError(
                code="ANALYSIS_NO_ENABLED_SITES",
                status=409,
                title="没有可用站点",
                detail="至少需要启用一个已通过连接测试的站点才能执行分析",
            )

        queries = build_search_queries(unit, max_queries=self._policy.max_queries_per_site)
        planned_query_signatures = tuple(search_query_signature(query) for query in queries)
        candidates, site_evidence = await self._search_sites(bindings, queries)
        ranked = rank_candidates(
            unit.descriptor,
            tuple(
                (_candidate_key(candidate), candidate.descriptor)
                for candidate in candidates.values()
            ),
        )
        score_by_key = {item.candidate_id: item.result for item in ranked}
        selected_keys = {
            item.candidate_id
            for item in tuple(item for item in ranked if not item.result.rejected)[
                : self._policy.max_candidates_to_verify
            ]
        }

        evidence: list[CandidatePreflightEvidence] = []
        binding_by_site = {binding.site_id: binding for binding in bindings}
        for ranked_item in ranked:
            candidate = candidates[_split_candidate_key(ranked_item.candidate_id)]
            selected = ranked_item.candidate_id in selected_keys
            if not selected:
                evidence.append(_unverified_evidence(candidate, ranked_item.result, selected=False))
                continue
            binding = binding_by_site.get(candidate.site_id)
            if binding is None:
                evidence.append(
                    _unverified_evidence(
                        candidate,
                        ranked_item.result,
                        selected=True,
                        error_code="SITE_IDENTITY_MISMATCH",
                    )
                )
                continue
            evidence.append(
                await self._verify_candidate(
                    binding,
                    candidate,
                    unit,
                    inventory,
                    fallback_score=score_by_key[ranked_item.candidate_id],
                )
            )

        final_inventory = scan_source_inventory(source_root)
        if source_inventory_digest(final_inventory) != inventory_digest:
            raise ApplicationError(
                code="ANALYSIS_SOURCE_CHANGED",
                status=409,
                title="源文件发生变化",
                detail="分析期间源文件清单或文件快照发生变化，请重新执行分析",
            )
        initial_site_versions = tuple((item.config_id, item.config_version) for item in bindings)
        if tuple(sorted(self._site_service.enabled_site_versions())) != tuple(
            sorted(initial_site_versions)
        ):
            raise ApplicationError(
                code="ANALYSIS_SITE_CONFIG_CHANGED",
                status=409,
                title="站点配置发生变化",
                detail="分析期间启用站点或版本发生变化，请重新执行分析",
            )
        if self._task_version(task_id, unit.normalized_unit_key) != task_version:
            raise ApplicationError(
                code="ANALYSIS_TASK_CHANGED",
                status=409,
                title="任务发生变化",
                detail="分析期间任务版本发生变化，请重新执行分析",
            )

        snapshot = PreflightSnapshot(
            task_id=task_id,
            task_version=task_version,
            normalized_unit_key=unit.normalized_unit_key,
            source_inventory_digest=inventory_digest,
            site_versions=initial_site_versions,
            planned_queries=planned_query_signatures,
            sites=site_evidence,
            candidates=tuple(evidence),
            created_at=datetime.now(UTC),
        )
        with self._session_factory() as session:
            task = TaskRepository(session).get(task_id)
            if task is None or task.version != task_version:
                raise ApplicationError(
                    code="ANALYSIS_TASK_CHANGED",
                    status=409,
                    title="任务发生变化",
                    detail="写入 preflight 前任务版本已变化",
                )
            PreflightSnapshotRepository(session).create_or_get(snapshot)
            session.commit()
        return snapshot

    async def _search_sites(
        self,
        bindings: tuple[EnabledSiteAdapter, ...],
        queries: tuple[SearchQuery, ...],
    ) -> tuple[dict[tuple[str, str], CandidateMeta], tuple[SiteAnalysisEvidence, ...]]:
        candidates: dict[tuple[str, str], CandidateMeta] = {}
        sites: list[SiteAnalysisEvidence] = []
        for binding in bindings:
            executed: list[str] = []
            errors: list[str] = []
            try:
                capabilities = await binding.adapter.capabilities()
            except SiteAdapterError as exc:
                sites.append(
                    SiteAnalysisEvidence(
                        binding.config_id,
                        binding.config_version,
                        binding.site_id,
                        (),
                        (exc.code,),
                    )
                )
                continue
            query_limit = self._policy.max_queries_per_site
            if capabilities.min_request_interval_seconds > 0:
                query_limit = 1
            for query in queries[:query_limit]:
                signature = search_query_signature(query)
                executed.append(signature)
                try:
                    page = await binding.adapter.search(query)
                except SiteAdapterError as exc:
                    errors.append(exc.code)
                    continue
                if page.site_id != binding.site_id:
                    errors.append("SITE_IDENTITY_MISMATCH")
                    continue
                for candidate in page.items:
                    if candidate.site_id != binding.site_id:
                        errors.append("SITE_IDENTITY_MISMATCH")
                        continue
                    candidates.setdefault(candidate.identity, candidate)
            sites.append(
                SiteAnalysisEvidence(
                    binding.config_id,
                    binding.config_version,
                    binding.site_id,
                    tuple(executed),
                    tuple(errors),
                )
            )
        return candidates, tuple(sites)

    async def _verify_candidate(
        self,
        binding: EnabledSiteAdapter,
        candidate: CandidateMeta,
        unit: TaskUnit,
        inventory: tuple[SourceFileCandidate, ...],
        *,
        fallback_score: CandidateScore,
    ) -> CandidatePreflightEvidence:
        detailed = candidate
        current_score = fallback_score
        try:
            details = await binding.adapter.fetch_details(candidate.torrent_id)
            if details.site_id != binding.site_id or details.torrent_id != candidate.torrent_id:
                raise SiteAdapterError("SITE_IDENTITY_MISMATCH", "站点详情身份与搜索候选不一致")
            detailed = details.candidate
            current_score = score_candidate(unit.descriptor, detailed.descriptor)
            if current_score.rejected:
                return _unverified_evidence(detailed, current_score, selected=True)
            payload = await binding.adapter.fetch_torrent(candidate.torrent_id)
            if payload.site_id != binding.site_id or payload.torrent_id != candidate.torrent_id:
                raise SiteAdapterError("SITE_IDENTITY_MISMATCH", "torrent payload 身份与候选不一致")
            meta = parse_torrent(payload.content)
            mappings = auto_map_files(meta, inventory)
            level = _verification_level(meta, mappings)
            return CandidatePreflightEvidence(
                site_id=detailed.site_id,
                torrent_id=detailed.torrent_id,
                display_name=detailed.display_name,
                score=current_score,
                selected_for_verification=True,
                metainfo_digest=meta.metainfo_digest,
                verification_level=level,
                mappings=mappings,
            )
        except SiteAdapterError as exc:
            return _unverified_evidence(
                detailed,
                current_score,
                selected=True,
                error_code=exc.code,
            )
        except DomainViolation as exc:
            return _unverified_evidence(
                detailed,
                current_score,
                selected=True,
                error_code=exc.code.value,
            )

    def _task_version(self, task_id: str, normalized_unit_key: str) -> int:
        with self._session_factory() as session:
            task = TaskRepository(session).get(task_id)
            if task is None:
                raise ApplicationError(
                    code="TASK_NOT_FOUND",
                    status=404,
                    title="任务不存在",
                    detail="未找到指定分析任务",
                )
            if task.normalized_unit_key != normalized_unit_key:
                raise ApplicationError(
                    code="ANALYSIS_UNIT_MISMATCH",
                    status=409,
                    title="处理单元不匹配",
                    detail="分析请求的处理单元与任务记录不一致",
                )
            return task.version


def _verification_level(
    meta: TorrentMeta,
    mappings: tuple[AutoMappingDecision, ...],
) -> VerificationLevel:
    piece_mappings = tuple(
        V1FileMapping(
            mapping.torrent_path,
            mapping.state,
            Path(mapping.source_path) if mapping.source_path is not None else None,
        )
        for mapping in mappings
    )
    if meta.torrent_kind is TorrentKind.V1:
        return verify_v1_pieces(meta, piece_mappings).level
    if meta.torrent_kind is TorrentKind.V2:
        return verify_v2_files(meta, piece_mappings).level
    return verify_hybrid(meta, piece_mappings).level


def _candidate_key(candidate: CandidateMeta) -> str:
    return f"{candidate.site_id}\x1f{candidate.torrent_id}"


def _split_candidate_key(value: str) -> tuple[str, str]:
    site_id, torrent_id = value.split("\x1f", 1)
    return site_id, torrent_id


def _unverified_evidence(
    candidate: CandidateMeta,
    score: CandidateScore,
    *,
    selected: bool,
    error_code: str | None = None,
) -> CandidatePreflightEvidence:
    return CandidatePreflightEvidence(
        site_id=candidate.site_id,
        torrent_id=candidate.torrent_id,
        display_name=candidate.display_name,
        score=score,
        selected_for_verification=selected,
        metainfo_digest=None,
        verification_level=None,
        mappings=(),
        error_code=error_code,
    )
