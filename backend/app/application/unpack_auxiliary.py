from __future__ import annotations

import asyncio
import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol, cast

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.downloaders import (
    QbittorrentWriteBinding,
    TransmissionWriteBinding,
)
from backend.app.application.errors import ApplicationError
from backend.app.application.sites import EnabledSiteAdapter
from backend.app.domain.errors import DomainViolation
from backend.app.domain.file_mapping import AutoMappingDecision, SourceFileCandidate, auto_map_files
from backend.app.domain.operation import OperationStatus
from backend.app.domain.torrent import TorrentKind, TorrentMeta
from backend.app.domain.unpack import (
    UnpackCandidateVerificationStatus,
    UnpackExecutionStatus,
    UnpackItemStatus,
)
from backend.app.domain.unpack_auxiliary import (
    AuxiliaryFetchPlan,
    build_auxiliary_fetch_plan,
    torrent_client_hash,
)
from backend.app.domain.verification import (
    FileSnapshot,
    HybridVerificationResult,
    PieceStatus,
    TorrentVerificationResult,
    V1VerificationResult,
    V2VerificationResult,
    VerificationLevel,
)
from backend.app.infrastructure.adapters.downloaders import (
    DownloaderAdapterError,
    DownloaderFileSelectionAdapter,
    QbittorrentAddRequest,
    QbittorrentTorrentState,
    TransmissionAddRequest,
    TransmissionTorrentState,
)
from backend.app.infrastructure.adapters.site_errors import SiteAdapterError
from backend.app.infrastructure.authorized_paths import AuthorizedPathScope
from backend.app.infrastructure.persistence.database import begin_immediate_write
from backend.app.infrastructure.persistence.models import (
    UnpackExecution,
    UnpackExecutionItem,
    UnpackExternalOperationJournal,
    UnpackMatchCandidate,
    new_uuid,
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

_AUX_DIR_OPERATION = "UNPACK_AUX_STAGING_DIR"
_AUX_ADD_OPERATION = "UNPACK_AUX_TORRENT_ADD"
_AUX_SELECT_OPERATION = "UNPACK_AUX_FILE_SELECTION"
_AUX_START_OPERATION = "UNPACK_AUX_TORRENT_START"
_AUX_STOP_OPERATION = "UNPACK_AUX_TORRENT_STOP"
_AUX_REMOVE_OPERATION = "UNPACK_AUX_TORRENT_REMOVE"
_AUX_SCHEMA_VERSION = "packbreaker-unpack-aux-v1"


class UnpackAuxiliarySiteProvider(Protocol):
    def enabled_adapters(self) -> tuple[EnabledSiteAdapter, ...]: ...


class UnpackAuxiliaryDownloaderProvider(Protocol):
    def write_binding(
        self,
        downloader_id: str,
    ) -> QbittorrentWriteBinding | TransmissionWriteBinding: ...


@dataclass(frozen=True, slots=True)
class UnpackAuxiliaryReport:
    execution_id: str
    processed_count: int
    downloading_count: int
    verified_count: int
    mismatch_count: int
    error_count: int
    execution_status: UnpackExecutionStatus


@dataclass(frozen=True, slots=True)
class _Context:
    execution_id: str
    item_id: str
    candidate_id: str
    generation: int
    source_snapshot: dict[str, Any]
    source_relative_path: str
    site_config_id: str
    site_config_version: int
    adapter_site_id: str
    torrent_id: str
    expected_metainfo_digest: str
    target_downloader_id: str
    output_directory: str
    missing_paths: tuple[str, ...]
    auxiliary_state: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _Prepared:
    context: _Context
    binding: QbittorrentWriteBinding | TransmissionWriteBinding
    meta: TorrentMeta
    payload: bytes
    plan: AuxiliaryFetchPlan
    torrent_hash: str
    staging_path: Path
    remote_save_path: str
    ownership_tag: str


class UnpackAuxiliaryStagingService:
    """辅助文件 staging 编排。

    所有下载器/文件系统写操作都先持久化 v2 journal intent。源影片从不链接或复制
    到 staging；完整验证时只读组合源影片与 staging 中的辅助文件。
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        site_provider: UnpackAuxiliarySiteProvider,
        downloader_provider: UnpackAuxiliaryDownloaderProvider,
        *,
        path_scope: AuthorizedPathScope,
        fetch_timeout_seconds: float = 30.0,
    ) -> None:
        if fetch_timeout_seconds <= 0:
            raise ValueError("辅助文件 torrent 获取超时必须大于 0")
        self._session_factory = session_factory
        self._site_provider = site_provider
        self._downloader_provider = downloader_provider
        self._path_scope = path_scope
        self._fetch_timeout_seconds = fetch_timeout_seconds

    def list_auxiliary_execution_ids(self, *, limit: int = 20) -> tuple[str, ...]:
        if limit < 1 or limit > 500:
            raise self._invalid("辅助文件 execution 查询数量必须位于 1 到 500 之间")
        with self._session_factory() as session:
            return tuple(
                session.scalars(
                    select(UnpackExecution.id)
                    .where(UnpackExecution.status == UnpackExecutionStatus.CONTENT_VERIFYING.value)
                    .where(
                        select(UnpackExecutionItem.id)
                        .where(UnpackExecutionItem.execution_id == UnpackExecution.id)
                        .where(
                            UnpackExecutionItem.status == UnpackItemStatus.AUXILIARY_FETCHING.value
                        )
                        .exists()
                    )
                    .order_by(UnpackExecution.updated_at, UnpackExecution.id)
                    .limit(limit)
                ).all()
            )

    async def advance_next_batch(
        self,
        execution_id: str,
        *,
        limit: int = 2,
    ) -> UnpackAuxiliaryReport:
        if limit < 1 or limit > 50:
            raise self._invalid("辅助文件批次必须位于 1 到 50 之间")
        item_ids = self._list_item_ids(execution_id, limit=limit)
        processed = 0
        for item_id in item_ids:
            await self._advance_item(item_id)
            processed += 1
        return self._report(execution_id, processed_count=processed)

    def _list_item_ids(self, execution_id: str, *, limit: int) -> tuple[str, ...]:
        with self._session_factory() as session:
            execution = session.get(UnpackExecution, execution_id)
            if execution is None:
                raise self._not_found()
            return tuple(
                session.scalars(
                    select(UnpackExecutionItem.id)
                    .where(UnpackExecutionItem.execution_id == execution_id)
                    .where(UnpackExecutionItem.status == UnpackItemStatus.AUXILIARY_FETCHING.value)
                    .order_by(UnpackExecutionItem.id)
                    .limit(limit)
                ).all()
            )

    async def _advance_item(self, item_id: str) -> None:
        try:
            context = self._load_context(item_id)
            prepared = await self._prepare(context)
            state = str(context.auxiliary_state.get("state") or "PENDING_FETCH")
            if state in {"PENDING_FETCH", "PREPARING"}:
                self._set_auxiliary_state(
                    context.item_id,
                    {
                        **context.auxiliary_state,
                        "state": "PREPARING",
                    },
                )
                await self._ensure_staging_ready(prepared)
                self._set_auxiliary_state(
                    context.item_id,
                    _downloading_state(prepared),
                )
                return
            if state == "DOWNLOADING":
                await self._poll_downloading(prepared)
                return
            if state == "READY_FOR_FINALIZATION":
                return
            raise self._conflict("辅助文件状态无法继续")
        except DownloaderAdapterError as exc:
            if exc.code in {"DOWNLOADER_UNAVAILABLE", "DOWNLOADER_CONNECTION_FAILED"}:
                # Polling is read-only until the auxiliary files are present.
                # Preserve the bound torrent and journal across transient RPC
                # outages; a later worker pass must verify ownership again.
                return
            code, message = _safe_error(exc)
            self._mark_error(item_id, code=code, message=message)
        except SiteAdapterError as exc:
            if exc.retryable or exc.code in {"SITE_UNAVAILABLE", "SITE_RATE_LIMITED"}:
                # The original selected candidate and external journal must
                # survive transient tracker outages during staging polling.
                return
            code, message = _safe_error(exc)
            self._mark_error(item_id, code=code, message=message)
        except (ApplicationError, DomainViolation) as exc:
            code, message = _safe_error(exc)
            self._mark_error(item_id, code=code, message=message)

    def _load_context(self, item_id: str) -> _Context:
        with self._session_factory() as session:
            item = session.get(UnpackExecutionItem, item_id)
            if item is None:
                raise self._item_not_found()
            if item.status != UnpackItemStatus.AUXILIARY_FETCHING.value:
                raise self._conflict("影片项当前不处于辅助文件补齐阶段")
            execution = session.get(UnpackExecution, item.execution_id)
            if execution is None:
                raise self._not_found()
            if item.selected_candidate_id is None:
                raise self._conflict("辅助文件补齐缺少选中候选")
            candidate = session.get(UnpackMatchCandidate, item.selected_candidate_id)
            if (
                candidate is None
                or candidate.item_id != item.id
                or candidate.generation != item.candidate_generation
            ):
                raise self._conflict("辅助文件补齐候选已过期")
            raw_ref = dict(candidate.raw_ref)
            site_config_version = raw_ref.get("site_config_version")
            adapter_site_id = raw_ref.get("adapter_site_id")
            torrent_id = raw_ref.get("torrent_id")
            if (
                not isinstance(site_config_version, int)
                or isinstance(site_config_version, bool)
                or site_config_version < 1
                or not isinstance(adapter_site_id, str)
                or not adapter_site_id
                or not isinstance(torrent_id, str)
                or not torrent_id
            ):
                raise self._conflict("辅助文件补齐候选缺少安全站点引用")
            expected_digest = item.torrent_metainfo_digest or candidate.metainfo_digest
            if not isinstance(expected_digest, str) or len(expected_digest) != 64:
                raise self._conflict("辅助文件补齐缺少已验证的 metainfo digest")

            snapshot = dict(execution.config_snapshot)
            output_config = snapshot.get("output_config")
            if not isinstance(output_config, dict):
                raise self._conflict("execution 缺少输出配置快照")
            target_downloader_id = output_config.get("target_downloader_id")
            output_directory = output_config.get("output_directory")
            if not isinstance(target_downloader_id, str) or not target_downloader_id:
                raise self._conflict("execution 缺少目标下载器快照")
            if not isinstance(output_directory, str) or not output_directory:
                raise self._conflict("execution 缺少输出目录快照")

            source_snapshot = dict(item.source_snapshot)
            source_relative_path = source_snapshot.get("relative_path")
            if not isinstance(source_relative_path, str) or not source_relative_path:
                source_path = source_snapshot.get("path")
                if not isinstance(source_path, str) or not source_path:
                    raise self._conflict("影片项缺少来源路径")
                source_relative_path = PurePosixPath(source_path.replace("\\", "/")).name

            aux = dict(item.auxiliary_state or {})
            raw_missing = aux.get("missing_paths")
            if not isinstance(raw_missing, list) or any(
                not isinstance(path, str) or not path for path in raw_missing
            ):
                raise self._conflict("辅助文件补齐缺少待补齐路径")
            return _Context(
                execution_id=execution.id,
                item_id=item.id,
                candidate_id=candidate.id,
                generation=item.candidate_generation,
                source_snapshot=source_snapshot,
                source_relative_path=source_relative_path,
                site_config_id=candidate.site_id,
                site_config_version=site_config_version,
                adapter_site_id=adapter_site_id,
                torrent_id=torrent_id,
                expected_metainfo_digest=expected_digest,
                target_downloader_id=target_downloader_id,
                output_directory=output_directory,
                missing_paths=tuple(raw_missing),
                auxiliary_state=aux,
            )

    async def _prepare(self, context: _Context) -> _Prepared:
        site_binding = next(
            (
                item
                for item in self._site_provider.enabled_adapters()
                if item.config_id == context.site_config_id
            ),
            None,
        )
        if site_binding is None:
            raise self._conflict("候选站点当前不可用")
        if (
            site_binding.config_version != context.site_config_version
            or site_binding.site_id != context.adapter_site_id
        ):
            raise self._conflict("候选站点配置已变化，请重新匹配")

        async with asyncio.timeout(self._fetch_timeout_seconds):
            payload = await site_binding.adapter.fetch_torrent(context.torrent_id)
        if payload.site_id != context.adapter_site_id or payload.torrent_id != context.torrent_id:
            raise self._conflict("重新获取的 torrent 身份与候选不一致")
        meta = parse_torrent(payload.content)
        if meta.metainfo_digest != context.expected_metainfo_digest:
            raise ApplicationError(
                code="UNPACK_AUX_TORRENT_CHANGED",
                status=409,
                title="候选 torrent 已变化",
                detail="当前 metainfo digest 与内容验证阶段绑定值不一致",
            )
        try:
            plan = build_auxiliary_fetch_plan(meta, context.missing_paths)
            torrent_hash = torrent_client_hash(meta)
        except ValueError as exc:
            raise self._invalid(str(exc)) from exc

        binding = self._downloader_provider.write_binding(context.target_downloader_id)
        if binding.capabilities.get("supports_selective_files") is not True:
            raise ApplicationError(
                code="UNPACK_AUX_SELECTIVE_FILES_UNSUPPORTED",
                status=409,
                title="当前下载器不支持辅助文件补齐",
                detail="目标下载器能力快照未声明 supports_selective_files",
            )

        base = Path(context.output_directory).parent
        staging_candidate = (
            base / ".packbreaker-staging" / "unpack" / context.execution_id / context.item_id
        )
        try:
            staging_text, _anchor, _exists = self._path_scope.resolve_output_anchor(
                staging_candidate.as_posix()
            )
        except DomainViolation as exc:
            raise self._invalid(str(exc)) from exc
        staging_path = Path(staging_text)
        try:
            remote_save_path = binding.remote_save_path(staging_path)
        except (DomainViolation, ValueError) as exc:
            raise ApplicationError(
                code="UNPACK_AUX_STAGING_PATH_UNMAPPED",
                status=409,
                title="辅助文件 staging 无法映射到目标下载器",
                detail="请检查目标下载器路径映射是否覆盖输出目录所在挂载",
            ) from exc
        frozen_binding_digest = context.auxiliary_state.get("binding_digest")
        frozen_downloader_version = context.auxiliary_state.get("downloader_version")
        frozen_remote_save_path = context.auxiliary_state.get("remote_save_path")
        if frozen_binding_digest is not None and frozen_binding_digest != binding.binding_digest:
            raise ApplicationError(
                code="UNPACK_AUX_DOWNLOADER_CONFIG_CHANGED",
                status=409,
                title="目标下载器配置已变化",
                detail="辅助文件补齐期间下载器路径映射或能力发生变化，禁止继续",
            )
        if (
            frozen_downloader_version is not None
            and frozen_downloader_version != binding.downloader_version
        ):
            raise ApplicationError(
                code="UNPACK_AUX_DOWNLOADER_CONFIG_CHANGED",
                status=409,
                title="目标下载器配置已变化",
                detail="辅助文件补齐期间下载器配置版本发生变化，禁止继续",
            )
        if frozen_remote_save_path is not None and frozen_remote_save_path != remote_save_path:
            raise ApplicationError(
                code="UNPACK_AUX_STAGING_PATH_CHANGED",
                status=409,
                title="辅助文件 staging 映射已变化",
                detail="辅助文件补齐期间远端保存路径发生变化，禁止继续",
            )
        ownership_tag = f"packbreaker-aux-{context.item_id}"
        return _Prepared(
            context=context,
            binding=binding,
            meta=meta,
            payload=payload.content,
            plan=plan,
            torrent_hash=torrent_hash,
            staging_path=staging_path,
            remote_save_path=remote_save_path,
            ownership_tag=ownership_tag,
        )

    async def _ensure_staging_ready(self, prepared: _Prepared) -> None:
        self._ensure_directory(prepared)
        await self._ensure_torrent_added(prepared)
        await self._ensure_file_selection(prepared)
        await self._ensure_torrent_started(prepared)

    def _ensure_directory(self, prepared: _Prepared) -> None:
        target = {"path": prepared.staging_path.as_posix()}
        intent = {
            "schema_version": _AUX_SCHEMA_VERSION,
            "operation_token": _operation_token(prepared, _AUX_DIR_OPERATION),
        }
        journal, created = self._record_intent(
            prepared.context.item_id,
            _AUX_DIR_OPERATION,
            prepared,
            target=target,
            intent=intent,
            before_snapshot={"exists": _lstat_kind(prepared.staging_path) is not None},
        )
        kind = _lstat_kind(prepared.staging_path)
        if journal.status == OperationStatus.APPLIED.value:
            if kind != "directory":
                raise self._conflict("已确认的 staging 目录当前不再是安全目录")
            return
        if created and kind is not None:
            self._mark_reconcile(journal.id, "UNPACK_AUX_STAGING_PREEXISTED")
            raise self._conflict("staging 目标在 intent 前已存在，拒绝接管")
        if kind is None:
            prepared.staging_path.mkdir(parents=True, exist_ok=False)
        if _lstat_kind(prepared.staging_path) != "directory":
            self._mark_reconcile(journal.id, "UNPACK_AUX_STAGING_INVALID")
            raise self._conflict("staging 创建后不是安全目录")
        self._mark_applied(journal.id, {"path": prepared.staging_path.as_posix()})

    async def _ensure_torrent_added(self, prepared: _Prepared) -> None:
        target = {
            "downloader_id": prepared.context.target_downloader_id,
            "torrent_hash": prepared.torrent_hash,
            "remote_save_path": prepared.remote_save_path,
        }
        intent = {
            "schema_version": _AUX_SCHEMA_VERSION,
            "metainfo_digest": prepared.meta.metainfo_digest,
            "binding_digest": prepared.binding.binding_digest,
            "downloader_version": prepared.binding.downloader_version,
            "ownership_tag": prepared.ownership_tag,
            "operation_token": _operation_token(prepared, _AUX_ADD_OPERATION),
        }
        journal, _created = self._record_intent(
            prepared.context.item_id,
            _AUX_ADD_OPERATION,
            prepared,
            target=target,
            intent=intent,
        )
        state = await _owned_torrent_state(prepared)
        if journal.status == OperationStatus.APPLIED.value:
            if state is None:
                self._mark_reconcile(journal.id, "UNPACK_AUX_APPLIED_TORRENT_MISSING")
                raise ApplicationError(
                    code="UNPACK_AUX_APPLIED_TORRENT_MISSING",
                    status=409,
                    title="已确认的 staging torrent 已丢失",
                    detail="外部状态与已确认 journal 不一致，禁止自动重新添加",
                )
            return
        if state is not None:
            self._mark_applied(journal.id, _state_snapshot(state))
            return
        if journal.status == OperationStatus.RECONCILE_REQUIRED.value:
            raise ApplicationError(
                code="UNPACK_AUX_ADD_RECONCILE_REQUIRED",
                status=409,
                title="辅助下载任务添加结果需要对账",
                detail="上次添加结果未知且当前未发现可证明归属的 torrent，禁止盲目重发",
            )
        try:
            if isinstance(prepared.binding, QbittorrentWriteBinding):
                await prepared.binding.adapter.add_torrent(
                    QbittorrentAddRequest(
                        torrent_content=prepared.payload,
                        save_path=prepared.remote_save_path,
                        verification_level=VerificationLevel.CLIENT_CHECK_REQUIRED,
                        skip_checking=False,
                        tags=(prepared.ownership_tag,),
                        paused=True,
                    )
                )
            else:
                await prepared.binding.adapter.add_torrent(
                    TransmissionAddRequest(
                        torrent_content=prepared.payload,
                        save_path=prepared.remote_save_path,
                        labels=(prepared.ownership_tag,),
                        paused=True,
                    )
                )
        except DownloaderAdapterError:
            recovered = await _owned_torrent_state(prepared)
            if recovered is not None:
                self._mark_applied(journal.id, _state_snapshot(recovered))
                return
            self._mark_reconcile(journal.id, "UNPACK_AUX_ADD_RESULT_UNKNOWN")
            raise
        state = await _owned_torrent_state(prepared)
        if state is None:
            self._mark_reconcile(journal.id, "UNPACK_AUX_ADD_NOT_OBSERVED")
            raise self._conflict("下载器返回添加成功，但未观察到归属明确的 staging torrent")
        self._mark_applied(journal.id, _state_snapshot(state))

    async def _ensure_file_selection(self, prepared: _Prepared) -> None:
        target = {
            "downloader_id": prepared.context.target_downloader_id,
            "torrent_hash": prepared.torrent_hash,
        }
        intent = {
            "schema_version": _AUX_SCHEMA_VERSION,
            "wanted": list(prepared.plan.wanted_indices),
            "unwanted": list(prepared.plan.unwanted_indices),
            "binding_digest": prepared.binding.binding_digest,
            "operation_token": _operation_token(prepared, _AUX_SELECT_OPERATION),
        }
        journal, _created = self._record_intent(
            prepared.context.item_id,
            _AUX_SELECT_OPERATION,
            prepared,
            target=target,
            intent=intent,
        )
        if journal.status == OperationStatus.APPLIED.value:
            return
        await _require_owned_torrent_state(prepared)
        selector = cast(DownloaderFileSelectionAdapter, prepared.binding.adapter)
        try:
            await selector.set_file_selection(
                prepared.torrent_hash,
                wanted=prepared.plan.wanted_indices,
                unwanted=prepared.plan.unwanted_indices,
            )
        except DownloaderAdapterError:
            self._mark_reconcile(journal.id, "UNPACK_AUX_FILE_SELECTION_RESULT_UNKNOWN")
            raise
        self._mark_applied(
            journal.id,
            {
                "wanted": list(prepared.plan.wanted_indices),
                "unwanted": list(prepared.plan.unwanted_indices),
            },
        )

    async def _ensure_torrent_started(self, prepared: _Prepared) -> None:
        target = {
            "downloader_id": prepared.context.target_downloader_id,
            "torrent_hash": prepared.torrent_hash,
        }
        intent = {
            "schema_version": _AUX_SCHEMA_VERSION,
            "binding_digest": prepared.binding.binding_digest,
            "operation_token": _operation_token(prepared, _AUX_START_OPERATION),
        }
        journal, _created = self._record_intent(
            prepared.context.item_id,
            _AUX_START_OPERATION,
            prepared,
            target=target,
            intent=intent,
        )
        state = await _require_owned_torrent_state(prepared)
        if journal.status == OperationStatus.APPLIED.value:
            if state.stopped:
                self._mark_reconcile(journal.id, "UNPACK_AUX_START_STATE_DRIFTED")
                raise self._conflict("已确认启动的 staging torrent 当前处于停止状态")
            return
        if not state.stopped:
            self._mark_applied(journal.id, _state_snapshot(state))
            return
        try:
            await prepared.binding.adapter.start_torrent(prepared.torrent_hash)
        except DownloaderAdapterError:
            current = await _owned_torrent_state(prepared)
            if current is not None and not current.stopped:
                self._mark_applied(journal.id, _state_snapshot(current))
                return
            self._mark_reconcile(journal.id, "UNPACK_AUX_START_RESULT_UNKNOWN")
            raise
        current = await _require_owned_torrent_state(prepared)
        if current.stopped:
            self._mark_reconcile(journal.id, "UNPACK_AUX_START_NOT_OBSERVED")
            raise self._conflict("下载器未进入辅助文件下载状态")
        self._mark_applied(journal.id, _state_snapshot(current))

    async def _poll_downloading(self, prepared: _Prepared) -> None:
        remote = await _owned_torrent_state(prepared)
        if remote is None:
            # A remote remove can succeed before its local verification status
            # commits, or the acknowledgement can be lost. Only an existing,
            # identity-bound remove intent plus a completed stop journal allow
            # recovery without a still-present torrent.
            with self._session_factory() as session:
                stop = session.scalar(
                    select(UnpackExternalOperationJournal).where(
                        UnpackExternalOperationJournal.item_id == prepared.context.item_id,
                        UnpackExternalOperationJournal.idempotency_key
                        == _journal_key(prepared, _AUX_STOP_OPERATION),
                    )
                )
                removal = session.scalar(
                    select(UnpackExternalOperationJournal).where(
                        UnpackExternalOperationJournal.item_id == prepared.context.item_id,
                        UnpackExternalOperationJournal.idempotency_key
                        == _journal_key(prepared, _AUX_REMOVE_OPERATION),
                    )
                )
            if (
                stop is None
                or stop.status != OperationStatus.APPLIED.value
                or removal is None
                or removal.status
                not in {
                    OperationStatus.INTENT_RECORDED.value,
                    OperationStatus.RECONCILE_REQUIRED.value,
                    OperationStatus.APPLIED.value,
                }
            ):
                raise self._conflict("临时种子已消失但缺少可信的停止/移除操作记录，必须人工对账")
            if not self._auxiliary_files_ready(prepared):
                raise self._conflict("种子已移除但辅助文件不完整，禁止继续")
            verification, mappings = self._verify_combined_sources(prepared)
            if verification.level is VerificationLevel.FULL_VERIFIED and not _has_mismatch(
                verification
            ):
                await self._ensure_torrent_removed(prepared)
                self._mark_verified(prepared, verification, mappings)
            else:
                self._mark_mismatch(prepared, verification, mappings)
            return
        if not self._auxiliary_files_ready(prepared):
            return
        await self._ensure_torrent_stopped(prepared)
        # Keep this item claimable by the auxiliary driver until after the
        # final remote cleanup and result commit. Switching to CONTENT_VERIFYING
        # here stranded items when Transmission disconnected on remove.
        verification, mappings = self._verify_combined_sources(prepared)
        if verification.level is VerificationLevel.FULL_VERIFIED and not _has_mismatch(
            verification
        ):
            await self._ensure_torrent_removed(prepared)
            self._mark_verified(prepared, verification, mappings)
            return
        self._mark_mismatch(prepared, verification, mappings)

    async def _ensure_torrent_stopped(self, prepared: _Prepared) -> None:
        target = {
            "downloader_id": prepared.context.target_downloader_id,
            "torrent_hash": prepared.torrent_hash,
        }
        intent = {
            "schema_version": _AUX_SCHEMA_VERSION,
            "binding_digest": prepared.binding.binding_digest,
            "operation_token": _operation_token(prepared, _AUX_STOP_OPERATION),
        }
        journal, _created = self._record_intent(
            prepared.context.item_id,
            _AUX_STOP_OPERATION,
            prepared,
            target=target,
            intent=intent,
        )
        state = await _require_owned_torrent_state(prepared)
        if journal.status == OperationStatus.APPLIED.value and state.stopped:
            return
        if journal.status == OperationStatus.APPLIED.value and not state.stopped:
            self._mark_reconcile(journal.id, "UNPACK_AUX_STOP_STATE_DRIFTED")
            raise ApplicationError(
                code="UNPACK_AUX_STOP_STATE_DRIFTED",
                status=409,
                title="临时种子暂停状态发生变化",
                detail="先前已确认暂停的临时种子再次运行，禁止重复发送暂停命令",
            )
        if state.stopped:
            self._mark_applied(journal.id, _state_snapshot(state))
            return
        if journal.status == OperationStatus.RECONCILE_REQUIRED.value:
            raise ApplicationError(
                code="UNPACK_AUX_STOP_RECONCILE_REQUIRED",
                status=409,
                title="临时种子暂停结果未确认",
                detail="临时种子仍在运行且上次暂停结果未知，禁止自动重复暂停",
            )
        try:
            await prepared.binding.adapter.stop_torrent(prepared.torrent_hash)
        except DownloaderAdapterError:
            try:
                current = await self._wait_until_stopped(prepared)
            except DownloaderAdapterError:
                self._mark_reconcile(journal.id, "UNPACK_AUX_STOP_RESULT_UNKNOWN")
                raise
            if current is not None and current.stopped:
                self._mark_applied(journal.id, _state_snapshot(current))
                return
            self._mark_reconcile(journal.id, "UNPACK_AUX_STOP_RESULT_UNKNOWN")
            raise
        try:
            current = await self._wait_until_stopped(prepared)
        except DownloaderAdapterError:
            self._mark_reconcile(journal.id, "UNPACK_AUX_STOP_RESULT_UNKNOWN")
            raise
        if current is None or not current.stopped:
            self._mark_reconcile(journal.id, "UNPACK_AUX_STOP_NOT_OBSERVED")
            raise self._conflict("辅助文件已就绪，但下载器无法安全暂停 staging torrent")
        self._mark_applied(journal.id, _state_snapshot(current))

    @staticmethod
    async def _wait_until_stopped(
        prepared: _Prepared,
    ) -> QbittorrentTorrentState | TransmissionTorrentState | None:
        # Transmission reports a successful torrent_stop RPC before the
        # subsequent torrent_get necessarily observes status=0. Poll only the
        # already-bound torrent; never send the stop operation twice.
        for attempt in range(10):
            if attempt:
                await asyncio.sleep(0.3)
            current = await _owned_torrent_state(prepared)
            if current is None or current.stopped:
                return current
        return current

    async def _ensure_torrent_removed(self, prepared: _Prepared) -> None:
        target = {
            "downloader_id": prepared.context.target_downloader_id,
            "torrent_hash": prepared.torrent_hash,
        }
        intent = {
            "schema_version": _AUX_SCHEMA_VERSION,
            "binding_digest": prepared.binding.binding_digest,
            "delete_local_data": False,
            "operation_token": _operation_token(prepared, _AUX_REMOVE_OPERATION),
        }
        journal, _created = self._record_intent(
            prepared.context.item_id,
            _AUX_REMOVE_OPERATION,
            prepared,
            target=target,
            intent=intent,
        )
        state = await _owned_torrent_state(prepared)
        if journal.status == OperationStatus.APPLIED.value and state is not None:
            self._mark_reconcile(journal.id, "UNPACK_AUX_REMOVED_TORRENT_REAPPEARED")
            raise ApplicationError(
                code="UNPACK_AUX_REMOVED_TORRENT_REAPPEARED",
                status=409,
                title="已移除的 staging torrent 再次出现",
                detail="外部状态与已确认 journal 不一致，禁止重复执行移除",
            )
        if state is None:
            self._mark_applied(journal.id, {"removed": True, "files_kept": True})
            return
        if journal.status == OperationStatus.RECONCILE_REQUIRED.value:
            raise ApplicationError(
                code="UNPACK_AUX_REMOVE_RECONCILE_REQUIRED",
                status=409,
                title="临时种子移除结果未确认",
                detail="远端仍存在临时种子且上次移除结果未知，禁止自动再次移除",
            )
        try:
            await prepared.binding.adapter.remove_torrent_keep_files(prepared.torrent_hash)
        except DownloaderAdapterError:
            try:
                current = await self._wait_until_removed(prepared)
            except DownloaderAdapterError:
                self._mark_reconcile(journal.id, "UNPACK_AUX_REMOVE_RESULT_UNKNOWN")
                raise
            if current is None:
                self._mark_applied(journal.id, {"removed": True, "files_kept": True})
                return
            self._mark_reconcile(journal.id, "UNPACK_AUX_REMOVE_RESULT_UNKNOWN")
            raise
        try:
            current = await self._wait_until_removed(prepared)
        except DownloaderAdapterError:
            self._mark_reconcile(journal.id, "UNPACK_AUX_REMOVE_RESULT_UNKNOWN")
            raise
        if current is not None:
            self._mark_reconcile(journal.id, "UNPACK_AUX_REMOVE_NOT_OBSERVED")
            raise self._conflict("staging torrent 移除结果无法确认")
        self._mark_applied(journal.id, {"removed": True, "files_kept": True})

    @staticmethod
    async def _wait_until_removed(
        prepared: _Prepared,
    ) -> QbittorrentTorrentState | TransmissionTorrentState | None:
        # The remote torrent list may briefly lag a successful remove RPC.
        # Only observe the same identity-bound torrent; never remove twice.
        for attempt in range(10):
            if attempt:
                await asyncio.sleep(0.3)
            current = await _owned_torrent_state(prepared)
            if current is None:
                return None
        return current

    def _auxiliary_files_ready(self, prepared: _Prepared) -> bool:
        files_by_path = {item.path: item for item in prepared.meta.files}
        for path in prepared.plan.missing_paths:
            torrent_file = files_by_path[path]
            local = _safe_torrent_path(prepared.staging_path, path)
            try:
                self._path_scope.resolve_existing_directory(local.parent.as_posix())
                snapshot = current_file_snapshot(local)
            except (DomainViolation, OSError):
                return False
            if snapshot.size != torrent_file.length:
                return False
        return True

    def _verify_combined_sources(
        self,
        prepared: _Prepared,
    ) -> tuple[TorrentVerificationResult, tuple[AutoMappingDecision, ...]]:
        source = self._source_candidate(prepared.context)
        files_by_path = {item.path: item for item in prepared.meta.files}
        candidates: list[SourceFileCandidate] = [source]
        for path in prepared.plan.missing_paths:
            torrent_file = files_by_path[path]
            local = _safe_torrent_path(prepared.staging_path, path)
            self._path_scope.resolve_existing_directory(local.parent.as_posix())
            snapshot = current_file_snapshot(local)
            if snapshot.size != torrent_file.length:
                raise self._conflict("staging 辅助文件大小与 torrent 元数据不一致")
            candidates.append(
                SourceFileCandidate(
                    relative_path=path,
                    source_path=local.as_posix(),
                    length=snapshot.size,
                    snapshot=snapshot,
                )
            )
        mappings = auto_map_files(prepared.meta, tuple(candidates))
        verification = _verify_torrent(prepared.meta, mappings)
        return verification, mappings

    def _source_candidate(self, context: _Context) -> SourceFileCandidate:
        source_path = context.source_snapshot.get("path")
        if not isinstance(source_path, str) or not source_path:
            raise self._conflict("影片项缺少来源路径快照")
        path = Path(self._path_scope.normalize_reference(source_path))
        self._path_scope.resolve_existing_directory(path.parent.as_posix())
        snapshot = current_file_snapshot(path)
        if not _snapshot_matches(context.source_snapshot, snapshot):
            raise ApplicationError(
                code="SOURCE_SNAPSHOT_CHANGED",
                status=409,
                title="来源文件已变化",
                detail="辅助文件下载期间来源影片发生变化，禁止继续",
            )
        return SourceFileCandidate(
            relative_path=context.source_relative_path,
            source_path=path.as_posix(),
            length=snapshot.size,
            snapshot=snapshot,
        )

    def _mark_verified(
        self,
        prepared: _Prepared,
        verification: TorrentVerificationResult,
        mappings: tuple[AutoMappingDecision, ...],
    ) -> None:
        with self._session_factory() as session:
            begin_immediate_write(session)
            item, candidate, execution = _current_records(session, prepared.context)
            now = utc_now()
            item.status = UnpackItemStatus.CONTENT_VERIFIED.value
            item.content_verification_level = VerificationLevel.FULL_VERIFIED.value
            item.last_error_code = None
            item.last_error_message = None
            item.auxiliary_state = {
                **_downloading_state(prepared),
                "state": "READY_FOR_FINALIZATION",
                "staging_torrent_removed": True,
                "verification_level": VerificationLevel.FULL_VERIFIED.value,
            }
            item.updated_at = now
            item.version += 1
            candidate.verification_status = UnpackCandidateVerificationStatus.VERIFIED.value
            candidate.verification_level = VerificationLevel.FULL_VERIFIED.value
            candidate.verification_error_code = None
            candidate.evidence = {
                **dict(candidate.evidence),
                "auxiliary_verification": _verification_evidence(
                    verification,
                    mappings,
                ),
            }
            session.flush()
            _refresh_execution_state(session, execution)
            session.commit()

    def _mark_mismatch(
        self,
        prepared: _Prepared,
        verification: TorrentVerificationResult,
        mappings: tuple[AutoMappingDecision, ...],
    ) -> None:
        with self._session_factory() as session:
            begin_immediate_write(session)
            item, candidate, execution = _current_records(session, prepared.context)
            now = utc_now()
            item.status = UnpackItemStatus.CONTENT_MISMATCH.value
            item.content_verification_level = VerificationLevel.BLOCKED.value
            item.last_error_code = "UNPACK_AUXILIARY_REVERIFY_FAILED"
            item.last_error_message = "辅助文件补齐后完整内容校验仍未通过"
            item.updated_at = now
            item.version += 1
            candidate.verification_status = UnpackCandidateVerificationStatus.MISMATCH.value
            candidate.verification_level = VerificationLevel.BLOCKED.value
            candidate.verification_error_code = "UNPACK_AUXILIARY_REVERIFY_FAILED"
            candidate.evidence = {
                **dict(candidate.evidence),
                "auxiliary_verification": _verification_evidence(
                    verification,
                    mappings,
                ),
            }
            session.flush()
            _refresh_execution_state(session, execution)
            session.commit()

    def _set_auxiliary_state(self, item_id: str, state: dict[str, Any]) -> None:
        with self._session_factory() as session:
            begin_immediate_write(session)
            item = session.get(UnpackExecutionItem, item_id)
            if item is None:
                raise self._item_not_found()
            if item.status != UnpackItemStatus.AUXILIARY_FETCHING.value:
                raise self._conflict("影片项已离开辅助文件补齐阶段")
            item.auxiliary_state = state
            item.updated_at = utc_now()
            item.version += 1
            session.commit()

    def _record_intent(
        self,
        item_id: str,
        operation_type: str,
        prepared: _Prepared,
        *,
        target: dict[str, Any],
        intent: dict[str, Any],
        before_snapshot: dict[str, Any] | None = None,
    ) -> tuple[UnpackExternalOperationJournal, bool]:
        key = _journal_key(prepared, operation_type)
        with self._session_factory() as session:
            begin_immediate_write(session)
            existing = session.scalar(
                select(UnpackExternalOperationJournal).where(
                    UnpackExternalOperationJournal.idempotency_key == key
                )
            )
            if existing is not None:
                if (
                    existing.item_id != item_id
                    or existing.operation_type != operation_type
                    or existing.target != target
                    or existing.intent != intent
                ):
                    raise self._conflict("同一外部操作幂等键绑定了不同 intent")
                session.expunge(existing)
                return existing, False
            now = utc_now()
            row = UnpackExternalOperationJournal(
                id=new_uuid(),
                item_id=item_id,
                idempotency_key=key,
                operation_type=operation_type,
                target=target,
                intent=intent,
                status=OperationStatus.INTENT_RECORDED.value,
                before_snapshot=before_snapshot,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.commit()
            session.expunge(row)
            return row, True

    def _mark_applied(self, journal_id: str, after_snapshot: dict[str, Any]) -> None:
        with self._session_factory() as session:
            begin_immediate_write(session)
            row = session.get(UnpackExternalOperationJournal, journal_id)
            if row is None:
                raise self._conflict("外部操作 journal 不存在")
            if row.status == OperationStatus.APPLIED.value:
                return
            if row.status not in {
                OperationStatus.INTENT_RECORDED.value,
                OperationStatus.RECONCILE_REQUIRED.value,
            }:
                raise self._conflict("外部操作 journal 当前状态不能确认成功")
            row.status = OperationStatus.APPLIED.value
            row.after_snapshot = after_snapshot
            row.last_error_code = None
            row.updated_at = utc_now()
            session.commit()

    def _mark_reconcile(self, journal_id: str, code: str) -> None:
        with self._session_factory() as session:
            begin_immediate_write(session)
            row = session.get(UnpackExternalOperationJournal, journal_id)
            if row is None:
                return
            if row.status == OperationStatus.RECONCILE_REQUIRED.value:
                return
            row.status = OperationStatus.RECONCILE_REQUIRED.value
            row.last_error_code = code
            row.updated_at = utc_now()
            session.commit()

    def _mark_error(self, item_id: str, *, code: str, message: str) -> None:
        with self._session_factory() as session:
            begin_immediate_write(session)
            item = session.get(UnpackExecutionItem, item_id)
            if item is None or item.status not in {
                UnpackItemStatus.AUXILIARY_FETCHING.value,
                UnpackItemStatus.CONTENT_VERIFYING.value,
            }:
                return
            execution = session.get(UnpackExecution, item.execution_id)
            if execution is None:
                return
            item.status = UnpackItemStatus.MATCH_ERROR.value
            item.last_error_code = code
            item.last_error_message = message
            item.updated_at = utc_now()
            item.version += 1
            session.flush()
            _refresh_execution_state(session, execution)
            session.commit()

    def _report(self, execution_id: str, *, processed_count: int) -> UnpackAuxiliaryReport:
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

            return UnpackAuxiliaryReport(
                execution_id=execution_id,
                processed_count=processed_count,
                downloading_count=count(UnpackItemStatus.AUXILIARY_FETCHING),
                verified_count=count(UnpackItemStatus.CONTENT_VERIFIED),
                mismatch_count=count(UnpackItemStatus.CONTENT_MISMATCH),
                error_count=count(UnpackItemStatus.MATCH_ERROR),
                execution_status=UnpackExecutionStatus(execution.status),
            )

    @staticmethod
    def _invalid(detail: str) -> ApplicationError:
        return ApplicationError(
            code="UNPACK_AUXILIARY_INVALID",
            status=422,
            title="辅助文件补齐参数无效",
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
            code="UNPACK_AUXILIARY_CONFLICT",
            status=409,
            title="辅助文件补齐状态冲突",
            detail=detail,
        )


async def _owned_torrent_state(
    prepared: _Prepared,
) -> QbittorrentTorrentState | TransmissionTorrentState | None:
    states = await prepared.binding.adapter.get_torrents((prepared.torrent_hash,))
    if not states:
        return None
    if len(states) != 1:
        raise ApplicationError(
            code="UNPACK_AUX_TORRENT_AMBIGUOUS",
            status=409,
            title="staging torrent 状态不唯一",
            detail="目标下载器返回多个同 hash torrent 状态",
        )
    state = states[0]
    if isinstance(state, QbittorrentTorrentState):
        save_path = state.save_path
        labels = state.tags
    else:
        save_path = state.download_dir
        labels = state.labels
    if save_path != prepared.remote_save_path or prepared.ownership_tag not in labels:
        raise ApplicationError(
            code="UNPACK_AUX_TORRENT_NOT_OWNED",
            status=409,
            title="检测到非 PackBreaker 管理的同 hash torrent",
            detail="禁止接管已有下载器任务",
        )
    return state


async def _require_owned_torrent_state(
    prepared: _Prepared,
) -> QbittorrentTorrentState | TransmissionTorrentState:
    state = await _owned_torrent_state(prepared)
    if state is None:
        raise ApplicationError(
            code="UNPACK_AUX_TORRENT_MISSING",
            status=409,
            title="staging torrent 已不存在",
            detail="辅助文件下载任务在下载器中丢失",
        )
    return state


def _downloading_state(prepared: _Prepared) -> dict[str, Any]:
    return {
        "state": "DOWNLOADING",
        "missing_paths": list(prepared.plan.missing_paths),
        "staging_path": prepared.staging_path.as_posix(),
        "remote_save_path": prepared.remote_save_path,
        "target_downloader_id": prepared.context.target_downloader_id,
        "downloader_version": prepared.binding.downloader_version,
        "binding_digest": prepared.binding.binding_digest,
        "torrent_hash": prepared.torrent_hash,
        "ownership_tag": prepared.ownership_tag,
        "wanted_indices": list(prepared.plan.wanted_indices),
        "unwanted_indices": list(prepared.plan.unwanted_indices),
    }


def _operation_token(prepared: _Prepared, operation_type: str) -> str:
    return hashlib.sha256(
        (
            f"{_AUX_SCHEMA_VERSION}\n{prepared.context.item_id}\n"
            f"{prepared.context.candidate_id}\n{prepared.meta.metainfo_digest}\n"
            f"{prepared.binding.binding_digest}\n{operation_type}"
        ).encode()
    ).hexdigest()


def _journal_key(prepared: _Prepared, operation_type: str) -> str:
    token = _operation_token(prepared, operation_type)
    return hashlib.sha256(
        f"{prepared.context.item_id}:{operation_type}:{token}".encode()
    ).hexdigest()


def _state_snapshot(
    state: QbittorrentTorrentState | TransmissionTorrentState,
) -> dict[str, Any]:
    if isinstance(state, QbittorrentTorrentState):
        return {
            "torrent_hash": state.torrent_hash,
            "save_path": state.save_path,
            "state": state.state,
            "tags": list(state.tags),
            "progress": state.progress,
        }
    return {
        "torrent_hash": state.torrent_hash,
        "download_dir": state.download_dir,
        "status": state.status,
        "labels": list(state.labels),
        "percent_done": state.percent_done,
        "recheck_progress": state.recheck_progress,
    }


def _safe_torrent_path(root: Path, torrent_path: str) -> Path:
    relative = PurePosixPath(torrent_path)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ApplicationError(
            code="UNPACK_AUX_TORRENT_PATH_INVALID",
            status=409,
            title="torrent 文件路径无效",
            detail="辅助文件路径不能逃逸 staging",
        )
    candidate = root.joinpath(*relative.parts)
    if not candidate.is_relative_to(root):
        raise ApplicationError(
            code="UNPACK_AUX_TORRENT_PATH_INVALID",
            status=409,
            title="torrent 文件路径无效",
            detail="辅助文件路径不能逃逸 staging",
        )
    return candidate


def _lstat_kind(path: Path) -> str | None:
    try:
        result = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ApplicationError(
            code="UNPACK_AUX_STAGING_STAT_FAILED",
            status=409,
            title="无法检查 staging 状态",
            detail="staging 路径状态读取失败",
        ) from exc
    if stat.S_ISDIR(result.st_mode):
        return "directory"
    if stat.S_ISLNK(result.st_mode):
        return "symlink"
    return "other"


def _verify_torrent(
    meta: TorrentMeta,
    mappings: tuple[AutoMappingDecision, ...],
) -> TorrentVerificationResult:
    piece_mappings = tuple(
        V1FileMapping(
            item.torrent_path,
            item.state,
            Path(item.source_path) if item.source_path is not None else None,
        )
        for item in mappings
    )
    if meta.torrent_kind is TorrentKind.V1:
        return verify_v1_pieces(meta, piece_mappings)
    if meta.torrent_kind is TorrentKind.V2:
        return verify_v2_files(meta, piece_mappings)
    return verify_hybrid(meta, piece_mappings)


def _has_mismatch(result: TorrentVerificationResult) -> bool:
    if isinstance(result, V1VerificationResult):
        return any(piece.status is PieceStatus.MISMATCH for piece in result.pieces)
    if isinstance(result, V2VerificationResult):
        return any(
            item.status is PieceStatus.MISMATCH
            or any(piece.status is PieceStatus.MISMATCH for piece in item.pieces)
            for item in result.files
        )
    if isinstance(result, HybridVerificationResult):
        return _has_mismatch(result.v1) or _has_mismatch(result.v2)
    raise TypeError(type(result).__name__)


def _verification_evidence(
    result: TorrentVerificationResult,
    mappings: tuple[AutoMappingDecision, ...],
) -> dict[str, Any]:
    return {
        "level": result.level.value,
        "piece_mismatch": _has_mismatch(result),
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


def _current_records(
    session: Session,
    context: _Context,
) -> tuple[UnpackExecutionItem, UnpackMatchCandidate, UnpackExecution]:
    item = session.get(UnpackExecutionItem, context.item_id)
    candidate = session.get(UnpackMatchCandidate, context.candidate_id)
    if item is None or candidate is None:
        raise ApplicationError(
            code="UNPACK_AUXILIARY_CONFLICT",
            status=409,
            title="辅助文件补齐状态冲突",
            detail="影片项或候选已不存在",
        )
    if (
        item.status != UnpackItemStatus.AUXILIARY_FETCHING.value
        or item.candidate_generation != context.generation
        or item.selected_candidate_id != context.candidate_id
        or candidate.item_id != item.id
        or candidate.generation != context.generation
    ):
        raise ApplicationError(
            code="UNPACK_AUXILIARY_CONFLICT",
            status=409,
            title="辅助文件补齐状态冲突",
            detail="辅助文件验证结果已过期，拒绝覆盖较新的影片或候选状态",
        )
    execution = session.get(UnpackExecution, item.execution_id)
    if execution is None:
        raise ApplicationError(
            code="UNPACK_EXECUTION_NOT_FOUND",
            status=404,
            title="数据拆包执行不存在",
            detail="未找到指定数据拆包执行",
        )
    return item, candidate, execution


def _refresh_execution_state(session: Session, execution: UnpackExecution) -> None:
    def count(status: UnpackItemStatus) -> int:
        return int(
            session.scalar(
                select(func.count(UnpackExecutionItem.id))
                .where(UnpackExecutionItem.execution_id == execution.id)
                .where(UnpackExecutionItem.status == status.value)
            )
            or 0
        )

    execution.content_verified_count = count(UnpackItemStatus.CONTENT_VERIFIED)
    execution.content_mismatch_count = count(UnpackItemStatus.CONTENT_MISMATCH)
    execution.error_count = count(UnpackItemStatus.MATCH_ERROR)
    execution.review_count = count(UnpackItemStatus.REVIEW_REQUIRED)
    active = sum(
        count(status)
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
    if active:
        execution.status = UnpackExecutionStatus.CONTENT_VERIFYING.value
        execution.finished_at = None
    elif execution.review_count:
        execution.status = UnpackExecutionStatus.REVIEW_REQUIRED.value
        execution.finished_at = None
    elif execution.content_mismatch_count or execution.error_count:
        execution.status = UnpackExecutionStatus.COMPLETED_WITH_ERRORS.value
        execution.finished_at = now
    execution.updated_at = now
    execution.version += 1


def _snapshot_matches(expected: dict[str, Any], observed: FileSnapshot) -> bool:
    fields = ("device", "inode", "size", "file_type")
    if any(expected.get(field) != getattr(observed, field) for field in fields):
        return False
    return str(expected.get("mtime_ns")) == str(observed.mtime_ns)


def _safe_error(
    exc: ApplicationError | SiteAdapterError | DownloaderAdapterError | DomainViolation,
) -> tuple[str, str]:
    if isinstance(exc, ApplicationError):
        return exc.code, exc.detail
    if isinstance(exc, SiteAdapterError):
        return exc.code, "辅助文件补齐访问站点失败"
    if isinstance(exc, DownloaderAdapterError):
        return exc.code, "辅助文件补齐访问下载器失败"
    return exc.code.value, "辅助文件补齐安全校验失败"
