from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.downloaders import (
    QbittorrentWriteBinding,
    TransmissionWriteBinding,
)
from backend.app.application.errors import ApplicationError
from backend.app.application.sites import EnabledSiteAdapter
from backend.app.domain.operation import OperationStatus
from backend.app.domain.unpack import UnpackExecutionStatus, UnpackItemStatus
from backend.app.domain.unpack_execution_plan import (
    UnpackExecutionPlan,
    unpack_execution_plan_from_payload,
)
from backend.app.domain.verification import VerificationLevel
from backend.app.infrastructure.adapters.downloaders import (
    DownloaderAdapterError,
    QbittorrentAddRequest,
    QbittorrentTorrentState,
    TransmissionAddRequest,
    TransmissionTorrentState,
)
from backend.app.infrastructure.adapters.site_errors import SiteAdapterError
from backend.app.infrastructure.persistence.database import begin_immediate_write
from backend.app.infrastructure.persistence.models import (
    UnpackExecution,
    UnpackExecutionItem,
    UnpackExternalOperationJournal,
    UnpackMatchCandidate,
    new_uuid,
    utc_now,
)
from backend.app.infrastructure.torrent_parser import parse_torrent

_SCHEMA_VERSION = "packbreaker-unpack-seeding-v1"
_QB_ADD = "UNPACK_EXEC_QBITTORRENT_ADD"
_QB_VERIFY = "UNPACK_EXEC_QBITTORRENT_RECHECK"
_QB_START = "UNPACK_EXEC_QBITTORRENT_START"
_TR_ADD = "UNPACK_EXEC_TRANSMISSION_ADD"
_TR_VERIFY = "UNPACK_EXEC_TRANSMISSION_VERIFY"
_TR_START = "UNPACK_EXEC_TRANSMISSION_START"


class UnpackSeedingSiteProvider(Protocol):
    def enabled_adapters(self) -> tuple[EnabledSiteAdapter, ...]: ...


class UnpackSeedingDownloaderProvider(Protocol):
    def write_binding(
        self,
        downloader_id: str,
    ) -> QbittorrentWriteBinding | TransmissionWriteBinding: ...


@dataclass(frozen=True, slots=True)
class UnpackSeedingReport:
    execution_id: str
    processed_count: int
    client_verifying_count: int
    completed_count: int
    error_count: int
    execution_status: UnpackExecutionStatus


@dataclass(frozen=True, slots=True)
class _Prepared:
    execution_id: str
    item_id: str
    item_version: int
    candidate_id: str
    generation: int
    site_config_id: str
    site_config_version: int
    adapter_site_id: str
    torrent_id: str
    plan: UnpackExecutionPlan
    binding: QbittorrentWriteBinding | TransmissionWriteBinding
    torrent_content: bytes | None
    expected_hashes: tuple[str, ...]
    ownership_tag: str


class UnpackSeedingService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        site_provider: UnpackSeedingSiteProvider,
        downloader_provider: UnpackSeedingDownloaderProvider,
        *,
        fetch_timeout_seconds: float = 30.0,
    ) -> None:
        if fetch_timeout_seconds <= 0:
            raise ValueError("最终辅种 torrent 获取超时必须大于 0")
        self._session_factory = session_factory
        self._site_provider = site_provider
        self._downloader_provider = downloader_provider
        self._fetch_timeout_seconds = fetch_timeout_seconds

    def list_seedable_execution_ids(self, *, limit: int = 20) -> tuple[str, ...]:
        if limit < 1 or limit > 500:
            raise self._invalid("最终辅种 execution 查询数量必须位于 1 到 500 之间")
        with self._session_factory() as session:
            executions = session.scalars(
                select(UnpackExecution)
                .where(
                    select(UnpackExecutionItem.id)
                    .where(UnpackExecutionItem.execution_id == UnpackExecution.id)
                    .where(
                        UnpackExecutionItem.status.in_(
                            (
                                UnpackItemStatus.EXECUTING.value,
                                UnpackItemStatus.CLIENT_VERIFYING.value,
                            )
                        )
                    )
                    .exists()
                )
                .order_by(UnpackExecution.updated_at, UnpackExecution.id)
                .limit(limit * 4)
            ).all()
            ids: list[str] = []
            for execution in executions:
                items = session.scalars(
                    select(UnpackExecutionItem)
                    .where(UnpackExecutionItem.execution_id == execution.id)
                    .where(
                        UnpackExecutionItem.status.in_(
                            (
                                UnpackItemStatus.EXECUTING.value,
                                UnpackItemStatus.CLIENT_VERIFYING.value,
                            )
                        )
                    )
                ).all()
                if any(
                    dict(item.execution_state or {}).get("stage")
                    in {
                        "FILES_MATERIALIZED",
                        "TORRENT_ADDED",
                        "CLIENT_VERIFYING",
                        "CLIENT_VERIFIED",
                        "STARTING",
                    }
                    for item in items
                ):
                    ids.append(execution.id)
                    if len(ids) == limit:
                        break
            return tuple(ids)

    async def advance_next_batch(
        self,
        execution_id: str,
        *,
        limit: int = 5,
    ) -> UnpackSeedingReport:
        if limit < 1 or limit > 100:
            raise self._invalid("最终辅种批次必须位于 1 到 100 之间")
        item_ids = self._list_item_ids(execution_id, limit=limit)
        processed = 0
        for item_id in item_ids:
            try:
                prepared = await self._prepare(item_id)
                await self._advance(prepared)
            except (
                ApplicationError,
                DownloaderAdapterError,
                SiteAdapterError,
                TimeoutError,
                ValueError,
            ) as exc:
                code, message = _safe_error(exc)
                self._mark_error(item_id, code=code, message=message)
            processed += 1
        return self._report(execution_id, processed_count=processed)

    def _list_item_ids(self, execution_id: str, *, limit: int) -> tuple[str, ...]:
        with self._session_factory() as session:
            execution = session.get(UnpackExecution, execution_id)
            if execution is None:
                raise self._not_found()
            rows = session.scalars(
                select(UnpackExecutionItem)
                .where(UnpackExecutionItem.execution_id == execution_id)
                .where(
                    UnpackExecutionItem.status.in_(
                        (
                            UnpackItemStatus.EXECUTING.value,
                            UnpackItemStatus.CLIENT_VERIFYING.value,
                        )
                    )
                )
                .order_by(UnpackExecutionItem.id)
                .limit(limit * 4)
            ).all()
            ids: list[str] = []
            for item in rows:
                stage = dict(item.execution_state or {}).get("stage")
                if stage not in {
                    "FILES_MATERIALIZED",
                    "TORRENT_ADDED",
                    "CLIENT_VERIFYING",
                    "CLIENT_VERIFIED",
                    "STARTING",
                }:
                    continue
                ids.append(item.id)
                if len(ids) == limit:
                    break
            return tuple(ids)

    async def _prepare(self, item_id: str) -> _Prepared:
        with self._session_factory() as session:
            item = session.get(UnpackExecutionItem, item_id)
            if item is None:
                raise self._item_not_found()
            if item.status not in {
                UnpackItemStatus.EXECUTING.value,
                UnpackItemStatus.CLIENT_VERIFYING.value,
            }:
                raise self._conflict("影片项当前不允许进入最终辅种阶段")
            stage = dict(item.execution_state or {}).get("stage")
            if stage not in {
                "FILES_MATERIALIZED",
                "TORRENT_ADDED",
                "CLIENT_VERIFYING",
                "CLIENT_VERIFIED",
                "STARTING",
            }:
                raise self._conflict("影片项尚未完成文件落位")
            if item.execution_plan is None or item.execution_plan_digest is None:
                raise self._conflict("影片项缺少冻结执行计划")
            try:
                plan = unpack_execution_plan_from_payload(item.execution_plan)
            except ValueError as exc:
                raise self._conflict("冻结执行计划无法通过领域校验") from exc
            if (
                plan.plan_digest != item.execution_plan_digest
                or plan.item_id != item.id
                or plan.candidate_id != item.selected_candidate_id
                or plan.candidate_generation != item.candidate_generation
                or not plan.ready
            ):
                raise self._conflict("冻结执行计划与当前影片项绑定不一致")
            candidate = session.get(UnpackMatchCandidate, plan.candidate_id)
            if (
                candidate is None
                or candidate.item_id != item.id
                or candidate.generation != plan.candidate_generation
                or candidate.metainfo_digest != plan.metainfo_digest
            ):
                raise self._conflict("候选证据与冻结执行计划不一致")
            raw_ref = dict(candidate.raw_ref)
            version = raw_ref.get("site_config_version")
            adapter_site_id = raw_ref.get("adapter_site_id")
            torrent_id = raw_ref.get("torrent_id")
            if (
                isinstance(version, bool)
                or not isinstance(version, int)
                or version < 1
                or not isinstance(adapter_site_id, str)
                or not adapter_site_id
                or not isinstance(torrent_id, str)
                or not torrent_id
            ):
                raise self._conflict("候选缺少安全站点引用")
            execution_id = item.execution_id
            item_version = item.version
            candidate_id = candidate.id
            generation = item.candidate_generation
            site_config_id = candidate.site_id

        site = next(
            (
                entry
                for entry in self._site_provider.enabled_adapters()
                if entry.config_id == site_config_id
            ),
            None,
        )
        if site is None:
            raise self._conflict("候选站点当前不可用")
        if site.config_version != version or site.site_id != adapter_site_id:
            raise self._conflict("候选站点配置已变化，请重新匹配")
        binding = self._downloader_provider.write_binding(plan.target_downloader_id)
        if (
            binding.downloader_version != plan.target_downloader_version
            or binding.binding_digest != plan.target_downloader_binding_digest
            or binding.remote_save_path(Path(plan.output_directory)) != plan.target_remote_save_path
        ):
            raise ApplicationError(
                code="UNPACK_SEED_DOWNLOADER_CHANGED",
                status=409,
                title="目标下载器配置已经变化",
                detail="最终辅种要求下载器版本、binding digest 与 save path 仍和冻结计划一致",
            )
        ownership_digest = hashlib.sha256(f"{item_id}:{plan.plan_digest}".encode()).hexdigest()
        ownership_tag = f"packbreaker-unpack-{ownership_digest[:16]}"
        torrent_content: bytes | None = None
        if stage == "FILES_MATERIALIZED":
            # Before ADD, always re-fetch and validate the frozen metainfo.
            async with asyncio.timeout(self._fetch_timeout_seconds):
                payload = await site.adapter.fetch_torrent(torrent_id)
            if payload.site_id != adapter_site_id or payload.torrent_id != torrent_id:
                raise self._conflict("重新获取的 torrent 身份与候选不一致")
            meta = parse_torrent(payload.content)
            if meta.metainfo_digest != plan.metainfo_digest:
                raise ApplicationError(
                    code="UNPACK_SEED_TORRENT_CHANGED",
                    status=409,
                    title="候选 torrent 已变化",
                    detail="最终辅种前 metainfo digest 与冻结执行计划不一致",
                )
            expected_hashes = tuple(
                value.lower()
                for value in (meta.v1_info_hash, meta.v2_info_hash)
                if value is not None
            )
            if not expected_hashes:
                raise self._conflict("torrent 缺少可用于下载器确认的 info hash")
            torrent_content = payload.content
        else:
            # Only an APPLIED ADD journal can authorize client-only resume.
            # An unrelated torrent with the same hash must never be adopted.
            operation_type = _QB_ADD if isinstance(binding, QbittorrentWriteBinding) else _TR_ADD
            journal = self._load_by_key(_operation_key_values(item_id, plan, operation_type))
            if journal is None:
                raise self._reconcile_required("已添加 torrent 缺少 ADD 流水")
            intent = journal.intent if isinstance(journal.intent, dict) else {}
            after = journal.after_snapshot if isinstance(journal.after_snapshot, dict) else {}
            raw_hashes = intent.get("expected_hashes")
            if (
                journal.item_id != item_id
                or journal.operation_type != operation_type
                or journal.status != OperationStatus.APPLIED.value
                or journal.target
                != {
                    "downloader_id": plan.target_downloader_id,
                    "remote_save_path": plan.target_remote_save_path,
                }
                or intent.get("schema_version") != _SCHEMA_VERSION
                or intent.get("plan_digest") != plan.plan_digest
                or intent.get("ownership_tag") != ownership_tag
                or not isinstance(raw_hashes, list)
                or not raw_hashes
                or not all(
                    isinstance(value, str)
                    and len(value) in (40, 64)
                    and all(char in "0123456789abcdef" for char in value)
                    for value in raw_hashes
                )
                or len(raw_hashes) != len(set(raw_hashes))
                or after.get("ownership_tag") != ownership_tag
                or after.get("save_path") != plan.target_remote_save_path
                or after.get("torrent_hash") not in raw_hashes
                or dict(item.execution_state or {}).get("torrent_hash") not in raw_hashes
            ):
                raise self._reconcile_required("已添加 torrent 的冻结身份或流水不一致")
            expected_hashes = tuple(raw_hashes)
        return _Prepared(
            execution_id=execution_id,
            item_id=item_id,
            item_version=item_version,
            candidate_id=candidate_id,
            generation=generation,
            site_config_id=site_config_id,
            site_config_version=version,
            adapter_site_id=adapter_site_id,
            torrent_id=torrent_id,
            plan=plan,
            binding=binding,
            torrent_content=torrent_content,
            expected_hashes=expected_hashes,
            ownership_tag=ownership_tag,
        )

    async def _advance(self, prepared: _Prepared) -> None:
        stage = self._current_stage(prepared)
        if stage == "FILES_MATERIALIZED":
            state = await self._ensure_added(prepared)
            if prepared.plan.client_check_required:
                self._set_stage(
                    prepared,
                    "CLIENT_VERIFYING",
                    status=UnpackItemStatus.CLIENT_VERIFYING,
                    torrent_hash=_torrent_hash(state),
                )
                return
            if not _verification_complete(state):
                raise ApplicationError(
                    code="UNPACK_SEED_SKIP_CHECK_NOT_COMPLETE",
                    status=409,
                    title="跳过客户端校验未得到完整状态",
                    detail="冻结计划允许 skip checking，但下载器未报告完整停止状态，禁止直接启动",
                )
            self._set_stage(
                prepared,
                "CLIENT_VERIFIED",
                status=UnpackItemStatus.EXECUTING,
                torrent_hash=_torrent_hash(state),
            )
            stage = "CLIENT_VERIFIED"

        else:
            self._require_applied_add(prepared)

        if stage in {"TORRENT_ADDED", "CLIENT_VERIFYING"}:
            state = await self._ensure_verified(prepared)
            if not _verification_complete(state):
                return
            self._set_stage(
                prepared,
                "CLIENT_VERIFIED",
                status=UnpackItemStatus.EXECUTING,
                torrent_hash=_torrent_hash(state),
            )
            stage = "CLIENT_VERIFIED"

        if stage in {"CLIENT_VERIFIED", "STARTING"}:
            if prepared.plan.client_check_required:
                self._require_applied_verify(prepared)
            state = await self._ensure_started(prepared)
            if not _seeding(state):
                return
            self._mark_completed(prepared, state)

    def _require_applied_add(self, prepared: _Prepared) -> None:
        operation_type = (
            _QB_ADD if isinstance(prepared.binding, QbittorrentWriteBinding) else _TR_ADD
        )
        journal = self._load_by_key(_operation_key(prepared, operation_type))
        if (
            journal is None
            or journal.item_id != prepared.item_id
            or journal.operation_type != operation_type
            or journal.status != OperationStatus.APPLIED.value
            or journal.intent.get("plan_digest") != prepared.plan.plan_digest
            or journal.intent.get("ownership_tag") != prepared.ownership_tag
        ):
            raise self._reconcile_required("最终辅种 ADD 前置证据")

    def _require_applied_verify(self, prepared: _Prepared) -> None:
        operation_type = (
            _QB_VERIFY if isinstance(prepared.binding, QbittorrentWriteBinding) else _TR_VERIFY
        )
        journal = self._load_by_key(_operation_key(prepared, operation_type))
        if (
            journal is None
            or journal.item_id != prepared.item_id
            or journal.operation_type != operation_type
            or journal.status != OperationStatus.APPLIED.value
            or journal.intent.get("plan_digest") != prepared.plan.plan_digest
            or not isinstance(journal.after_snapshot, dict)
            or journal.after_snapshot.get("verification_complete") is not True
            or journal.after_snapshot.get("request_accepted") is not True
        ):
            raise self._reconcile_required("客户端 VERIFY 前置证据")

    async def _ensure_added(
        self,
        prepared: _Prepared,
    ) -> QbittorrentTorrentState | TransmissionTorrentState:
        torrent_content = prepared.torrent_content
        if torrent_content is None:
            raise self._reconcile_required("ADD 前缺少已验证 torrent 内容")
        is_qb = isinstance(prepared.binding, QbittorrentWriteBinding)
        operation_type = _QB_ADD if is_qb else _TR_ADD
        key = _operation_key(prepared, operation_type)
        target = {
            "downloader_id": prepared.plan.target_downloader_id,
            "remote_save_path": prepared.plan.target_remote_save_path,
        }
        intent = {
            "schema_version": _SCHEMA_VERSION,
            "plan_digest": prepared.plan.plan_digest,
            "expected_hashes": list(prepared.expected_hashes),
            "ownership_tag": prepared.ownership_tag,
            "torrent_payload_digest": hashlib.sha256(torrent_content).hexdigest(),
            "skip_checking": is_qb and not prepared.plan.client_check_required,
        }
        journal = self._load_by_key(key)
        if journal is None:
            observed = await self._states(prepared)
            if observed:
                raise ApplicationError(
                    code="UNPACK_SEED_TORRENT_ALREADY_EXISTS",
                    status=409,
                    title="目标下载器已存在同一 torrent",
                    detail="add intent 建立前已存在同 hash 任务，PackBreaker 不会认领外部 torrent",
                )
            journal, _ = self._record_intent(
                prepared.item_id,
                operation_type,
                key=key,
                target=target,
                intent=intent,
                before_snapshot={
                    "torrent_absent": True,
                    "checked_hashes": list(prepared.expected_hashes),
                },
            )
        else:
            self._assert_same_journal(journal, operation_type, target, intent)
            if journal.status == OperationStatus.RECONCILE_REQUIRED.value:
                raise self._reconcile_required("torrent add")
            observed = await self._states(prepared)
            if observed:
                state = self._require_owned_state(prepared, observed)
                self._assert_add_state_safe(journal.id, state)
                if journal.status != OperationStatus.APPLIED.value:
                    self._mark_applied(journal.id, _state_snapshot(state, prepared.ownership_tag))
                return state
            if journal.status == OperationStatus.APPLIED.value:
                self._mark_reconcile(journal.id, "UNPACK_SEED_APPLIED_TORRENT_MISSING")
                raise self._reconcile_required("已确认 torrent add")

        try:
            if is_qb:
                qb_binding = cast(QbittorrentWriteBinding, prepared.binding)
                qb_result = await qb_binding.adapter.add_torrent(
                    QbittorrentAddRequest(
                        torrent_content=torrent_content,
                        save_path=prepared.plan.target_remote_save_path,
                        verification_level=VerificationLevel.FULL_VERIFIED,
                        skip_checking=not prepared.plan.client_check_required,
                        tags=(prepared.ownership_tag,),
                        paused=True,
                    )
                )
                if (
                    qb_result.failure_count
                    or qb_result.pending_count
                    or qb_result.success_count != 1
                ):
                    raise ApplicationError(
                        code="UNPACK_SEED_ADD_NOT_CONFIRMED",
                        status=409,
                        title="qBittorrent 添加未确认",
                        detail="qBittorrent add 未返回单个确定成功结果",
                    )
                if qb_result.added_torrent_ids and not set(qb_result.added_torrent_ids).issubset(
                    set(prepared.expected_hashes)
                ):
                    self._mark_reconcile(journal.id, "UNPACK_SEED_ADD_HASH_MISMATCH")
                    raise self._conflict("qBittorrent 返回的 torrent hash 与冻结计划不一致")
            else:
                transmission_binding = cast(TransmissionWriteBinding, prepared.binding)
                transmission_result = await transmission_binding.adapter.add_torrent(
                    TransmissionAddRequest(
                        torrent_content=torrent_content,
                        save_path=prepared.plan.target_remote_save_path,
                        labels=(prepared.ownership_tag,),
                        paused=True,
                    )
                )
                if transmission_result.torrent_hash not in prepared.expected_hashes:
                    self._mark_reconcile(journal.id, "UNPACK_SEED_ADD_HASH_MISMATCH")
                    raise self._conflict("Transmission 返回的 torrent hash 与冻结计划不一致")
        except DownloaderAdapterError:
            observed = await self._states(prepared)
            if observed:
                state = self._require_owned_state(prepared, observed)
                self._assert_add_state_safe(journal.id, state)
                self._mark_applied(journal.id, _state_snapshot(state, prepared.ownership_tag))
                return state
            self._mark_reconcile(journal.id, "UNPACK_SEED_ADD_RESULT_UNKNOWN")
            raise

        observed = await self._states(prepared)
        if not observed:
            self._mark_reconcile(journal.id, "UNPACK_SEED_ADD_NOT_VISIBLE")
            raise self._conflict("下载器 add 返回成功但 torrent 尚不可见")
        state = self._require_owned_state(prepared, observed)
        self._assert_add_state_safe(journal.id, state)
        self._mark_applied(journal.id, _state_snapshot(state, prepared.ownership_tag))
        return state

    def _assert_add_state_safe(
        self,
        journal_id: str,
        state: QbittorrentTorrentState | TransmissionTorrentState,
    ) -> None:
        if _stopped(state) or _checking(state):
            return
        self._mark_reconcile(journal_id, "UNPACK_SEED_ADD_NOT_PAUSED")
        raise ApplicationError(
            code="UNPACK_SEED_ADD_NOT_PAUSED",
            status=409,
            title="暂停添加约束未保持",
            detail="torrent add 后任务既非停止也非校验中状态，禁止继续最终辅种",
        )

    async def _ensure_verified(
        self,
        prepared: _Prepared,
    ) -> QbittorrentTorrentState | TransmissionTorrentState:
        state = self._require_owned_state(prepared, await self._states(prepared))
        is_qb = isinstance(prepared.binding, QbittorrentWriteBinding)
        operation_type = _QB_VERIFY if is_qb else _TR_VERIFY
        key = _operation_key(prepared, operation_type)
        target = {
            "downloader_id": prepared.plan.target_downloader_id,
            "torrent_hash": _torrent_hash(state),
        }
        intent = {
            "schema_version": _SCHEMA_VERSION,
            "plan_digest": prepared.plan.plan_digest,
            "ownership_tag": prepared.ownership_tag,
            "remote_save_path": prepared.plan.target_remote_save_path,
        }
        journal = self._load_by_key(key)
        if journal is None:
            if _checking(state):
                return state
            journal, _ = self._record_intent(
                prepared.item_id,
                operation_type,
                key=key,
                target=target,
                intent=intent,
                before_snapshot=_state_snapshot(state, prepared.ownership_tag),
            )
        else:
            self._assert_same_journal(journal, operation_type, target, intent)
            if journal.status == OperationStatus.RECONCILE_REQUIRED.value:
                raise self._reconcile_required("客户端校验")
            if journal.status == OperationStatus.APPLIED.value:
                if not _verification_complete(state):
                    self._mark_reconcile(journal.id, "UNPACK_SEED_VERIFIED_STATE_CHANGED")
                    raise self._reconcile_required("已确认客户端校验")
                return state
            request_accepted = bool(
                isinstance(journal.after_snapshot, dict)
                and journal.after_snapshot.get("request_accepted") is True
            )
            if request_accepted:
                if _checking(state):
                    return state
                if _verification_complete(state):
                    self._mark_applied(
                        journal.id,
                        {
                            **_state_snapshot(state, prepared.ownership_tag),
                            "request_accepted": True,
                            "verification_complete": True,
                        },
                    )
                    return state
                if _verification_incomplete(state):
                    self._mark_applied(
                        journal.id,
                        {
                            **_state_snapshot(state, prepared.ownership_tag),
                            "request_accepted": True,
                            "verification_complete": False,
                        },
                    )
                    raise ApplicationError(
                        code="UNPACK_SEED_CLIENT_VERIFY_FAILED",
                        status=409,
                        title="客户端下载器校验未通过",
                        detail="客户端下载器完成校验后仍报告数据不完整，禁止启动做种",
                    )
                return state
            self._mark_reconcile(journal.id, "UNPACK_SEED_VERIFY_RESULT_UNKNOWN")
            raise self._reconcile_required("客户端 VERIFY 请求")

        try:
            if is_qb:
                qb_binding = cast(QbittorrentWriteBinding, prepared.binding)
                await qb_binding.adapter.recheck_torrent(_torrent_hash(state))
            else:
                transmission_binding = cast(TransmissionWriteBinding, prepared.binding)
                await transmission_binding.adapter.verify_torrent(_torrent_hash(state))
        except DownloaderAdapterError:
            current = self._require_owned_state(prepared, await self._states(prepared))
            if _checking(current):
                self._update_after_snapshot(
                    journal.id,
                    {
                        **_state_snapshot(current, prepared.ownership_tag),
                        "request_accepted": True,
                    },
                )
                return current
            self._mark_reconcile(journal.id, "UNPACK_SEED_VERIFY_RESULT_UNKNOWN")
            raise

        current = self._require_owned_state(prepared, await self._states(prepared))
        self._update_after_snapshot(
            journal.id,
            {
                **_state_snapshot(current, prepared.ownership_tag),
                "request_accepted": True,
            },
        )
        if _verification_complete(current):
            self._mark_applied(
                journal.id,
                {
                    **_state_snapshot(current, prepared.ownership_tag),
                    "request_accepted": True,
                    "verification_complete": True,
                },
            )
        elif _verification_incomplete(current) and not _checking(current):
            self._mark_applied(
                journal.id,
                {
                    **_state_snapshot(current, prepared.ownership_tag),
                    "request_accepted": True,
                    "verification_complete": False,
                },
            )
            raise ApplicationError(
                code="UNPACK_SEED_CLIENT_VERIFY_FAILED",
                status=409,
                title="客户端下载器校验未通过",
                detail="客户端下载器完成校验后仍报告数据不完整，禁止启动做种",
            )
        return current

    async def _ensure_started(
        self,
        prepared: _Prepared,
    ) -> QbittorrentTorrentState | TransmissionTorrentState:
        state = self._require_owned_state(prepared, await self._states(prepared))
        if _seeding(state):
            operation_type = (
                _QB_START if isinstance(prepared.binding, QbittorrentWriteBinding) else _TR_START
            )
            key = _operation_key(prepared, operation_type)
            journal = self._load_by_key(key)
            if journal is None:
                raise self._reconcile_required("缺少 torrent start intent")
            self._assert_same_journal(
                journal,
                operation_type,
                {
                    "downloader_id": prepared.plan.target_downloader_id,
                    "torrent_hash": _torrent_hash(state),
                },
                {
                    "schema_version": _SCHEMA_VERSION,
                    "plan_digest": prepared.plan.plan_digest,
                    "ownership_tag": prepared.ownership_tag,
                    "remote_save_path": prepared.plan.target_remote_save_path,
                },
            )
            if journal.status == OperationStatus.RECONCILE_REQUIRED.value:
                raise self._reconcile_required("torrent start")
            if journal.status != OperationStatus.APPLIED.value:
                before = journal.before_snapshot or {}
                if (
                    before.get("verification_complete") is not True
                    or before.get("seeding") is not False
                ):
                    self._mark_reconcile(journal.id, "UNPACK_SEED_START_BEFORE_INVALID")
                    raise self._reconcile_required("torrent start 的前置状态")
                self._mark_applied(
                    journal.id,
                    {
                        **_state_snapshot(state, prepared.ownership_tag),
                        "request_accepted": True,
                    },
                )
            return state
        if not _verification_complete(state):
            raise self._conflict("只有客户端确认完整且停止的 torrent 才允许启动")

        is_qb = isinstance(prepared.binding, QbittorrentWriteBinding)
        operation_type = _QB_START if is_qb else _TR_START
        key = _operation_key(prepared, operation_type)
        target = {
            "downloader_id": prepared.plan.target_downloader_id,
            "torrent_hash": _torrent_hash(state),
        }
        intent = {
            "schema_version": _SCHEMA_VERSION,
            "plan_digest": prepared.plan.plan_digest,
            "ownership_tag": prepared.ownership_tag,
            "remote_save_path": prepared.plan.target_remote_save_path,
        }
        journal = self._load_by_key(key)
        if journal is None:
            journal, _ = self._record_intent(
                prepared.item_id,
                operation_type,
                key=key,
                target=target,
                intent=intent,
                before_snapshot=_state_snapshot(state, prepared.ownership_tag),
            )
        else:
            self._assert_same_journal(journal, operation_type, target, intent)
            if journal.status == OperationStatus.RECONCILE_REQUIRED.value:
                raise self._reconcile_required("torrent start")
            if journal.status == OperationStatus.APPLIED.value:
                if not _seeding(state):
                    self._mark_reconcile(journal.id, "UNPACK_SEED_STARTED_STATE_CHANGED")
                    raise self._reconcile_required("已确认 torrent start")
                return state
            request_accepted = bool(
                isinstance(journal.after_snapshot, dict)
                and journal.after_snapshot.get("request_accepted") is True
            )
            if request_accepted:
                if _seeding(state):
                    self._mark_applied(
                        journal.id,
                        {
                            **_state_snapshot(state, prepared.ownership_tag),
                            "request_accepted": True,
                        },
                    )
                    return state
                if _verification_complete(state):
                    return state
                self._mark_reconcile(journal.id, "UNPACK_SEED_START_STATE_CHANGED")
                raise self._reconcile_required("torrent start")

        self._set_stage(
            prepared,
            "STARTING",
            status=UnpackItemStatus.EXECUTING,
            torrent_hash=_torrent_hash(state),
        )
        try:
            if is_qb:
                qb_binding = cast(QbittorrentWriteBinding, prepared.binding)
                await qb_binding.adapter.start_torrent(_torrent_hash(state))
            else:
                transmission_binding = cast(TransmissionWriteBinding, prepared.binding)
                await transmission_binding.adapter.start_torrent(_torrent_hash(state))
        except DownloaderAdapterError:
            current = self._require_owned_state(prepared, await self._states(prepared))
            if _seeding(current):
                self._mark_applied(
                    journal.id,
                    {
                        **_state_snapshot(current, prepared.ownership_tag),
                        "request_accepted": True,
                    },
                )
                return current
            self._mark_reconcile(journal.id, "UNPACK_SEED_START_RESULT_UNKNOWN")
            raise

        current = self._require_owned_state(prepared, await self._states(prepared))
        if _seeding(current):
            self._mark_applied(
                journal.id,
                {
                    **_state_snapshot(current, prepared.ownership_tag),
                    "request_accepted": True,
                },
            )
            return current
        if _verification_complete(current):
            self._update_after_snapshot(
                journal.id,
                {
                    **_state_snapshot(current, prepared.ownership_tag),
                    "request_accepted": True,
                },
            )
            return current
        self._mark_reconcile(journal.id, "UNPACK_SEED_START_STATE_INVALID")
        raise self._conflict("torrent start 后进入不可解释状态")

    async def _states(
        self,
        prepared: _Prepared,
    ) -> tuple[QbittorrentTorrentState | TransmissionTorrentState, ...]:
        if isinstance(prepared.binding, QbittorrentWriteBinding):
            return await prepared.binding.adapter.get_torrents(prepared.expected_hashes)
        return await prepared.binding.adapter.get_torrents(prepared.expected_hashes)

    def _require_owned_state(
        self,
        prepared: _Prepared,
        observed: tuple[QbittorrentTorrentState | TransmissionTorrentState, ...],
    ) -> QbittorrentTorrentState | TransmissionTorrentState:
        owned = tuple(
            state
            for state in observed
            if _torrent_hash(state) in prepared.expected_hashes
            and _save_path(state) == prepared.plan.target_remote_save_path
            and prepared.ownership_tag in _ownership_values(state)
        )
        if len(observed) != 1 or len(owned) != 1:
            raise ApplicationError(
                code="UNPACK_SEED_OWNERSHIP_UNPROVEN",
                status=409,
                title="无法证明下载器任务归 PackBreaker 所有",
                detail="torrent hash、save path 与 ownership tag/label 必须同时唯一匹配",
            )
        return owned[0]

    def _current_stage(self, prepared: _Prepared) -> str:
        with self._session_factory() as session:
            item = session.get(UnpackExecutionItem, prepared.item_id)
            if item is None:
                raise self._item_not_found()
            if (
                item.execution_plan_digest != prepared.plan.plan_digest
                or item.selected_candidate_id != prepared.candidate_id
                or item.candidate_generation != prepared.generation
            ):
                raise self._conflict("最终辅种期间 item/plan/candidate 已变化")
            stage = dict(item.execution_state or {}).get("stage")
            if not isinstance(stage, str):
                raise self._conflict("影片项缺少执行 checkpoint")
            return stage

    def _set_stage(
        self,
        prepared: _Prepared,
        stage: str,
        *,
        status: UnpackItemStatus,
        torrent_hash: str,
    ) -> None:
        with self._session_factory() as session:
            begin_immediate_write(session)
            item = session.get(UnpackExecutionItem, prepared.item_id)
            if item is None:
                raise self._item_not_found()
            if (
                item.execution_plan_digest != prepared.plan.plan_digest
                or item.selected_candidate_id != prepared.candidate_id
                or item.candidate_generation != prepared.generation
            ):
                raise self._conflict("写入最终辅种 checkpoint 时 item 已变化")
            item.status = status.value
            item.execution_state = {
                "stage": stage,
                "plan_digest": prepared.plan.plan_digest,
                "torrent_hash": torrent_hash,
                "ownership_tag": prepared.ownership_tag,
            }
            item.updated_at = utc_now()
            item.version += 1
            session.commit()

    def _mark_completed(
        self,
        prepared: _Prepared,
        state: QbittorrentTorrentState | TransmissionTorrentState,
    ) -> None:
        with self._session_factory() as session:
            begin_immediate_write(session)
            item = session.get(UnpackExecutionItem, prepared.item_id)
            execution = session.get(UnpackExecution, prepared.execution_id)
            if item is None or execution is None:
                raise self._conflict("完成最终辅种时 item/execution 已不存在")
            if (
                item.execution_plan_digest != prepared.plan.plan_digest
                or item.selected_candidate_id != prepared.candidate_id
                or item.candidate_generation != prepared.generation
            ):
                raise self._conflict("完成最终辅种时 item 已变化")
            now = utc_now()
            item.status = UnpackItemStatus.COMPLETED.value
            item.execution_state = {
                "stage": "COMPLETED",
                "plan_digest": prepared.plan.plan_digest,
                "torrent_hash": _torrent_hash(state),
                "ownership_tag": prepared.ownership_tag,
                "completed_at": now.isoformat(),
            }
            item.last_error_code = None
            item.last_error_message = None
            item.updated_at = now
            item.version += 1
            session.flush()
            _refresh_execution_state(session, execution)
            session.commit()

    def _mark_error(self, item_id: str, *, code: str, message: str) -> None:
        with self._session_factory() as session:
            begin_immediate_write(session)
            item = session.get(UnpackExecutionItem, item_id)
            if item is None or item.status not in {
                UnpackItemStatus.EXECUTING.value,
                UnpackItemStatus.CLIENT_VERIFYING.value,
            }:
                return
            execution = session.get(UnpackExecution, item.execution_id)
            if execution is None:
                return
            now = utc_now()
            if item.status == UnpackItemStatus.CLIENT_VERIFYING.value:
                item.status = UnpackItemStatus.EXECUTING.value
                session.flush()
            item.status = UnpackItemStatus.EXECUTION_ERROR.value
            item.last_error_code = code
            item.last_error_message = message
            item.updated_at = now
            item.version += 1
            session.flush()
            _refresh_execution_state(session, execution)
            session.commit()

    def _report(
        self,
        execution_id: str,
        *,
        processed_count: int,
    ) -> UnpackSeedingReport:
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

            return UnpackSeedingReport(
                execution_id=execution_id,
                processed_count=processed_count,
                client_verifying_count=count(UnpackItemStatus.CLIENT_VERIFYING),
                completed_count=count(UnpackItemStatus.COMPLETED),
                error_count=count(UnpackItemStatus.EXECUTION_ERROR),
                execution_status=UnpackExecutionStatus(execution.status),
            )

    def _load_by_key(self, key: str) -> UnpackExternalOperationJournal | None:
        with self._session_factory() as session:
            row = session.scalar(
                select(UnpackExternalOperationJournal).where(
                    UnpackExternalOperationJournal.idempotency_key == key
                )
            )
            if row is not None:
                session.expunge(row)
            return row

    def _record_intent(
        self,
        item_id: str,
        operation_type: str,
        *,
        key: str,
        target: dict[str, Any],
        intent: dict[str, Any],
        before_snapshot: dict[str, Any] | None,
    ) -> tuple[UnpackExternalOperationJournal, bool]:
        with self._session_factory() as session:
            begin_immediate_write(session)
            existing = session.scalar(
                select(UnpackExternalOperationJournal).where(
                    UnpackExternalOperationJournal.idempotency_key == key
                )
            )
            if existing is not None:
                self._assert_same_journal(existing, operation_type, target, intent)
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

    def _assert_same_journal(
        self,
        journal: UnpackExternalOperationJournal,
        operation_type: str,
        target: dict[str, Any],
        intent: dict[str, Any],
    ) -> None:
        if (
            journal.operation_type != operation_type
            or journal.target != target
            or journal.intent != intent
        ):
            raise self._conflict("同一最终辅种幂等键绑定了不同 intent")

    def _mark_applied(self, journal_id: str, after_snapshot: dict[str, Any]) -> None:
        with self._session_factory() as session:
            begin_immediate_write(session)
            row = session.get(UnpackExternalOperationJournal, journal_id)
            if row is None:
                raise self._conflict("最终辅种 journal 不存在")
            if row.status == OperationStatus.APPLIED.value:
                return
            if row.status not in {
                OperationStatus.INTENT_RECORDED.value,
                OperationStatus.RECONCILE_REQUIRED.value,
            }:
                raise self._conflict("最终辅种 journal 当前状态不能确认成功")
            row.status = OperationStatus.APPLIED.value
            row.after_snapshot = after_snapshot
            row.last_error_code = None
            row.updated_at = utc_now()
            session.commit()

    def _update_after_snapshot(self, journal_id: str, value: dict[str, Any]) -> None:
        with self._session_factory() as session:
            begin_immediate_write(session)
            row = session.get(UnpackExternalOperationJournal, journal_id)
            if row is None or row.status != OperationStatus.INTENT_RECORDED.value:
                return
            row.after_snapshot = value
            row.updated_at = utc_now()
            session.commit()

    def _mark_reconcile(self, journal_id: str, code: str) -> None:
        with self._session_factory() as session:
            begin_immediate_write(session)
            row = session.get(UnpackExternalOperationJournal, journal_id)
            if row is None or row.status == OperationStatus.RECONCILE_REQUIRED.value:
                return
            row.status = OperationStatus.RECONCILE_REQUIRED.value
            row.last_error_code = code
            row.updated_at = utc_now()
            session.commit()

    @staticmethod
    def _reconcile_required(stage: str) -> ApplicationError:
        return ApplicationError(
            code="UNPACK_SEED_RECONCILE_REQUIRED",
            status=409,
            title="最终辅种需要对账",
            detail=f"{stage} 的上次外部写结果无法安全证明，禁止盲目重放",
        )

    @staticmethod
    def _invalid(detail: str) -> ApplicationError:
        return ApplicationError(
            code="UNPACK_SEED_INVALID",
            status=422,
            title="最终辅种参数无效",
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
            code="UNPACK_SEED_CONFLICT",
            status=409,
            title="数据拆包最终辅种冲突",
            detail=detail,
        )


def _operation_key(prepared: _Prepared, operation_type: str) -> str:
    return _operation_key_values(prepared.item_id, prepared.plan, operation_type)


def _operation_key_values(item_id: str, plan: UnpackExecutionPlan, operation_type: str) -> str:
    return hashlib.sha256(
        (
            f"{_SCHEMA_VERSION}:{item_id}:{plan.plan_digest}:"
            f"{plan.target_downloader_id}:{operation_type}"
        ).encode()
    ).hexdigest()


def _torrent_hash(state: QbittorrentTorrentState | TransmissionTorrentState) -> str:
    return state.torrent_hash.lower()


def _save_path(state: QbittorrentTorrentState | TransmissionTorrentState) -> str:
    if isinstance(state, QbittorrentTorrentState):
        return state.save_path
    return state.download_dir


def _ownership_values(
    state: QbittorrentTorrentState | TransmissionTorrentState,
) -> tuple[str, ...]:
    if isinstance(state, QbittorrentTorrentState):
        return state.tags
    return state.labels


def _checking(state: QbittorrentTorrentState | TransmissionTorrentState) -> bool:
    return state.checking


def _stopped(state: QbittorrentTorrentState | TransmissionTorrentState) -> bool:
    return state.stopped


def _verification_complete(
    state: QbittorrentTorrentState | TransmissionTorrentState,
) -> bool:
    return state.verification_complete


def _verification_incomplete(
    state: QbittorrentTorrentState | TransmissionTorrentState,
) -> bool:
    return state.verification_incomplete


def _seeding(state: QbittorrentTorrentState | TransmissionTorrentState) -> bool:
    return state.seeding


def _state_snapshot(
    state: QbittorrentTorrentState | TransmissionTorrentState,
    ownership_tag: str,
) -> dict[str, Any]:
    if isinstance(state, QbittorrentTorrentState):
        return {
            "torrent_hash": state.torrent_hash,
            "save_path": state.save_path,
            "state": state.state,
            "tags": list(state.tags),
            "progress": state.progress,
            "ownership_tag": ownership_tag,
            "checking": state.checking,
            "verification_complete": state.verification_complete,
            "seeding": state.seeding,
        }
    return {
        "torrent_hash": state.torrent_hash,
        "save_path": state.download_dir,
        "status": state.status,
        "labels": list(state.labels),
        "percent_done": state.percent_done,
        "recheck_progress": state.recheck_progress,
        "ownership_tag": ownership_tag,
        "checking": state.checking,
        "verification_complete": state.verification_complete,
        "seeding": state.seeding,
    }


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

    execution.error_count = count(UnpackItemStatus.MATCH_ERROR) + count(
        UnpackItemStatus.EXECUTION_ERROR
    )
    execution.completed_count = count(UnpackItemStatus.COMPLETED)
    active = sum(
        count(status)
        for status in (
            UnpackItemStatus.CONTENT_VERIFIED,
            UnpackItemStatus.PLAN_PENDING,
            UnpackItemStatus.EXECUTING,
            UnpackItemStatus.CLIENT_VERIFYING,
        )
    )
    now = utc_now()
    if active:
        execution.status = UnpackExecutionStatus.EXECUTING.value
        execution.finished_at = None
    elif execution.error_count:
        execution.status = UnpackExecutionStatus.COMPLETED_WITH_ERRORS.value
        execution.finished_at = now
    elif execution.completed_count == execution.total_count:
        execution.status = UnpackExecutionStatus.COMPLETED.value
        execution.finished_at = now
    execution.updated_at = now
    execution.version += 1


def _safe_error(
    exc: ApplicationError | DownloaderAdapterError | SiteAdapterError | TimeoutError | ValueError,
) -> tuple[str, str]:
    if isinstance(exc, ApplicationError):
        return exc.code, exc.detail
    if isinstance(exc, DownloaderAdapterError):
        return exc.code, "最终辅种下载器操作失败"
    if isinstance(exc, SiteAdapterError):
        return exc.code, "最终辅种获取 torrent 失败"
    if isinstance(exc, TimeoutError):
        return "UNPACK_SEED_TIMEOUT", "最终辅种网络或外部操作超时"
    return "UNPACK_SEED_INVALID", str(exc)
