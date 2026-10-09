from __future__ import annotations

import asyncio
import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.errors import ApplicationError
from backend.app.application.sites import EnabledSiteAdapter
from backend.app.domain.errors import DomainViolation, ErrorCode
from backend.app.domain.file_mapping import AutoMappingDecision, SourceFileCandidate, auto_map_files
from backend.app.domain.torrent import TorrentKind, TorrentMeta
from backend.app.domain.unpack import (
    UnpackCandidateVerificationStatus,
    UnpackExecutionStatus,
    UnpackItemStatus,
    item_transition_allowed,
)
from backend.app.domain.unpack_auxiliary import is_auxiliary_torrent_path
from backend.app.domain.verification import (
    FileMappingState,
    FileSnapshot,
    HybridVerificationResult,
    PieceStatus,
    TorrentVerificationResult,
    V1VerificationResult,
    V2VerificationResult,
    VerificationLevel,
)
from backend.app.infrastructure.adapters.site_errors import SiteAdapterError
from backend.app.infrastructure.authorized_paths import AuthorizedPathScope
from backend.app.infrastructure.persistence.database import begin_immediate_write
from backend.app.infrastructure.persistence.models import (
    UnpackExecution,
    UnpackExecutionItem,
    UnpackExternalOperationJournal,
    UnpackMatchCandidate,
    utc_now,
)
from backend.app.infrastructure.piece_verifier import (
    V1FileMapping,
    verify_hybrid,
    verify_v1_pieces,
    verify_v2_files,
)
from backend.app.infrastructure.source_inventory import current_file_snapshot
from backend.app.infrastructure.torrent_parser import parse_torrent


class UnpackContentSiteProvider(Protocol):
    def enabled_adapters(self) -> tuple[EnabledSiteAdapter, ...]: ...


@dataclass(frozen=True, slots=True)
class UnpackContentVerificationReport:
    execution_id: str
    processed_count: int
    content_verified_count: int
    content_mismatch_count: int
    auxiliary_pending_count: int
    review_count: int
    error_count: int
    execution_status: UnpackExecutionStatus


@dataclass(frozen=True, slots=True)
class _VerificationClaim:
    item_id: str
    item_version: int
    candidate_id: str
    generation: int
    site_config_id: str
    site_config_version: int
    adapter_site_id: str
    torrent_id: str
    source_path: str
    source_relative_path: str
    source_snapshot: dict[str, object]


@dataclass(frozen=True, slots=True)
class _VerificationOutcome:
    candidate_status: UnpackCandidateVerificationStatus
    verification_level: VerificationLevel
    item_status: UnpackItemStatus
    metainfo_digest: str
    error_code: str | None
    error_message: str | None
    evidence: dict[str, object]
    auxiliary_state: dict[str, object] | None


class UnpackContentVerificationService:
    """Unpack v2 只读 torrent 内容验证。此服务不写下载器，也不修改源文件。"""

    _MAX_AUTO_CANDIDATE_ATTEMPTS = 3

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        site_provider: UnpackContentSiteProvider,
        *,
        path_scope: AuthorizedPathScope,
        fetch_timeout_seconds: float = 30.0,
    ) -> None:
        if fetch_timeout_seconds <= 0:
            raise ValueError("torrent 获取超时必须大于 0")
        self._session_factory = session_factory
        self._site_provider = site_provider
        self._path_scope = path_scope
        self._fetch_timeout_seconds = fetch_timeout_seconds

    def list_verifiable_execution_ids(self, *, limit: int = 20) -> tuple[str, ...]:
        if limit < 1 or limit > 500:
            raise self._invalid("内容验证 execution 查询数量必须位于 1 到 500 之间")
        eligible_item = (
            select(UnpackExecutionItem.id)
            .where(UnpackExecutionItem.execution_id == UnpackExecution.id)
            .where(
                UnpackExecutionItem.status.in_(
                    (
                        UnpackItemStatus.MATCHED_AUTO.value,
                        UnpackItemStatus.MATCHED_MANUAL.value,
                    )
                )
            )
            .exists()
        )
        with self._session_factory() as session:
            return tuple(
                session.scalars(
                    select(UnpackExecution.id)
                    .where(
                        UnpackExecution.status.in_(
                            (
                                UnpackExecutionStatus.CONTENT_VERIFYING.value,
                                UnpackExecutionStatus.REVIEW_REQUIRED.value,
                            )
                        )
                    )
                    .where(eligible_item)
                    .order_by(UnpackExecution.updated_at, UnpackExecution.id)
                    .limit(limit)
                ).all()
            )

    async def verify_next_batch(
        self,
        execution_id: str,
        *,
        limit: int = 5,
    ) -> UnpackContentVerificationReport:
        if limit < 1 or limit > 100:
            raise self._invalid("内容验证批次必须位于 1 到 100 之间")
        processed = 0
        for _ in range(limit):
            claim = self._claim_next_item(execution_id)
            if claim is None:
                break
            await self._verify_claim(claim)
            processed += 1
        return self._report(execution_id, processed_count=processed)

    def _claim_next_item(self, execution_id: str) -> _VerificationClaim | None:
        with self._session_factory() as session:
            begin_immediate_write(session)
            execution = session.get(UnpackExecution, execution_id)
            if execution is None:
                raise self._not_found()
            if execution.status not in {
                UnpackExecutionStatus.CONTENT_VERIFYING.value,
                UnpackExecutionStatus.REVIEW_REQUIRED.value,
            }:
                return None

            item = session.scalar(
                select(UnpackExecutionItem)
                .where(UnpackExecutionItem.execution_id == execution_id)
                .where(
                    UnpackExecutionItem.status.in_(
                        (
                            UnpackItemStatus.MATCHED_AUTO.value,
                            UnpackItemStatus.MATCHED_MANUAL.value,
                        )
                    )
                )
                .order_by(UnpackExecutionItem.id)
                .limit(1)
            )
            if item is None:
                self._refresh_execution_state(session, execution)
                session.commit()
                return None
            if item.selected_candidate_id is None:
                self._mark_claim_error(
                    session,
                    execution,
                    item,
                    code="UNPACK_VERIFY_CANDIDATE_MISSING",
                    message="已匹配影片缺少选中候选",
                )
                return None

            candidate = session.get(UnpackMatchCandidate, item.selected_candidate_id)
            if (
                candidate is None
                or candidate.item_id != item.id
                or candidate.generation != item.candidate_generation
            ):
                self._mark_claim_error(
                    session,
                    execution,
                    item,
                    code="UNPACK_VERIFY_CANDIDATE_STALE",
                    message="选中候选不属于当前候选代次",
                )
                return None

            raw_ref = dict(candidate.raw_ref)
            adapter_site_id = raw_ref.get("adapter_site_id")
            torrent_id = raw_ref.get("torrent_id")
            config_version = raw_ref.get("site_config_version")
            if (
                not isinstance(adapter_site_id, str)
                or not adapter_site_id
                or not isinstance(torrent_id, str)
                or not torrent_id
                or not isinstance(config_version, int)
                or isinstance(config_version, bool)
                or config_version < 1
            ):
                self._mark_claim_error(
                    session,
                    execution,
                    item,
                    code="UNPACK_VERIFY_CANDIDATE_REF_INVALID",
                    message="候选缺少安全站点引用",
                )
                return None

            source = dict(item.source_snapshot)
            source_path = source.get("path")
            relative_path = source.get("relative_path")
            if not isinstance(source_path, str) or not source_path:
                self._mark_claim_error(
                    session,
                    execution,
                    item,
                    code="UNPACK_VERIFY_SOURCE_INVALID",
                    message="影片项缺少来源路径快照",
                )
                return None
            if not isinstance(relative_path, str) or not relative_path:
                relative_path = PurePosixPath(source_path.replace("\\", "/")).name

            now = utc_now()
            item.status = UnpackItemStatus.TORRENT_FETCHING.value
            item.updated_at = now
            item.version += 1
            candidate.verification_status = UnpackCandidateVerificationStatus.VERIFYING.value
            candidate.verification_error_code = None
            session.commit()
            return _VerificationClaim(
                item_id=item.id,
                item_version=item.version,
                candidate_id=candidate.id,
                generation=item.candidate_generation,
                site_config_id=candidate.site_id,
                site_config_version=config_version,
                adapter_site_id=adapter_site_id,
                torrent_id=torrent_id,
                source_path=source_path,
                source_relative_path=relative_path,
                source_snapshot=source,
            )

    async def _verify_claim(self, claim: _VerificationClaim) -> None:
        try:
            binding = self._binding_for(claim)
            source = self._current_source(claim)
            async with asyncio.timeout(self._fetch_timeout_seconds):
                payload = await binding.adapter.fetch_torrent(claim.torrent_id)
            if payload.site_id != claim.adapter_site_id or payload.torrent_id != claim.torrent_id:
                raise ApplicationError(
                    code="UNPACK_VERIFY_TORRENT_IDENTITY_MISMATCH",
                    status=409,
                    title="候选 torrent 身份不一致",
                    detail="站点返回的 torrent 身份与选中候选不一致",
                )
            meta = parse_torrent(payload.content)
            # A torrent may contain several existing movie files in the same
            # authorized directory, even though discovery created one task
            # item for just one video. Inspect only direct regular siblings
            # named in this torrent; never recursively scan the media library.
            source = self._current_source(claim)
            candidates = self._source_candidates_for_torrent(source, meta)
            mappings = auto_map_files(meta, candidates)
            phase = (
                UnpackItemStatus.AUXILIARY_FETCHING
                if _only_auxiliary_missing(mappings)
                else UnpackItemStatus.CONTENT_VERIFYING
            )
            self._mark_verification_phase(claim, phase)
            verification = await asyncio.to_thread(_verify_torrent, meta, mappings)
            # Never persist FULL_VERIFIED if a media file changed during the
            # potentially lengthy torrent download or piece hashing.
            self._current_source(claim)
            for mapping in mappings:
                if mapping.source_path is None or mapping.snapshot is None:
                    continue
                if current_file_snapshot(Path(mapping.source_path)) != mapping.snapshot:
                    raise ApplicationError(
                        code="SOURCE_SNAPSHOT_CHANGED",
                        status=409,
                        title="来源文件已变化",
                        detail="内容校验期间来源目录文件已变化，请重新匹配",
                    )
            outcome = _build_outcome(meta, mappings, verification)
        except TimeoutError:
            self._finalize_failure(
                claim,
                code="UNPACK_VERIFY_TORRENT_FETCH_TIMEOUT",
                message="获取候选 torrent 超时，可稍后重试",
            )
            return
        except SiteAdapterError as exc:
            self._finalize_failure(
                claim,
                code=exc.code,
                message="获取候选 torrent 失败，可稍后重试",
            )
            return
        except DomainViolation as exc:
            self._finalize_failure(
                claim,
                code=exc.code.value,
                message="本地内容验证安全检查未通过",
            )
            return
        except ApplicationError as exc:
            self._finalize_failure(claim, code=exc.code, message=exc.detail)
            return
        try:
            self._finalize_outcome(claim, outcome)
        except ApplicationError as exc:
            if exc.code == "UNPACK_CONTENT_VERIFY_CONFLICT":
                return
            raise

    def _mark_verification_phase(
        self,
        claim: _VerificationClaim,
        phase: UnpackItemStatus,
    ) -> None:
        if phase not in {
            UnpackItemStatus.AUXILIARY_FETCHING,
            UnpackItemStatus.CONTENT_VERIFYING,
        }:
            raise ValueError("内容验证阶段无效")
        with self._session_factory() as session:
            begin_immediate_write(session)
            item, _candidate, execution = self._require_claim_current(session, claim)
            item.status = phase.value
            item.updated_at = utc_now()
            item.version += 1
            session.flush()
            self._refresh_execution_state(session, execution)
            session.commit()

    def _binding_for(self, claim: _VerificationClaim) -> EnabledSiteAdapter:
        binding = next(
            (
                item
                for item in self._site_provider.enabled_adapters()
                if item.config_id == claim.site_config_id
            ),
            None,
        )
        if binding is None:
            raise ApplicationError(
                code="UNPACK_VERIFY_SITE_UNAVAILABLE",
                status=409,
                title="候选站点不可用",
                detail="候选对应站点当前未启用",
            )
        if (
            binding.config_version != claim.site_config_version
            or binding.site_id != claim.adapter_site_id
        ):
            raise ApplicationError(
                code="UNPACK_VERIFY_SITE_CONFIG_CHANGED",
                status=409,
                title="候选站点配置已变化",
                detail="候选生成后的站点版本或站点身份已变化，请重新匹配",
            )
        return binding

    def _current_source(self, claim: _VerificationClaim) -> SourceFileCandidate:
        path = Path(self._path_scope.normalize_reference(claim.source_path))
        self._path_scope.resolve_existing_directory(path.parent.as_posix())
        observed = current_file_snapshot(path)
        if not _snapshot_matches(claim.source_snapshot, observed):
            raise ApplicationError(
                code="SOURCE_SNAPSHOT_CHANGED",
                status=409,
                title="来源文件已变化",
                detail="匹配后来源影片的 inode、大小或修改时间已变化，请重新匹配",
            )
        return SourceFileCandidate(
            relative_path=claim.source_relative_path,
            source_path=path.as_posix(),
            length=observed.size,
            snapshot=observed,
        )

    def _source_candidates_for_torrent(
        self, source: SourceFileCandidate, meta: TorrentMeta
    ) -> tuple[SourceFileCandidate, ...]:
        """Bounded, same-directory, read-only lookup for torrent file basenames."""
        source_path = Path(source.source_path)
        self._path_scope.resolve_existing_directory(source_path.parent.as_posix())
        lengths_by_name: dict[str, set[int]] = {}
        for torrent_file in meta.files:
            if torrent_file.padding or torrent_file.zero_length:
                continue
            basename = PurePosixPath(torrent_file.path).name
            lengths_by_name.setdefault(basename, set()).add(torrent_file.length)

        siblings: list[SourceFileCandidate] = [source]
        try:
            with os.scandir(source_path.parent) as entries:
                for index, entry in enumerate(entries):
                    if index >= 2048:
                        raise DomainViolation(
                            ErrorCode.TORRENT_LIMIT_EXCEEDED,
                            "影片同级目录条目数超出安全扫描上限",
                        )
                    if entry.name == source_path.name or entry.name not in lengths_by_name:
                        continue
                    observed = entry.stat(follow_symlinks=False)
                    if not stat.S_ISREG(observed.st_mode):
                        continue
                    if observed.st_size not in lengths_by_name[entry.name]:
                        continue
                    child_path = source_path.parent / entry.name
                    snapshot = current_file_snapshot(child_path)
                    siblings.append(
                        SourceFileCandidate(
                            relative_path=entry.name,
                            source_path=child_path.as_posix(),
                            length=snapshot.size,
                            snapshot=snapshot,
                        )
                    )
        except OSError as exc:
            raise DomainViolation(
                ErrorCode.PATH_MAPPING_INVALID, "无法安全读取来源影片所在目录"
            ) from exc
        return tuple(sorted(siblings, key=lambda item: (item.relative_path, item.source_path)))

    def _finalize_outcome(
        self,
        claim: _VerificationClaim,
        outcome: _VerificationOutcome,
    ) -> None:
        with self._session_factory() as session:
            begin_immediate_write(session)
            item, candidate, execution = self._require_claim_current(
                session,
                claim,
                allowed_statuses={
                    UnpackItemStatus.TORRENT_FETCHING,
                    UnpackItemStatus.AUXILIARY_FETCHING,
                    UnpackItemStatus.CONTENT_VERIFYING,
                },
            )
            now = utc_now()
            candidate.verification_status = outcome.candidate_status.value
            candidate.verification_level = outcome.verification_level.value
            candidate.verification_error_code = outcome.error_code
            candidate.metainfo_digest = outcome.metainfo_digest
            candidate.evidence = {
                **dict(candidate.evidence),
                "content_verification": outcome.evidence,
            }
            if (
                outcome.item_status is UnpackItemStatus.CONTENT_MISMATCH
                and outcome.error_code
                in {"UNPACK_CONTENT_MISMATCH", "UNPACK_CONTENT_REQUIRED_FILE_MISSING"}
                and self._select_auto_fallback(session, item, candidate, execution)
            ):
                item.content_verification_level = None
                item.torrent_metainfo_digest = None
                item.auxiliary_state = None
                item.last_error_code = None
                item.last_error_message = None
                item.updated_at = now
                item.version += 1
                session.flush()
                self._refresh_execution_state(session, execution)
                session.commit()
                return
            if item.status == UnpackItemStatus.AUXILIARY_FETCHING.value and outcome.item_status in {
                UnpackItemStatus.CONTENT_VERIFIED,
                UnpackItemStatus.CONTENT_MISMATCH,
                UnpackItemStatus.REVIEW_REQUIRED,
            }:
                item.status = UnpackItemStatus.CONTENT_VERIFYING.value
                session.flush()
            item.status = outcome.item_status.value
            item.content_verification_level = outcome.verification_level.value
            item.torrent_metainfo_digest = outcome.metainfo_digest
            item.auxiliary_state = outcome.auxiliary_state
            item.last_error_code = outcome.error_code
            item.last_error_message = outcome.error_message
            item.updated_at = now
            item.version += 1
            session.flush()
            self._refresh_execution_state(session, execution)
            session.commit()

    def _select_auto_fallback(
        self,
        session: Session,
        item: UnpackExecutionItem,
        failed: UnpackMatchCandidate,
        execution: UnpackExecution,
    ) -> bool:
        """Select at most three auto-proposed candidates in one generation."""
        if item.match_origin != "AUTO" or not item_transition_allowed(
            UnpackItemStatus(item.status), UnpackItemStatus.MATCHED_AUTO
        ):
            return False
        # A prior auxiliary downloader request or materialization may already
        # have side effects. Do not silently change torrents after that point.
        if (
            session.scalar(
                select(UnpackExternalOperationJournal.id)
                .where(UnpackExternalOperationJournal.item_id == item.id)
                .limit(1)
            )
            is not None
        ):
            return False
        snapshot = execution.config_snapshot
        matching = snapshot.get("matching") if isinstance(snapshot, dict) else None
        threshold = matching.get("auto_match_threshold_bps") if isinstance(matching, dict) else None
        if (
            not isinstance(threshold, int)
            or isinstance(threshold, bool)
            or not 0 <= threshold <= 10000
        ):
            return False
        generation = session.scalars(
            select(UnpackMatchCandidate)
            .where(UnpackMatchCandidate.item_id == item.id)
            .where(UnpackMatchCandidate.generation == item.candidate_generation)
            .order_by(UnpackMatchCandidate.score_bps.desc(), UnpackMatchCandidate.id)
        ).all()
        attempts = sum(
            candidate.verification_status
            in {
                UnpackCandidateVerificationStatus.MISMATCH.value,
                UnpackCandidateVerificationStatus.UNAVAILABLE.value,
                UnpackCandidateVerificationStatus.VERIFYING.value,
                UnpackCandidateVerificationStatus.VERIFIED.value,
            }
            for candidate in generation
        )
        if attempts >= self._MAX_AUTO_CANDIDATE_ATTEMPTS:
            return False
        for candidate in generation:
            if (
                candidate.id == failed.id
                or candidate.verification_status
                != UnpackCandidateVerificationStatus.NOT_CHECKED.value
                or candidate.score_bps < threshold
            ):
                continue
            evidence = candidate.evidence
            if not isinstance(evidence, dict) or evidence.get("hard_conflicts") != []:
                continue
            exact = evidence.get("exact")
            if (
                not isinstance(exact, dict)
                or exact.get("title") is not True
                or exact.get("size") is not True
            ):
                continue
            ref = candidate.raw_ref
            if (
                not isinstance(ref, dict)
                or ref.get("site_config_id") != candidate.site_id
                or not isinstance(ref.get("torrent_id"), str)
                or not ref["torrent_id"]
                or not isinstance(ref.get("adapter_site_id"), str)
                or not ref["adapter_site_id"]
                or type(ref.get("site_config_version")) is not int
                or ref["site_config_version"] < 1
            ):
                continue
            item.selected_candidate_id = candidate.id
            item.status = UnpackItemStatus.MATCHED_AUTO.value
            return True
        return False

    def _finalize_failure(
        self,
        claim: _VerificationClaim,
        *,
        code: str,
        message: str,
    ) -> None:
        with self._session_factory() as session:
            begin_immediate_write(session)
            try:
                item, candidate, execution = self._require_claim_current(
                    session,
                    claim,
                    allowed_statuses={
                        UnpackItemStatus.TORRENT_FETCHING,
                        UnpackItemStatus.AUXILIARY_FETCHING,
                        UnpackItemStatus.CONTENT_VERIFYING,
                    },
                )
            except ApplicationError:
                return
            now = utc_now()
            candidate.verification_status = UnpackCandidateVerificationStatus.UNAVAILABLE.value
            candidate.verification_level = None
            candidate.verification_error_code = code
            item.status = (
                UnpackItemStatus.REVIEW_REQUIRED.value
                if item.status == UnpackItemStatus.CONTENT_VERIFYING.value
                else UnpackItemStatus.MATCH_ERROR.value
            )
            item.content_verification_level = None
            item.last_error_code = code
            item.last_error_message = message
            item.updated_at = now
            item.version += 1
            session.flush()
            self._refresh_execution_state(session, execution)
            session.commit()

    def _require_claim_current(
        self,
        session: Session,
        claim: _VerificationClaim,
        *,
        allowed_statuses: set[UnpackItemStatus] | None = None,
    ) -> tuple[UnpackExecutionItem, UnpackMatchCandidate, UnpackExecution]:
        item = session.get(UnpackExecutionItem, claim.item_id)
        candidate = session.get(UnpackMatchCandidate, claim.candidate_id)
        statuses = allowed_statuses or {UnpackItemStatus.TORRENT_FETCHING}
        if (
            item is None
            or candidate is None
            or item.status not in {status.value for status in statuses}
            or item.candidate_generation != claim.generation
            or item.selected_candidate_id != candidate.id
            or candidate.item_id != item.id
        ):
            raise self._conflict("内容验证结果已过期，拒绝覆盖较新的影片状态")
        execution = session.get(UnpackExecution, item.execution_id)
        if execution is None:
            raise self._not_found()
        return item, candidate, execution

    def _mark_claim_error(
        self,
        session: Session,
        execution: UnpackExecution,
        item: UnpackExecutionItem,
        *,
        code: str,
        message: str,
    ) -> None:
        now = utc_now()
        if item.status == UnpackItemStatus.MATCHED_MANUAL.value:
            item.status = UnpackItemStatus.TORRENT_FETCHING.value
            item.updated_at = now
            item.version += 1
            session.flush()
            item.status = UnpackItemStatus.MATCH_ERROR.value
        else:
            item.status = UnpackItemStatus.REVIEW_REQUIRED.value
        item.last_error_code = code
        item.last_error_message = message
        item.updated_at = now
        item.version += 1
        session.flush()
        self._refresh_execution_state(session, execution)
        session.commit()

    @staticmethod
    def _refresh_execution_state(session: Session, execution: UnpackExecution) -> None:
        statuses = (
            UnpackItemStatus.MATCHED_AUTO,
            UnpackItemStatus.MATCHED_MANUAL,
            UnpackItemStatus.TORRENT_FETCHING,
            UnpackItemStatus.AUXILIARY_FETCHING,
            UnpackItemStatus.CONTENT_VERIFYING,
            UnpackItemStatus.CONTENT_VERIFIED,
            UnpackItemStatus.CONTENT_MISMATCH,
            UnpackItemStatus.REVIEW_REQUIRED,
            UnpackItemStatus.MATCH_TIMEOUT,
            UnpackItemStatus.MATCH_ERROR,
            UnpackItemStatus.NO_MATCH,
        )
        counts = {
            status: int(
                session.scalar(
                    select(func.count(UnpackExecutionItem.id))
                    .where(UnpackExecutionItem.execution_id == execution.id)
                    .where(UnpackExecutionItem.status == status.value)
                )
                or 0
            )
            for status in statuses
        }
        execution.content_verified_count = counts[UnpackItemStatus.CONTENT_VERIFIED]
        execution.content_mismatch_count = counts[UnpackItemStatus.CONTENT_MISMATCH]
        execution.review_count = counts[UnpackItemStatus.REVIEW_REQUIRED]
        execution.timeout_count = counts[UnpackItemStatus.MATCH_TIMEOUT]
        execution.error_count = counts[UnpackItemStatus.MATCH_ERROR]
        active_content = sum(
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
        now = utc_now()
        if active_content:
            execution.status = UnpackExecutionStatus.CONTENT_VERIFYING.value
            execution.finished_at = None
        elif counts[UnpackItemStatus.REVIEW_REQUIRED]:
            execution.status = UnpackExecutionStatus.REVIEW_REQUIRED.value
            execution.finished_at = None
        elif (
            counts[UnpackItemStatus.CONTENT_MISMATCH]
            or counts[UnpackItemStatus.MATCH_TIMEOUT]
            or counts[UnpackItemStatus.MATCH_ERROR]
            or counts[UnpackItemStatus.NO_MATCH]
        ):
            execution.status = UnpackExecutionStatus.COMPLETED_WITH_ERRORS.value
            execution.finished_at = now
        execution.updated_at = now
        execution.version += 1

    def _report(
        self,
        execution_id: str,
        *,
        processed_count: int,
    ) -> UnpackContentVerificationReport:
        with self._session_factory() as session:
            execution = session.get(UnpackExecution, execution_id)
            if execution is None:
                raise self._not_found()

            def count(status: UnpackItemStatus) -> int:
                return int(
                    session.scalar(
                        select(func.count(UnpackExecutionItem.id))
                        .where(UnpackExecutionItem.execution_id == execution_id)
                        .where(UnpackExecutionItem.status == status.value)
                    )
                    or 0
                )

            return UnpackContentVerificationReport(
                execution_id=execution_id,
                processed_count=processed_count,
                content_verified_count=count(UnpackItemStatus.CONTENT_VERIFIED),
                content_mismatch_count=count(UnpackItemStatus.CONTENT_MISMATCH),
                auxiliary_pending_count=count(UnpackItemStatus.AUXILIARY_FETCHING),
                review_count=count(UnpackItemStatus.REVIEW_REQUIRED),
                error_count=count(UnpackItemStatus.MATCH_ERROR),
                execution_status=UnpackExecutionStatus(execution.status),
            )

    @staticmethod
    def _invalid(detail: str) -> ApplicationError:
        return ApplicationError(
            code="UNPACK_CONTENT_VERIFY_INVALID",
            status=422,
            title="内容验证参数无效",
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
    def _conflict(detail: str) -> ApplicationError:
        return ApplicationError(
            code="UNPACK_CONTENT_VERIFY_CONFLICT",
            status=409,
            title="内容验证状态冲突",
            detail=detail,
        )


def _verify_torrent(
    meta: TorrentMeta,
    mappings: tuple[AutoMappingDecision, ...],
) -> TorrentVerificationResult:
    piece_mappings = tuple(
        V1FileMapping(
            mapping.torrent_path,
            mapping.state,
            Path(mapping.source_path) if mapping.source_path is not None else None,
        )
        for mapping in mappings
    )
    if meta.torrent_kind is TorrentKind.V1:
        return verify_v1_pieces(meta, piece_mappings)
    if meta.torrent_kind is TorrentKind.V2:
        return verify_v2_files(meta, piece_mappings)
    return verify_hybrid(meta, piece_mappings)


def _build_outcome(
    meta: TorrentMeta,
    mappings: tuple[AutoMappingDecision, ...],
    verification: TorrentVerificationResult,
) -> _VerificationOutcome:
    missing_auxiliary: list[str] = []
    missing_required: list[str] = []
    ambiguous: list[str] = []
    mapped = 0
    for mapping in mappings:
        if mapping.state is FileMappingState.MAPPED:
            mapped += 1
        elif mapping.state is FileMappingState.AMBIGUOUS:
            ambiguous.append(mapping.torrent_path)
        elif mapping.state is FileMappingState.MISSING:
            if is_auxiliary_torrent_path(mapping.torrent_path):
                missing_auxiliary.append(mapping.torrent_path)
            else:
                missing_required.append(mapping.torrent_path)

    mismatch = _has_mismatch(verification)
    verifier_level = verification.level
    evidence: dict[str, object] = {
        "torrent_kind": meta.torrent_kind.value,
        "verifier_level": verifier_level.value,
        "mapped_count": mapped,
        "missing_auxiliary": missing_auxiliary,
        "missing_required": missing_required,
        "ambiguous": ambiguous,
        "piece_mismatch": mismatch,
        "mappings": [
            {
                "torrent_path": item.torrent_path,
                "state": item.state.value,
                "method": item.method.value,
                "source_path": item.source_path,
                "snapshot": (
                    {
                        "device": item.snapshot.device,
                        "inode": item.snapshot.inode,
                        "size": item.snapshot.size,
                        "mtime_ns": str(item.snapshot.mtime_ns),
                        "file_type": item.snapshot.file_type,
                    }
                    if item.snapshot is not None
                    else None
                ),
            }
            for item in mappings
        ],
    }
    if mapped == 0:
        missing_required.append("<source-not-mapped>")
        evidence["missing_required"] = missing_required

    if mismatch:
        return _VerificationOutcome(
            candidate_status=UnpackCandidateVerificationStatus.MISMATCH,
            verification_level=VerificationLevel.BLOCKED,
            item_status=UnpackItemStatus.CONTENT_MISMATCH,
            metainfo_digest=meta.metainfo_digest,
            error_code="UNPACK_CONTENT_MISMATCH",
            error_message="本地影片内容与候选 torrent 校验数据不一致",
            evidence=evidence,
            auxiliary_state=None,
        )
    if missing_required or ambiguous or verifier_level is VerificationLevel.BLOCKED:
        return _VerificationOutcome(
            candidate_status=UnpackCandidateVerificationStatus.UNAVAILABLE,
            verification_level=VerificationLevel.BLOCKED,
            item_status=UnpackItemStatus.CONTENT_MISMATCH,
            metainfo_digest=meta.metainfo_digest,
            error_code="UNPACK_CONTENT_REQUIRED_FILE_MISSING",
            error_message="候选 torrent 存在缺失或歧义的非辅助文件，禁止自动辅种",
            evidence=evidence,
            auxiliary_state=None,
        )
    if missing_auxiliary:
        return _VerificationOutcome(
            candidate_status=UnpackCandidateVerificationStatus.UNAVAILABLE,
            verification_level=VerificationLevel.CLIENT_CHECK_REQUIRED,
            item_status=UnpackItemStatus.AUXILIARY_FETCHING,
            metainfo_digest=meta.metainfo_digest,
            error_code="UNPACK_AUXILIARY_FILES_REQUIRED",
            error_message="主影片未发现内容冲突，但候选 torrent 还缺少辅助文件",
            evidence=evidence,
            auxiliary_state={
                "state": "PENDING_FETCH",
                "missing_paths": missing_auxiliary,
            },
        )
    if verifier_level is VerificationLevel.FULL_VERIFIED:
        return _VerificationOutcome(
            candidate_status=UnpackCandidateVerificationStatus.VERIFIED,
            verification_level=VerificationLevel.FULL_VERIFIED,
            item_status=UnpackItemStatus.CONTENT_VERIFIED,
            metainfo_digest=meta.metainfo_digest,
            error_code=None,
            error_message=None,
            evidence=evidence,
            auxiliary_state=None,
        )
    return _VerificationOutcome(
        candidate_status=UnpackCandidateVerificationStatus.UNAVAILABLE,
        verification_level=VerificationLevel.CLIENT_CHECK_REQUIRED,
        item_status=UnpackItemStatus.REVIEW_REQUIRED,
        metainfo_digest=meta.metainfo_digest,
        error_code="UNPACK_CONTENT_CLIENT_CHECK_REQUIRED",
        error_message="现有本地证据不足以完整证明候选内容一致，需要人工处理",
        evidence=evidence,
        auxiliary_state=None,
    )


def _only_auxiliary_missing(mappings: tuple[AutoMappingDecision, ...]) -> bool:
    has_auxiliary = False
    mapped = False
    for mapping in mappings:
        if mapping.state is FileMappingState.MAPPED:
            mapped = True
            continue
        if mapping.state in {FileMappingState.PADDING, FileMappingState.ZERO_LENGTH}:
            continue
        if mapping.state is FileMappingState.AMBIGUOUS:
            return False
        if mapping.state is FileMappingState.MISSING:
            if not is_auxiliary_torrent_path(mapping.torrent_path):
                return False
            has_auxiliary = True
    return mapped and has_auxiliary


def _has_mismatch(result: TorrentVerificationResult) -> bool:
    if isinstance(result, V1VerificationResult):
        return any(piece.status is PieceStatus.MISMATCH for piece in result.pieces)
    if isinstance(result, V2VerificationResult):
        return any(
            file.status is PieceStatus.MISMATCH
            or any(piece.status is PieceStatus.MISMATCH for piece in file.pieces)
            for file in result.files
        )
    if isinstance(result, HybridVerificationResult):
        return _has_mismatch(result.v1) or _has_mismatch(result.v2)
    raise TypeError(type(result).__name__)


def _snapshot_matches(expected: dict[str, object], observed: FileSnapshot) -> bool:
    fields = ("device", "inode", "size", "file_type")
    if any(expected.get(field) != getattr(observed, field) for field in fields):
        return False
    return str(expected.get("mtime_ns")) == str(observed.mtime_ns)
