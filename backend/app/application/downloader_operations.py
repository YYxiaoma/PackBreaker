from __future__ import annotations

import asyncio
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Protocol
from weakref import WeakValueDictionary

from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.errors import ApplicationError
from backend.app.domain.downloader import normalize_remote_path
from backend.app.domain.errors import DomainViolation
from backend.app.domain.idempotency import downloader_operation_key
from backend.app.domain.operation import OperationStatus
from backend.app.domain.verification import VerificationLevel
from backend.app.infrastructure.adapters.downloaders import (
    DownloaderAdapterError,
    QbittorrentAddRequest,
    QbittorrentTorrentState,
    QbittorrentWriteAdapter,
)
from backend.app.infrastructure.persistence.repositories import (
    OperationIntent,
    OperationJournalRepository,
)
from backend.app.infrastructure.torrent_parser import parse_torrent

QBITTORRENT_ADD_OPERATION = "QBITTORRENT_ADD"
QBITTORRENT_OPERATION_SCHEMA_VERSION = "packbreaker-qbittorrent-operation-v1"
QBITTORRENT_RECHECK_OPERATION = "QBITTORRENT_RECHECK"
QBITTORRENT_RECHECK_SCHEMA_VERSION = "packbreaker-qbittorrent-recheck-v1"
QBITTORRENT_START_OPERATION = "QBITTORRENT_START"
QBITTORRENT_START_SCHEMA_VERSION = "packbreaker-qbittorrent-start-v1"
QBITTORRENT_REMOVE_OPERATION = "QBITTORRENT_REMOVE"
QBITTORRENT_REMOVE_SCHEMA_VERSION = "packbreaker-qbittorrent-remove-v1"
QBITTORRENT_RECONCILABLE_OPERATIONS = frozenset(
    {
        QBITTORRENT_ADD_OPERATION,
        QBITTORRENT_RECHECK_OPERATION,
        QBITTORRENT_START_OPERATION,
    }
)
_OPERATION_LOCKS: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()


@dataclass(frozen=True, slots=True)
class QbittorrentAddOperationRequest:
    task_id: str
    candidate_key: str
    downloader_id: str
    downloader_version: int
    execution_plan_id: str
    execution_plan_digest: str
    expected_metainfo_digest: str
    torrent_content: bytes
    remote_save_path: str
    verification_level: VerificationLevel
    skip_checking: bool = False
    category: str | None = None
    tags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class QbittorrentAddOperationResult:
    journal_id: str
    torrent_hash: str
    save_path: str
    state: str
    ownership_tag: str
    skip_checking: bool
    replayed: bool
    recovered_after_unknown_result: bool


@dataclass(frozen=True, slots=True)
class QbittorrentRecheckOperationRequest:
    task_id: str
    candidate_key: str
    downloader_id: str
    downloader_version: int
    execution_plan_id: str
    qbit_add_journal_id: str
    torrent_hash: str
    remote_save_path: str
    ownership_tag: str


@dataclass(frozen=True, slots=True)
class QbittorrentRecheckOperationResult:
    journal_id: str
    torrent_hash: str
    save_path: str
    state: str
    progress: float
    ownership_tag: str
    checking: bool
    verification_complete: bool
    verification_incomplete: bool
    checking_observed: bool
    completion_proven: bool
    replayed: bool
    recovered_after_unknown_result: bool


@dataclass(frozen=True, slots=True)
class QbittorrentStartOperationRequest:
    task_id: str
    candidate_key: str
    downloader_id: str
    downloader_version: int
    execution_plan_id: str
    qbit_add_journal_id: str
    verification_journal_id: str | None
    torrent_hash: str
    remote_save_path: str
    ownership_tag: str


@dataclass(frozen=True, slots=True)
class QbittorrentStartOperationResult:
    journal_id: str
    torrent_hash: str
    save_path: str
    state: str
    progress: float
    ownership_tag: str
    seeding: bool
    replayed: bool
    recovered_after_unknown_result: bool


@dataclass(frozen=True, slots=True)
class QbittorrentRemoveOperationRequest:
    task_id: str
    candidate_key: str
    downloader_id: str
    downloader_version: int
    execution_plan_id: str
    qbit_add_journal_id: str
    torrent_hash: str
    remote_save_path: str
    ownership_tag: str


@dataclass(frozen=True, slots=True)
class QbittorrentRemoveOperationResult:
    journal_id: str
    torrent_hash: str
    removed: bool
    replayed: bool
    recovered_after_unknown_result: bool


@dataclass(frozen=True, slots=True)
class QbittorrentJournalReconcileResult:
    journal_id: str
    operation_type: str
    status: OperationStatus
    replayed: bool


class QbittorrentWriteBindingPort(Protocol):
    @property
    def downloader_id(self) -> str: ...

    @property
    def downloader_version(self) -> int: ...

    @property
    def capabilities(self) -> Mapping[str, Any]: ...

    @property
    def adapter(self) -> QbittorrentWriteAdapter: ...


@dataclass(frozen=True, slots=True)
class _JournalView:
    id: str
    task_id: str
    idempotency_key: str
    operation_type: str
    target: dict[str, Any]
    intent: dict[str, Any]
    status: OperationStatus
    before_snapshot: dict[str, Any] | None
    after_snapshot: dict[str, Any] | None


class QbittorrentAddOperationService:
    """以 operation journal 包围 qB 添加，未知结果先查询真实状态再决定是否重试。"""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    async def execute(
        self,
        request: QbittorrentAddOperationRequest,
        binding: QbittorrentWriteBindingPort,
    ) -> QbittorrentAddOperationResult:
        if (
            binding.downloader_id != request.downloader_id
            or binding.downloader_version != request.downloader_version
        ):
            raise ApplicationError(
                code="DOWNLOADER_CONFIG_CHANGED",
                status=409,
                title="qBittorrent 配置已经变化",
                detail="operation intent 必须绑定同一个下载器 ID 与配置版本",
            )
        if request.skip_checking and binding.capabilities.get("supports_skip_checking") is not True:
            raise ApplicationError(
                code="DOWNLOADER_SKIP_CHECKING_BLOCKED",
                status=409,
                title="qBittorrent 跳过校验被阻断",
                detail="当前下载器能力快照未声明支持 skip_checking",
            )
        prepared = self._prepare(request)
        async with _operation_lock(prepared.operation_key):
            return await self._execute_prepared(prepared, binding)

    async def _execute_prepared(
        self,
        prepared: _PreparedAdd,
        binding: QbittorrentWriteBindingPort,
    ) -> QbittorrentAddOperationResult:
        request = prepared.request
        adapter = binding.adapter
        existing = self._load_by_key(prepared.operation_key)
        if existing is not None:
            self._assert_same_intent(existing, prepared)
            if existing.status is OperationStatus.APPLIED:
                state = await self._verify_applied(existing, prepared, adapter)
                return _result(existing, state, prepared, replayed=True, recovered=False)
            if existing.status is not OperationStatus.INTENT_RECORDED:
                raise _journal_not_executable(existing)

            observed = await self._get_states(adapter, prepared.expected_hashes)
            if observed:
                state = await self._confirm_owned_state(
                    existing,
                    prepared,
                    adapter,
                    observed,
                    unknown_result=True,
                )
                applied = self._transition(
                    existing.id,
                    OperationStatus.INTENT_RECORDED,
                    OperationStatus.APPLIED,
                    after_snapshot=_state_snapshot(state, prepared.ownership_tag),
                )
                return _result(applied, state, prepared, replayed=True, recovered=True)
            journal = existing
            replayed = True
        else:
            observed = await self._get_states(adapter, prepared.expected_hashes)
            if observed:
                raise ApplicationError(
                    code="DOWNLOADER_TORRENT_ALREADY_EXISTS",
                    status=409,
                    title="qBittorrent 已存在同一 torrent",
                    detail="添加 intent 建立前已发现相同 torrent，PackBreaker 不会认领外部任务",
                )
            journal = self._record_intent(prepared)
            replayed = False

        add_request = QbittorrentAddRequest(
            torrent_content=request.torrent_content,
            save_path=prepared.remote_save_path,
            verification_level=request.verification_level,
            skip_checking=request.skip_checking,
            tags=tuple((*request.tags, prepared.ownership_tag)),
            category=request.category,
            paused=True,
        )
        try:
            add_result = await adapter.add_torrent(add_request)
        except DownloaderAdapterError as exc:
            raise _adapter_application_error(exc, "qBittorrent 添加结果未知") from exc

        if add_result.failure_count:
            raise ApplicationError(
                code="DOWNLOADER_ADD_REJECTED",
                status=502,
                title="qBittorrent 拒绝添加 torrent",
                detail="添加接口报告失败，operation intent 保留以供后续对账",
            )
        if add_result.pending_count:
            raise ApplicationError(
                code="DOWNLOADER_ADD_PENDING",
                status=409,
                title="qBittorrent 添加仍在等待",
                detail="添加结果尚未落定，后续重试会先查询真实 torrent 状态",
            )
        if add_result.success_count != 1:
            self._mark_reconcile(journal.id)
            raise _state_mismatch("qBittorrent 添加计数与单 torrent 请求不一致")
        if add_result.added_torrent_ids and not set(add_result.added_torrent_ids).issubset(
            set(prepared.expected_hashes)
        ):
            self._mark_reconcile(journal.id)
            raise _state_mismatch("qBittorrent 返回的 torrent 身份与 metainfo 不一致")

        observed = await self._get_states(adapter, prepared.expected_hashes)
        if not observed:
            raise ApplicationError(
                code="DOWNLOADER_ADD_NOT_CONFIRMED",
                status=502,
                title="qBittorrent 添加未确认",
                detail="添加接口返回成功但实际 torrent 尚不可见，operation intent 保留并禁止继续",
            )
        state = await self._confirm_owned_state(
            journal,
            prepared,
            adapter,
            observed,
            unknown_result=False,
        )
        applied = self._transition(
            journal.id,
            OperationStatus.INTENT_RECORDED,
            OperationStatus.APPLIED,
            after_snapshot=_state_snapshot(state, prepared.ownership_tag),
        )
        return _result(applied, state, prepared, replayed=replayed, recovered=False)

    async def _verify_applied(
        self,
        journal: _JournalView,
        prepared: _PreparedAdd,
        adapter: QbittorrentWriteAdapter,
    ) -> QbittorrentTorrentState:
        observed = await self._get_states(adapter, prepared.expected_hashes)
        expected_hash = _required_text(journal.after_snapshot, "torrent_hash")
        expected_save_path = _required_text(journal.after_snapshot, "save_path")
        matching = tuple(state for state in observed if state.torrent_hash == expected_hash)
        if (
            len(observed) != 1
            or len(matching) != 1
            or matching[0].save_path != expected_save_path
            or prepared.ownership_tag not in matching[0].tags
            or not matching[0].stopped
        ):
            self._transition_if_current(
                journal.id,
                OperationStatus.APPLIED,
                OperationStatus.RECONCILE_REQUIRED,
            )
            raise _state_mismatch("已登记 qBittorrent 任务的身份、保存路径、标签或停止状态发生变化")
        return matching[0]

    async def _confirm_owned_state(
        self,
        journal: _JournalView,
        prepared: _PreparedAdd,
        adapter: QbittorrentWriteAdapter,
        observed: tuple[QbittorrentTorrentState, ...],
        *,
        unknown_result: bool,
    ) -> QbittorrentTorrentState:
        owned = tuple(
            state
            for state in observed
            if state.torrent_hash in prepared.expected_hashes
            and prepared.ownership_tag in state.tags
        )
        if len(observed) != 1 or len(owned) != 1 or owned[0].save_path != prepared.remote_save_path:
            self._mark_reconcile(journal.id)
            raise _state_mismatch(
                "qBittorrent 中出现相同 torrent，但无法用 ownership tag 与保存路径证明归属"
                if unknown_result
                else "qBittorrent 添加后的 torrent 身份、ownership tag 或保存路径不匹配"
            )
        state = owned[0]
        if state.stopped:
            return state

        try:
            await adapter.stop_torrent(state.torrent_hash)
            refreshed = await self._get_states(adapter, (state.torrent_hash,))
        except DownloaderAdapterError as exc:
            self._mark_reconcile(journal.id)
            raise _adapter_application_error(exc, "qBittorrent 未保持停止状态") from exc
        stopped = tuple(
            item
            for item in refreshed
            if item.torrent_hash == state.torrent_hash
            and prepared.ownership_tag in item.tags
            and item.save_path == prepared.remote_save_path
            and item.stopped
        )
        if len(stopped) != 1:
            self._mark_reconcile(journal.id)
            raise _state_mismatch("qBittorrent 未按暂停添加约束保持停止状态")
        return stopped[0]

    async def _get_states(
        self,
        adapter: QbittorrentWriteAdapter,
        expected_hashes: tuple[str, ...],
    ) -> tuple[QbittorrentTorrentState, ...]:
        try:
            return await adapter.get_torrents(expected_hashes)
        except DownloaderAdapterError as exc:
            raise _adapter_application_error(exc, "无法确认 qBittorrent torrent 状态") from exc

    def _prepare(self, request: QbittorrentAddOperationRequest) -> _PreparedAdd:
        if request.downloader_version < 1:
            raise ValueError("downloader version 必须大于等于 1")
        if (
            request.skip_checking
            and request.verification_level is not VerificationLevel.FULL_VERIFIED
        ):
            raise ApplicationError(
                code="DOWNLOADER_SKIP_CHECKING_BLOCKED",
                status=409,
                title="qBittorrent 跳过校验被阻断",
                detail="只有 FULL_VERIFIED execution plan 才允许 skip_checking",
            )
        try:
            meta = parse_torrent(request.torrent_content)
            remote_save_path = normalize_remote_path(request.remote_save_path)
        except (DomainViolation, ValueError) as exc:
            raise ApplicationError(
                code="DOWNLOADER_ADD_INPUT_INVALID",
                status=422,
                title="qBittorrent 添加输入无效",
                detail="torrent payload 或目标保存路径无法通过安全校验",
            ) from exc
        if meta.metainfo_digest != request.expected_metainfo_digest:
            raise ApplicationError(
                code="DOWNLOADER_TORRENT_CHANGED",
                status=409,
                title="待添加 torrent 已变化",
                detail="torrent metainfo digest 与 execution plan 绑定值不一致",
            )
        expected_hashes = tuple(
            item.lower() for item in (meta.v1_info_hash, meta.v2_info_hash) if item is not None
        )
        operation_key = downloader_operation_key(
            candidate_key=request.candidate_key,
            operation_type=QBITTORRENT_ADD_OPERATION,
            downloader_id=request.downloader_id,
        )
        return _PreparedAdd(
            operation_key=operation_key,
            ownership_tag=f"packbreaker-{operation_key[:16]}",
            remote_save_path=remote_save_path,
            expected_hashes=expected_hashes,
            torrent_payload_digest=sha256(request.torrent_content).hexdigest(),
            request=request,
        )

    def _record_intent(self, prepared: _PreparedAdd) -> _JournalView:
        request = prepared.request
        intent = OperationIntent(
            task_id=request.task_id,
            idempotency_key=prepared.operation_key,
            operation_type=QBITTORRENT_ADD_OPERATION,
            target={
                "downloader_id": request.downloader_id,
                "remote_save_path": prepared.remote_save_path,
            },
            intent=_intent_payload(prepared),
            before_snapshot={
                "torrent_absent": True,
                "checked_hashes": list(prepared.expected_hashes),
            },
        )
        with self._session_factory() as session:
            journal, _ = OperationJournalRepository(session).record_intent(intent)
            session.commit()
            return _journal_view(journal)

    def _assert_same_intent(self, journal: _JournalView, prepared: _PreparedAdd) -> None:
        if (
            journal.task_id != prepared.request.task_id
            or journal.operation_type != QBITTORRENT_ADD_OPERATION
            or journal.target
            != {
                "downloader_id": prepared.request.downloader_id,
                "remote_save_path": prepared.remote_save_path,
            }
            or journal.intent != _intent_payload(prepared)
            or journal.before_snapshot
            != {
                "torrent_absent": True,
                "checked_hashes": list(prepared.expected_hashes),
            }
        ):
            raise ApplicationError(
                code="DOWNLOADER_ADD_IDEMPOTENCY_CONFLICT",
                status=409,
                title="qBittorrent 添加幂等键冲突",
                detail="同一 candidate/downloader 对应了不同的添加意图",
            )

    def _load_by_key(self, key: str) -> _JournalView | None:
        with self._session_factory() as session:
            journal = OperationJournalRepository(session).get_by_idempotency_key(key)
            return None if journal is None else _journal_view(journal)

    def _transition(
        self,
        journal_id: str,
        expected_status: OperationStatus,
        to_status: OperationStatus,
        *,
        after_snapshot: dict[str, Any] | None = None,
    ) -> _JournalView:
        with self._session_factory() as session:
            journal = OperationJournalRepository(session).transition_status(
                journal_id=journal_id,
                expected_status=expected_status,
                to_status=to_status,
                after_snapshot=after_snapshot,
            )
            session.commit()
            return _journal_view(journal)

    def _transition_if_current(
        self,
        journal_id: str,
        expected_status: OperationStatus,
        to_status: OperationStatus,
    ) -> None:
        with self._session_factory() as session:
            repository = OperationJournalRepository(session)
            journal = repository.get(journal_id)
            if journal is None or OperationStatus(journal.status) is not expected_status:
                return
            repository.transition_status(
                journal_id=journal_id,
                expected_status=expected_status,
                to_status=to_status,
            )
            session.commit()

    def _mark_reconcile(self, journal_id: str) -> None:
        self._transition_if_current(
            journal_id,
            OperationStatus.INTENT_RECORDED,
            OperationStatus.RECONCILE_REQUIRED,
        )


class QbittorrentRecheckOperationService:
    """以独立 journal 包围强制 recheck；未知结果绝不盲目重复发起昂贵校验。"""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    async def execute(
        self,
        request: QbittorrentRecheckOperationRequest,
        binding: QbittorrentWriteBindingPort,
    ) -> QbittorrentRecheckOperationResult:
        if (
            binding.downloader_id != request.downloader_id
            or binding.downloader_version != request.downloader_version
        ):
            raise ApplicationError(
                code="DOWNLOADER_CONFIG_CHANGED",
                status=409,
                title="qBittorrent 配置已经变化",
                detail="recheck intent 必须绑定同一个下载器 ID 与配置版本",
            )
        if (
            binding.capabilities.get("supports_force_recheck") is not True
            or binding.capabilities.get("supports_verify_progress") is not True
        ):
            raise ApplicationError(
                code="DOWNLOADER_RECHECK_UNSUPPORTED",
                status=409,
                title="qBittorrent 客户端校验能力不可用",
                detail="强制 recheck 与校验进度能力必须同时经过探测确认",
            )

        prepared = _prepare_recheck(request)
        async with _operation_lock(prepared.operation_key):
            return await self._execute_prepared(prepared, binding)

    async def _execute_prepared(
        self,
        prepared: _PreparedRecheck,
        binding: QbittorrentWriteBindingPort,
    ) -> QbittorrentRecheckOperationResult:
        adapter = binding.adapter
        existing = self._load_by_key(prepared.operation_key)
        if existing is not None:
            _assert_same_recheck_intent(existing, prepared)
            if existing.status is OperationStatus.APPLIED:
                state = await self._owned_state(adapter, prepared, reconcile_journal_id=existing.id)
                return _recheck_result(
                    existing,
                    state,
                    prepared,
                    replayed=True,
                    recovered=False,
                )
            if existing.status is not OperationStatus.INTENT_RECORDED:
                raise _journal_not_executable(existing)

            state = await self._owned_state(adapter, prepared, reconcile_journal_id=existing.id)
            if _recheck_unknown_result_proves_applied(existing, state):
                applied = self._transition(
                    existing.id,
                    OperationStatus.INTENT_RECORDED,
                    OperationStatus.APPLIED,
                    after_snapshot=_state_snapshot(state, prepared.ownership_tag),
                )
                return _recheck_result(
                    applied,
                    state,
                    prepared,
                    replayed=True,
                    recovered=True,
                )
            self._mark_reconcile(existing.id)
            raise ApplicationError(
                code="DOWNLOADER_RECHECK_RESULT_UNKNOWN",
                status=409,
                title="qBittorrent recheck 结果无法判定",
                detail="recheck 响应丢失且当前状态不能证明命令已执行，禁止自动重复发起校验",
            )

        before = await self._owned_state(adapter, prepared, reconcile_journal_id=None)
        if not before.stopped:
            raise ApplicationError(
                code="DOWNLOADER_RECHECK_STATE_INVALID",
                status=409,
                title="qBittorrent 状态不允许启动 recheck",
                detail="新的强制校验只能从 PackBreaker 所有且处于停止状态的 torrent 发起",
            )
        journal = self._record_intent(prepared, before)
        try:
            await adapter.recheck_torrent(prepared.torrent_hash)
        except DownloaderAdapterError as exc:
            raise _adapter_application_error(exc, "qBittorrent recheck 结果未知") from exc

        after = await self._owned_state(adapter, prepared, reconcile_journal_id=journal.id)
        applied = self._transition(
            journal.id,
            OperationStatus.INTENT_RECORDED,
            OperationStatus.APPLIED,
            after_snapshot=_state_snapshot(after, prepared.ownership_tag),
        )
        return _recheck_result(applied, after, prepared, replayed=False, recovered=False)

    async def _owned_state(
        self,
        adapter: QbittorrentWriteAdapter,
        prepared: _PreparedRecheck,
        *,
        reconcile_journal_id: str | None,
    ) -> QbittorrentTorrentState:
        try:
            observed = await adapter.get_torrents((prepared.torrent_hash,))
        except DownloaderAdapterError as exc:
            raise _adapter_application_error(exc, "无法确认 qBittorrent recheck 状态") from exc
        matching = tuple(
            state
            for state in observed
            if state.torrent_hash == prepared.torrent_hash
            and state.save_path == prepared.remote_save_path
            and prepared.ownership_tag in state.tags
        )
        if len(observed) != 1 or len(matching) != 1:
            if reconcile_journal_id is not None:
                self._mark_reconcile(reconcile_journal_id)
            raise _state_mismatch(
                "recheck torrent 的 hash、save path、ownership tag 或存在性不匹配"
            )
        return matching[0]

    def _record_intent(
        self,
        prepared: _PreparedRecheck,
        before: QbittorrentTorrentState,
    ) -> _JournalView:
        request = prepared.request
        intent = OperationIntent(
            task_id=request.task_id,
            idempotency_key=prepared.operation_key,
            operation_type=QBITTORRENT_RECHECK_OPERATION,
            target={
                "downloader_id": request.downloader_id,
                "torrent_hash": prepared.torrent_hash,
            },
            intent=_recheck_intent_payload(prepared),
            before_snapshot=_state_snapshot(before, prepared.ownership_tag),
        )
        with self._session_factory() as session:
            journal, _ = OperationJournalRepository(session).record_intent(intent)
            session.commit()
            return _journal_view(journal)

    def _load_by_key(self, key: str) -> _JournalView | None:
        with self._session_factory() as session:
            journal = OperationJournalRepository(session).get_by_idempotency_key(key)
            return None if journal is None else _journal_view(journal)

    def _transition(
        self,
        journal_id: str,
        expected_status: OperationStatus,
        to_status: OperationStatus,
        *,
        after_snapshot: dict[str, Any] | None = None,
    ) -> _JournalView:
        with self._session_factory() as session:
            journal = OperationJournalRepository(session).transition_status(
                journal_id=journal_id,
                expected_status=expected_status,
                to_status=to_status,
                after_snapshot=after_snapshot,
            )
            session.commit()
            return _journal_view(journal)

    def _mark_reconcile(self, journal_id: str) -> None:
        with self._session_factory() as session:
            repository = OperationJournalRepository(session)
            journal = repository.get(journal_id)
            if journal is None:
                return
            current = OperationStatus(journal.status)
            if current not in {OperationStatus.INTENT_RECORDED, OperationStatus.APPLIED}:
                return
            repository.transition_status(
                journal_id=journal_id,
                expected_status=current,
                to_status=OperationStatus.RECONCILE_REQUIRED,
            )
            session.commit()


class QbittorrentStartOperationService:
    """以 journal 包围 qB start；只有实际进入完整上行状态才确认 APPLIED。"""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    async def execute(
        self,
        request: QbittorrentStartOperationRequest,
        binding: QbittorrentWriteBindingPort,
    ) -> QbittorrentStartOperationResult:
        if (
            binding.downloader_id != request.downloader_id
            or binding.downloader_version != request.downloader_version
        ):
            raise ApplicationError(
                code="DOWNLOADER_CONFIG_CHANGED",
                status=409,
                title="qBittorrent 配置已经变化",
                detail="start intent 必须绑定同一个下载器 ID 与配置版本",
            )
        prepared = _prepare_start(request)
        async with _operation_lock(prepared.operation_key):
            return await self._execute_prepared(prepared, binding)

    async def _execute_prepared(
        self,
        prepared: _PreparedStart,
        binding: QbittorrentWriteBindingPort,
    ) -> QbittorrentStartOperationResult:
        adapter = binding.adapter
        existing = self._load_by_key(prepared.operation_key)
        replayed = False
        recovered = False
        if existing is not None:
            _assert_same_start_intent(existing, prepared)
            if existing.status is OperationStatus.APPLIED:
                state = await self._owned_state(adapter, prepared, reconcile_journal_id=existing.id)
                if not state.seeding:
                    self._mark_reconcile(existing.id)
                    raise _state_mismatch("已确认启动的 qBittorrent torrent 已离开完整上行状态")
                return _start_result(
                    existing,
                    state,
                    prepared,
                    replayed=True,
                    recovered=False,
                )
            if existing.status is not OperationStatus.INTENT_RECORDED:
                raise _journal_not_executable(existing)

            state = await self._owned_state(adapter, prepared, reconcile_journal_id=existing.id)
            if state.seeding:
                applied = self._transition(
                    existing.id,
                    OperationStatus.INTENT_RECORDED,
                    OperationStatus.APPLIED,
                    after_snapshot=_state_snapshot(state, prepared.ownership_tag),
                )
                return _start_result(
                    applied,
                    state,
                    prepared,
                    replayed=True,
                    recovered=True,
                )
            if not state.verification_complete:
                self._mark_reconcile(existing.id)
                raise _state_mismatch(
                    "start intent 存在时 torrent 已不再处于停止且完整的可启动状态"
                )
            journal = existing
            replayed = True
            recovered = True
        else:
            before = await self._owned_state(adapter, prepared, reconcile_journal_id=None)
            if before.seeding:
                raise ApplicationError(
                    code="DOWNLOADER_START_PREEXISTING",
                    status=409,
                    title="qBittorrent torrent 已在做种",
                    detail="start intent 前 torrent 已被外部启动，PackBreaker 不会认领",
                )
            if not before.verification_complete:
                raise ApplicationError(
                    code="DOWNLOADER_START_STATE_INVALID",
                    status=409,
                    title="qBittorrent 状态不允许开始做种",
                    detail="只有停止且 progress=1 的 PackBreaker torrent 才允许执行 start",
                )
            journal = self._record_intent(prepared, before)

        try:
            await adapter.start_torrent(prepared.torrent_hash)
        except DownloaderAdapterError as exc:
            raise _adapter_application_error(exc, "qBittorrent start 结果未知") from exc

        after = await self._owned_state(adapter, prepared, reconcile_journal_id=journal.id)
        if not after.seeding:
            if after.verification_complete:
                raise ApplicationError(
                    code="DOWNLOADER_START_NOT_CONFIRMED",
                    status=409,
                    title="qBittorrent start 尚未确认",
                    detail=(
                        "start 已发送但 torrent 仍处于停止且完整状态；"
                        "后续 tick 会先查询真实状态再安全重试"
                    ),
                )
            self._mark_reconcile(journal.id)
            raise _state_mismatch("start 后 torrent 未进入可解释的完整上行状态")

        applied = self._transition(
            journal.id,
            OperationStatus.INTENT_RECORDED,
            OperationStatus.APPLIED,
            after_snapshot=_state_snapshot(after, prepared.ownership_tag),
        )
        return _start_result(
            applied,
            after,
            prepared,
            replayed=replayed,
            recovered=recovered,
        )

    async def _owned_state(
        self,
        adapter: QbittorrentWriteAdapter,
        prepared: _PreparedStart,
        *,
        reconcile_journal_id: str | None,
    ) -> QbittorrentTorrentState:
        try:
            observed = await adapter.get_torrents((prepared.torrent_hash,))
        except DownloaderAdapterError as exc:
            raise _adapter_application_error(exc, "无法确认 qBittorrent start 状态") from exc
        matching = tuple(
            state
            for state in observed
            if state.torrent_hash == prepared.torrent_hash
            and state.save_path == prepared.remote_save_path
            and prepared.ownership_tag in state.tags
        )
        if len(observed) != 1 or len(matching) != 1:
            if reconcile_journal_id is not None:
                self._mark_reconcile(reconcile_journal_id)
            raise _state_mismatch("start torrent 的 hash、save path、ownership tag 或存在性不匹配")
        return matching[0]

    def _record_intent(
        self,
        prepared: _PreparedStart,
        before: QbittorrentTorrentState,
    ) -> _JournalView:
        request = prepared.request
        intent = OperationIntent(
            task_id=request.task_id,
            idempotency_key=prepared.operation_key,
            operation_type=QBITTORRENT_START_OPERATION,
            target={
                "downloader_id": request.downloader_id,
                "torrent_hash": prepared.torrent_hash,
            },
            intent=_start_intent_payload(prepared),
            before_snapshot=_state_snapshot(before, prepared.ownership_tag),
        )
        with self._session_factory() as session:
            journal, _ = OperationJournalRepository(session).record_intent(intent)
            session.commit()
            return _journal_view(journal)

    def _load_by_key(self, key: str) -> _JournalView | None:
        with self._session_factory() as session:
            journal = OperationJournalRepository(session).get_by_idempotency_key(key)
            return None if journal is None else _journal_view(journal)

    def _transition(
        self,
        journal_id: str,
        expected_status: OperationStatus,
        to_status: OperationStatus,
        *,
        after_snapshot: dict[str, Any] | None = None,
    ) -> _JournalView:
        with self._session_factory() as session:
            journal = OperationJournalRepository(session).transition_status(
                journal_id=journal_id,
                expected_status=expected_status,
                to_status=to_status,
                after_snapshot=after_snapshot,
            )
            session.commit()
            return _journal_view(journal)

    def _mark_reconcile(self, journal_id: str) -> None:
        with self._session_factory() as session:
            repository = OperationJournalRepository(session)
            journal = repository.get(journal_id)
            if journal is None:
                return
            current = OperationStatus(journal.status)
            if current not in {OperationStatus.INTENT_RECORDED, OperationStatus.APPLIED}:
                return
            repository.transition_status(
                journal_id=journal_id,
                expected_status=current,
                to_status=OperationStatus.RECONCILE_REQUIRED,
            )
            session.commit()


class QbittorrentRemoveOperationService:
    """以 journal 包围 qB 任务移除；永远保留数据文件，并先验证 PackBreaker 所有权。"""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    async def execute(
        self,
        request: QbittorrentRemoveOperationRequest,
        binding: QbittorrentWriteBindingPort,
    ) -> QbittorrentRemoveOperationResult:
        if (
            binding.downloader_id != request.downloader_id
            or binding.downloader_version != request.downloader_version
        ):
            raise ApplicationError(
                code="DOWNLOADER_CONFIG_CHANGED",
                status=409,
                title="qBittorrent 配置已经变化",
                detail="remove intent 必须绑定同一个下载器 ID 与配置版本",
            )
        prepared = _prepare_remove(request)
        self._assert_add_journal(prepared)
        async with _operation_lock(prepared.operation_key):
            return await self._execute_prepared(prepared, binding)

    async def _execute_prepared(
        self,
        prepared: _PreparedRemove,
        binding: QbittorrentWriteBindingPort,
    ) -> QbittorrentRemoveOperationResult:
        adapter = binding.adapter
        existing = self._load_by_key(prepared.operation_key)
        if existing is not None:
            _assert_same_remove_intent(existing, prepared)
            if existing.status is OperationStatus.NOOP:
                return _remove_result(existing, prepared, replayed=True, recovered=False)
            if existing.status is OperationStatus.APPLIED:
                observed = await self._get_state(
                    adapter, prepared, reconcile_journal_id=existing.id
                )
                if observed is not None:
                    self._mark_reconcile(existing.id)
                    raise _state_mismatch("已确认移除的 qBittorrent torrent 再次出现")
                return _remove_result(existing, prepared, replayed=True, recovered=False)
            if existing.status is not OperationStatus.INTENT_RECORDED:
                raise _journal_not_executable(existing)

            observed = await self._get_state(adapter, prepared, reconcile_journal_id=existing.id)
            if observed is None:
                applied = self._transition(
                    existing.id,
                    OperationStatus.INTENT_RECORDED,
                    OperationStatus.APPLIED,
                    after_snapshot=_removed_snapshot(prepared),
                )
                return _remove_result(applied, prepared, replayed=True, recovered=True)
            journal = existing
            replayed = True
        else:
            observed = await self._get_state(adapter, prepared, reconcile_journal_id=None)
            if observed is None:
                journal = self._record_intent(prepared, before=None)
                noop = self._transition(
                    journal.id,
                    OperationStatus.INTENT_RECORDED,
                    OperationStatus.NOOP,
                )
                return _remove_result(noop, prepared, replayed=False, recovered=False)
            journal = self._record_intent(prepared, before=observed)
            replayed = False

        if not observed.stopped:
            try:
                await adapter.stop_torrent(prepared.torrent_hash)
            except DownloaderAdapterError as exc:
                raise _adapter_application_error(exc, "qBittorrent stop 结果未知") from exc
            observed = await self._get_state(adapter, prepared, reconcile_journal_id=journal.id)
            if observed is None:
                applied = self._transition(
                    journal.id,
                    OperationStatus.INTENT_RECORDED,
                    OperationStatus.APPLIED,
                    after_snapshot=_removed_snapshot(prepared),
                )
                return _remove_result(applied, prepared, replayed=replayed, recovered=True)
            if not observed.stopped:
                raise ApplicationError(
                    code="DOWNLOADER_STOP_NOT_CONFIRMED",
                    status=409,
                    title="qBittorrent stop 尚未确认",
                    detail="移除任务前 torrent 仍未进入停止状态；后续重试会先查询真实状态",
                )

        try:
            await adapter.remove_torrent_keep_files(prepared.torrent_hash)
        except DownloaderAdapterError as exc:
            after_error = await self._get_state(
                adapter,
                prepared,
                reconcile_journal_id=journal.id,
            )
            if after_error is None:
                applied = self._transition(
                    journal.id,
                    OperationStatus.INTENT_RECORDED,
                    OperationStatus.APPLIED,
                    after_snapshot=_removed_snapshot(prepared),
                )
                return _remove_result(applied, prepared, replayed=replayed, recovered=True)
            raise _adapter_application_error(exc, "qBittorrent remove 结果未知") from exc

        after = await self._get_state(adapter, prepared, reconcile_journal_id=journal.id)
        if after is not None:
            raise ApplicationError(
                code="DOWNLOADER_REMOVE_NOT_CONFIRMED",
                status=409,
                title="qBittorrent remove 尚未确认",
                detail="deleteFiles=false 请求后 torrent 仍可见；后续重试会先查询真实状态",
            )
        applied = self._transition(
            journal.id,
            OperationStatus.INTENT_RECORDED,
            OperationStatus.APPLIED,
            after_snapshot=_removed_snapshot(prepared),
        )
        return _remove_result(applied, prepared, replayed=replayed, recovered=False)

    async def _get_state(
        self,
        adapter: QbittorrentWriteAdapter,
        prepared: _PreparedRemove,
        *,
        reconcile_journal_id: str | None,
    ) -> QbittorrentTorrentState | None:
        try:
            observed = await adapter.get_torrents((prepared.torrent_hash,))
        except DownloaderAdapterError as exc:
            raise _adapter_application_error(exc, "无法确认 qBittorrent remove 状态") from exc
        if not observed:
            return None
        matching = tuple(
            state
            for state in observed
            if state.torrent_hash == prepared.torrent_hash
            and state.save_path == prepared.remote_save_path
            and prepared.ownership_tag in state.tags
        )
        if len(observed) != 1 or len(matching) != 1:
            if reconcile_journal_id is not None:
                self._mark_reconcile(reconcile_journal_id)
            raise _state_mismatch("remove torrent 的 hash、save path、ownership tag 或存在性不匹配")
        return matching[0]

    def _assert_add_journal(self, prepared: _PreparedRemove) -> None:
        with self._session_factory() as session:
            journal = OperationJournalRepository(session).get(prepared.request.qbit_add_journal_id)
            if (
                journal is None
                or journal.task_id != prepared.request.task_id
                or journal.operation_type != QBITTORRENT_ADD_OPERATION
                or OperationStatus(journal.status) is not OperationStatus.APPLIED
                or journal.intent.get("execution_plan_id") != prepared.request.execution_plan_id
                or journal.target.get("downloader_id") != prepared.request.downloader_id
                or journal.after_snapshot is None
                or journal.after_snapshot.get("torrent_hash") != prepared.torrent_hash
                or journal.after_snapshot.get("save_path") != prepared.remote_save_path
                or journal.after_snapshot.get("ownership_tag") != prepared.ownership_tag
            ):
                raise ApplicationError(
                    code="DOWNLOADER_REMOVE_OWNERSHIP_INVALID",
                    status=409,
                    title="qBittorrent 移除所有权证据无效",
                    detail="只有 APPLIED 的 PackBreaker add journal 才能授权 qB remove",
                )

    def _record_intent(
        self,
        prepared: _PreparedRemove,
        *,
        before: QbittorrentTorrentState | None,
    ) -> _JournalView:
        request = prepared.request
        intent = OperationIntent(
            task_id=request.task_id,
            idempotency_key=prepared.operation_key,
            operation_type=QBITTORRENT_REMOVE_OPERATION,
            target={
                "downloader_id": request.downloader_id,
                "torrent_hash": prepared.torrent_hash,
            },
            intent=_remove_intent_payload(prepared),
            before_snapshot=(
                {"torrent_absent": True}
                if before is None
                else _state_snapshot(before, prepared.ownership_tag)
            ),
        )
        with self._session_factory() as session:
            journal, _ = OperationJournalRepository(session).record_intent(intent)
            session.commit()
            return _journal_view(journal)

    def _load_by_key(self, key: str) -> _JournalView | None:
        with self._session_factory() as session:
            journal = OperationJournalRepository(session).get_by_idempotency_key(key)
            return None if journal is None else _journal_view(journal)

    def _transition(
        self,
        journal_id: str,
        expected_status: OperationStatus,
        to_status: OperationStatus,
        *,
        after_snapshot: dict[str, Any] | None = None,
    ) -> _JournalView:
        with self._session_factory() as session:
            journal = OperationJournalRepository(session).transition_status(
                journal_id=journal_id,
                expected_status=expected_status,
                to_status=to_status,
                after_snapshot=after_snapshot,
            )
            session.commit()
            return _journal_view(journal)

    def _mark_reconcile(self, journal_id: str) -> None:
        with self._session_factory() as session:
            repository = OperationJournalRepository(session)
            journal = repository.get(journal_id)
            if journal is None:
                return
            current = OperationStatus(journal.status)
            if current not in {OperationStatus.INTENT_RECORDED, OperationStatus.APPLIED}:
                return
            repository.transition_status(
                journal_id=journal_id,
                expected_status=current,
                to_status=OperationStatus.RECONCILE_REQUIRED,
            )
            session.commit()


class QbittorrentJournalReconcileService:
    """只读查询 qB 真实状态，重新证明已有 after snapshot；绝不重发下载器写命令。"""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    async def reconcile(
        self,
        journal_id: str,
        binding: QbittorrentWriteBindingPort,
        *,
        allow_applied_replay: bool = False,
    ) -> QbittorrentJournalReconcileResult:
        async with _operation_lock(f"journal-reconcile:{journal_id}"):
            journal = self._load(journal_id)
            if journal.operation_type not in QBITTORRENT_RECONCILABLE_OPERATIONS:
                raise ApplicationError(
                    code="OPERATION_RECONCILE_UNSUPPORTED",
                    status=409,
                    title="该 qBittorrent 操作不能自动对账",
                    detail="仅 ADD、RECHECK、START 的已确认完成快照支持只读重新证明",
                )
            if journal.status is OperationStatus.APPLIED:
                if not allow_applied_replay:
                    raise _qbit_reconcile_state_invalid()
                replayed = True
            elif journal.status is OperationStatus.RECONCILE_REQUIRED:
                replayed = False
            else:
                raise _qbit_reconcile_state_invalid()

            prepared = _prepare_journal_reconcile(journal)
            if (
                binding.downloader_id != prepared.downloader_id
                or binding.downloader_version != prepared.downloader_version
            ):
                raise ApplicationError(
                    code="DOWNLOADER_CONFIG_CHANGED",
                    status=409,
                    title="qBittorrent 配置已经变化",
                    detail="对账只能使用 operation journal 原先绑定的下载器配置版本",
                )

            state = await self._owned_state(binding.adapter, prepared)
            if not _qbit_reconcile_postcondition_holds(prepared.operation_type, state):
                raise _qbit_reconcile_blocked()

            if journal.status is OperationStatus.RECONCILE_REQUIRED:
                assert journal.after_snapshot is not None
                journal = self._transition_to_applied(journal, journal.after_snapshot)
            return QbittorrentJournalReconcileResult(
                journal_id=journal.id,
                operation_type=journal.operation_type,
                status=journal.status,
                replayed=replayed,
            )

    async def _owned_state(
        self,
        adapter: QbittorrentWriteAdapter,
        prepared: _PreparedJournalReconcile,
    ) -> QbittorrentTorrentState:
        try:
            observed = await adapter.get_torrents((prepared.torrent_hash,))
        except DownloaderAdapterError as exc:
            raise ApplicationError(
                code="OPERATION_RECONCILE_DOWNLOADER_UNAVAILABLE",
                status=502,
                title="无法查询 qBittorrent 当前状态",
                detail="当前无法取得足够的下载器状态证据，operation journal 保持安全阻断",
            ) from exc
        matching = tuple(
            state
            for state in observed
            if state.torrent_hash == prepared.torrent_hash
            and state.save_path == prepared.remote_save_path
            and prepared.ownership_tag in state.tags
        )
        if len(observed) != 1 or len(matching) != 1:
            raise _qbit_reconcile_blocked()
        return matching[0]

    def _load(self, journal_id: str) -> _JournalView:
        with self._session_factory() as session:
            journal = OperationJournalRepository(session).get(journal_id)
            if journal is None:
                raise ApplicationError(
                    code="OPERATION_NOT_FOUND",
                    status=404,
                    title="operation journal 不存在",
                    detail="无法对不存在的 qBittorrent operation journal 执行对账",
                )
            return _journal_view(journal)

    def _transition_to_applied(
        self,
        journal: _JournalView,
        after_snapshot: dict[str, Any],
    ) -> _JournalView:
        with self._session_factory() as session:
            updated = OperationJournalRepository(session).transition_status(
                journal_id=journal.id,
                expected_status=OperationStatus.RECONCILE_REQUIRED,
                to_status=OperationStatus.APPLIED,
                after_snapshot=deepcopy(after_snapshot),
            )
            session.commit()
            return _journal_view(updated)


@dataclass(frozen=True, slots=True)
class _PreparedAdd:
    operation_key: str
    ownership_tag: str
    remote_save_path: str
    expected_hashes: tuple[str, ...]
    torrent_payload_digest: str
    request: QbittorrentAddOperationRequest


@dataclass(frozen=True, slots=True)
class _PreparedRecheck:
    operation_key: str
    torrent_hash: str
    remote_save_path: str
    ownership_tag: str
    request: QbittorrentRecheckOperationRequest


@dataclass(frozen=True, slots=True)
class _PreparedStart:
    operation_key: str
    torrent_hash: str
    remote_save_path: str
    ownership_tag: str
    request: QbittorrentStartOperationRequest


@dataclass(frozen=True, slots=True)
class _PreparedRemove:
    operation_key: str
    torrent_hash: str
    remote_save_path: str
    ownership_tag: str
    request: QbittorrentRemoveOperationRequest


@dataclass(frozen=True, slots=True)
class _PreparedJournalReconcile:
    operation_type: str
    downloader_id: str
    downloader_version: int
    torrent_hash: str
    remote_save_path: str
    ownership_tag: str


def _prepare_journal_reconcile(journal: _JournalView) -> _PreparedJournalReconcile:
    expected_schema = {
        QBITTORRENT_ADD_OPERATION: QBITTORRENT_OPERATION_SCHEMA_VERSION,
        QBITTORRENT_RECHECK_OPERATION: QBITTORRENT_RECHECK_SCHEMA_VERSION,
        QBITTORRENT_START_OPERATION: QBITTORRENT_START_SCHEMA_VERSION,
    }.get(journal.operation_type)
    if expected_schema is None or journal.after_snapshot is None:
        raise _qbit_reconcile_unprovable()

    downloader_id = journal.target.get("downloader_id")
    downloader_version = journal.intent.get("downloader_version")
    torrent_hash = journal.after_snapshot.get("torrent_hash")
    remote_save_path = journal.intent.get("remote_save_path")
    ownership_tag = journal.intent.get("ownership_tag")
    snapshot_save_path = journal.after_snapshot.get("save_path")
    snapshot_ownership_tag = journal.after_snapshot.get("ownership_tag")
    snapshot_tags = journal.after_snapshot.get("tags")
    if (
        journal.intent.get("schema_version") != expected_schema
        or not isinstance(downloader_id, str)
        or not downloader_id
        or not isinstance(downloader_version, int)
        or isinstance(downloader_version, bool)
        or downloader_version < 1
        or not isinstance(torrent_hash, str)
        or not isinstance(remote_save_path, str)
        or not isinstance(ownership_tag, str)
        or snapshot_save_path != remote_save_path
        or snapshot_ownership_tag != ownership_tag
        or not isinstance(snapshot_tags, list)
        or ownership_tag not in snapshot_tags
    ):
        raise _qbit_reconcile_unprovable()

    normalized_hash = torrent_hash.strip().lower()
    if len(normalized_hash) not in {40, 64} or any(
        character not in "0123456789abcdef" for character in normalized_hash
    ):
        raise _qbit_reconcile_unprovable()
    try:
        normalized_save_path = normalize_remote_path(remote_save_path)
    except DomainViolation as exc:
        raise _qbit_reconcile_unprovable() from exc
    if normalized_save_path != remote_save_path:
        raise _qbit_reconcile_unprovable()

    if journal.operation_type == QBITTORRENT_ADD_OPERATION:
        checked_hashes = (
            journal.before_snapshot.get("checked_hashes") if journal.before_snapshot else None
        )
        if (
            journal.target.get("remote_save_path") != remote_save_path
            or not isinstance(checked_hashes, list)
            or normalized_hash not in checked_hashes
        ):
            raise _qbit_reconcile_unprovable()
    else:
        if (
            journal.target.get("torrent_hash") != normalized_hash
            or journal.intent.get("torrent_hash") != normalized_hash
        ):
            raise _qbit_reconcile_unprovable()

    return _PreparedJournalReconcile(
        operation_type=journal.operation_type,
        downloader_id=downloader_id,
        downloader_version=downloader_version,
        torrent_hash=normalized_hash,
        remote_save_path=normalized_save_path,
        ownership_tag=ownership_tag,
    )


def _qbit_reconcile_postcondition_holds(
    operation_type: str,
    state: QbittorrentTorrentState,
) -> bool:
    if operation_type == QBITTORRENT_ADD_OPERATION:
        return state.stopped
    if operation_type == QBITTORRENT_RECHECK_OPERATION:
        return state.checking or state.verification_complete or state.verification_incomplete
    if operation_type == QBITTORRENT_START_OPERATION:
        return state.seeding
    return False


def _prepare_recheck(request: QbittorrentRecheckOperationRequest) -> _PreparedRecheck:
    if request.downloader_version < 1:
        raise ValueError("downloader version 必须大于等于 1")
    if len(request.candidate_key) != 64 or any(
        character not in "0123456789abcdef" for character in request.candidate_key
    ):
        raise ValueError("candidate_key 必须是 64 位十六进制摘要")
    torrent_hash = request.torrent_hash.strip().lower()
    if len(torrent_hash) not in {40, 64} or any(
        character not in "0123456789abcdef" for character in torrent_hash
    ):
        raise ValueError("torrent_hash 格式无效")
    if not request.qbit_add_journal_id.strip() or not request.execution_plan_id.strip():
        raise ValueError("recheck 必须绑定 add journal 与 execution plan")
    ownership_tag = request.ownership_tag.strip()
    if not ownership_tag or len(ownership_tag) > 128 or "," in ownership_tag:
        raise ValueError("ownership tag 格式无效")
    try:
        remote_save_path = normalize_remote_path(request.remote_save_path)
    except DomainViolation as exc:
        raise ValueError("remote save path 格式无效") from exc
    return _PreparedRecheck(
        operation_key=downloader_operation_key(
            candidate_key=request.candidate_key,
            operation_type=QBITTORRENT_RECHECK_OPERATION,
            downloader_id=request.downloader_id,
        ),
        torrent_hash=torrent_hash,
        remote_save_path=remote_save_path,
        ownership_tag=ownership_tag,
        request=request,
    )


def _recheck_intent_payload(prepared: _PreparedRecheck) -> dict[str, Any]:
    request = prepared.request
    return {
        "schema_version": QBITTORRENT_RECHECK_SCHEMA_VERSION,
        "downloader_version": request.downloader_version,
        "execution_plan_id": request.execution_plan_id,
        "qbit_add_journal_id": request.qbit_add_journal_id,
        "torrent_hash": prepared.torrent_hash,
        "remote_save_path": prepared.remote_save_path,
        "ownership_tag": prepared.ownership_tag,
    }


def _prepare_start(request: QbittorrentStartOperationRequest) -> _PreparedStart:
    if request.downloader_version < 1:
        raise ValueError("downloader version 必须大于等于 1")
    if len(request.candidate_key) != 64 or any(
        character not in "0123456789abcdef" for character in request.candidate_key
    ):
        raise ValueError("candidate_key 必须是 64 位十六进制摘要")
    torrent_hash = request.torrent_hash.strip().lower()
    if len(torrent_hash) not in {40, 64} or any(
        character not in "0123456789abcdef" for character in torrent_hash
    ):
        raise ValueError("torrent_hash 格式无效")
    if not request.qbit_add_journal_id.strip() or not request.execution_plan_id.strip():
        raise ValueError("start 必须绑定 add journal 与 execution plan")
    verification_journal_id = request.verification_journal_id
    if verification_journal_id is not None:
        verification_journal_id = verification_journal_id.strip()
        if not verification_journal_id:
            raise ValueError("verification journal id 不能为空")
    ownership_tag = request.ownership_tag.strip()
    if not ownership_tag or len(ownership_tag) > 128 or "," in ownership_tag:
        raise ValueError("ownership tag 格式无效")
    try:
        remote_save_path = normalize_remote_path(request.remote_save_path)
    except DomainViolation as exc:
        raise ValueError("remote save path 格式无效") from exc
    normalized_request = QbittorrentStartOperationRequest(
        task_id=request.task_id,
        candidate_key=request.candidate_key,
        downloader_id=request.downloader_id,
        downloader_version=request.downloader_version,
        execution_plan_id=request.execution_plan_id,
        qbit_add_journal_id=request.qbit_add_journal_id,
        verification_journal_id=verification_journal_id,
        torrent_hash=torrent_hash,
        remote_save_path=remote_save_path,
        ownership_tag=ownership_tag,
    )
    return _PreparedStart(
        operation_key=downloader_operation_key(
            candidate_key=request.candidate_key,
            operation_type=QBITTORRENT_START_OPERATION,
            downloader_id=request.downloader_id,
        ),
        torrent_hash=torrent_hash,
        remote_save_path=remote_save_path,
        ownership_tag=ownership_tag,
        request=normalized_request,
    )


def _start_intent_payload(prepared: _PreparedStart) -> dict[str, Any]:
    request = prepared.request
    return {
        "schema_version": QBITTORRENT_START_SCHEMA_VERSION,
        "downloader_version": request.downloader_version,
        "execution_plan_id": request.execution_plan_id,
        "qbit_add_journal_id": request.qbit_add_journal_id,
        "verification_journal_id": request.verification_journal_id,
        "torrent_hash": prepared.torrent_hash,
        "remote_save_path": prepared.remote_save_path,
        "ownership_tag": prepared.ownership_tag,
    }


def _prepare_remove(request: QbittorrentRemoveOperationRequest) -> _PreparedRemove:
    if request.downloader_version < 1:
        raise ValueError("downloader version 必须大于等于 1")
    if len(request.candidate_key) != 64 or any(
        character not in "0123456789abcdef" for character in request.candidate_key
    ):
        raise ValueError("candidate_key 必须是 64 位十六进制摘要")
    torrent_hash = request.torrent_hash.strip().lower()
    if len(torrent_hash) not in {40, 64} or any(
        character not in "0123456789abcdef" for character in torrent_hash
    ):
        raise ValueError("torrent_hash 格式无效")
    if not request.qbit_add_journal_id.strip() or not request.execution_plan_id.strip():
        raise ValueError("remove 必须绑定 add journal 与 execution plan")
    ownership_tag = request.ownership_tag.strip()
    if not ownership_tag or len(ownership_tag) > 128 or "," in ownership_tag:
        raise ValueError("ownership tag 格式无效")
    try:
        remote_save_path = normalize_remote_path(request.remote_save_path)
    except DomainViolation as exc:
        raise ValueError("remote save path 格式无效") from exc
    normalized_request = QbittorrentRemoveOperationRequest(
        task_id=request.task_id,
        candidate_key=request.candidate_key,
        downloader_id=request.downloader_id,
        downloader_version=request.downloader_version,
        execution_plan_id=request.execution_plan_id,
        qbit_add_journal_id=request.qbit_add_journal_id,
        torrent_hash=torrent_hash,
        remote_save_path=remote_save_path,
        ownership_tag=ownership_tag,
    )
    return _PreparedRemove(
        operation_key=downloader_operation_key(
            candidate_key=request.candidate_key,
            operation_type=QBITTORRENT_REMOVE_OPERATION,
            downloader_id=request.downloader_id,
        ),
        torrent_hash=torrent_hash,
        remote_save_path=remote_save_path,
        ownership_tag=ownership_tag,
        request=normalized_request,
    )


def _remove_intent_payload(prepared: _PreparedRemove) -> dict[str, Any]:
    request = prepared.request
    return {
        "schema_version": QBITTORRENT_REMOVE_SCHEMA_VERSION,
        "downloader_version": request.downloader_version,
        "execution_plan_id": request.execution_plan_id,
        "qbit_add_journal_id": request.qbit_add_journal_id,
        "torrent_hash": prepared.torrent_hash,
        "remote_save_path": prepared.remote_save_path,
        "ownership_tag": prepared.ownership_tag,
        "delete_files": False,
    }


def _assert_same_remove_intent(journal: _JournalView, prepared: _PreparedRemove) -> None:
    request = prepared.request
    if (
        journal.task_id != request.task_id
        or journal.operation_type != QBITTORRENT_REMOVE_OPERATION
        or journal.target
        != {
            "downloader_id": request.downloader_id,
            "torrent_hash": prepared.torrent_hash,
        }
        or journal.intent != _remove_intent_payload(prepared)
    ):
        raise ApplicationError(
            code="DOWNLOADER_REMOVE_IDEMPOTENCY_CONFLICT",
            status=409,
            title="qBittorrent remove 幂等键冲突",
            detail="同一 candidate/downloader 对应了不同的 remove 意图",
        )


def _removed_snapshot(prepared: _PreparedRemove) -> dict[str, Any]:
    return {
        "torrent_absent": True,
        "torrent_hash": prepared.torrent_hash,
        "delete_files": False,
    }


def _remove_result(
    journal: _JournalView,
    prepared: _PreparedRemove,
    *,
    replayed: bool,
    recovered: bool,
) -> QbittorrentRemoveOperationResult:
    return QbittorrentRemoveOperationResult(
        journal_id=journal.id,
        torrent_hash=prepared.torrent_hash,
        removed=journal.status is OperationStatus.APPLIED,
        replayed=replayed,
        recovered_after_unknown_result=recovered,
    )


def _assert_same_start_intent(journal: _JournalView, prepared: _PreparedStart) -> None:
    request = prepared.request
    if (
        journal.task_id != request.task_id
        or journal.operation_type != QBITTORRENT_START_OPERATION
        or journal.target
        != {
            "downloader_id": request.downloader_id,
            "torrent_hash": prepared.torrent_hash,
        }
        or journal.intent != _start_intent_payload(prepared)
    ):
        raise ApplicationError(
            code="DOWNLOADER_START_IDEMPOTENCY_CONFLICT",
            status=409,
            title="qBittorrent start 幂等键冲突",
            detail="同一 candidate/downloader 对应了不同的 start 意图",
        )


def _assert_same_recheck_intent(journal: _JournalView, prepared: _PreparedRecheck) -> None:
    request = prepared.request
    if (
        journal.task_id != request.task_id
        or journal.operation_type != QBITTORRENT_RECHECK_OPERATION
        or journal.target
        != {
            "downloader_id": request.downloader_id,
            "torrent_hash": prepared.torrent_hash,
        }
        or journal.intent != _recheck_intent_payload(prepared)
    ):
        raise ApplicationError(
            code="DOWNLOADER_RECHECK_IDEMPOTENCY_CONFLICT",
            status=409,
            title="qBittorrent recheck 幂等键冲突",
            detail="同一 candidate/downloader 对应了不同的 recheck 意图",
        )


def _recheck_unknown_result_proves_applied(
    journal: _JournalView,
    state: QbittorrentTorrentState,
) -> bool:
    if state.checking:
        return True
    before = journal.before_snapshot
    if before is None or not state.verification_complete:
        return False
    return before.get("state") != state.state or before.get("progress") != state.progress


def _recheck_result(
    journal: _JournalView,
    state: QbittorrentTorrentState,
    prepared: _PreparedRecheck,
    *,
    replayed: bool,
    recovered: bool,
) -> QbittorrentRecheckOperationResult:
    checking_observed = state.checking or _snapshot_was_checking(journal.after_snapshot)
    completion_proven = state.verification_complete and (
        checking_observed or _state_differs_from_snapshot(state, journal.before_snapshot)
    )
    return QbittorrentRecheckOperationResult(
        journal_id=journal.id,
        torrent_hash=state.torrent_hash,
        save_path=state.save_path,
        state=state.state,
        progress=state.progress,
        ownership_tag=prepared.ownership_tag,
        checking=state.checking,
        verification_complete=state.verification_complete,
        verification_incomplete=state.verification_incomplete,
        checking_observed=checking_observed,
        completion_proven=completion_proven,
        replayed=replayed,
        recovered_after_unknown_result=recovered,
    )


def _start_result(
    journal: _JournalView,
    state: QbittorrentTorrentState,
    prepared: _PreparedStart,
    *,
    replayed: bool,
    recovered: bool,
) -> QbittorrentStartOperationResult:
    return QbittorrentStartOperationResult(
        journal_id=journal.id,
        torrent_hash=state.torrent_hash,
        save_path=state.save_path,
        state=state.state,
        progress=state.progress,
        ownership_tag=prepared.ownership_tag,
        seeding=state.seeding,
        replayed=replayed,
        recovered_after_unknown_result=recovered,
    )


def _snapshot_was_checking(snapshot: dict[str, Any] | None) -> bool:
    return snapshot is not None and snapshot.get("state") in {"checkingDL", "checkingUP"}


def _state_differs_from_snapshot(
    state: QbittorrentTorrentState,
    snapshot: dict[str, Any] | None,
) -> bool:
    return snapshot is not None and (
        snapshot.get("state") != state.state or snapshot.get("progress") != state.progress
    )


def _intent_payload(prepared: _PreparedAdd) -> dict[str, Any]:
    request = prepared.request
    return {
        "schema_version": QBITTORRENT_OPERATION_SCHEMA_VERSION,
        "downloader_version": request.downloader_version,
        "execution_plan_id": request.execution_plan_id,
        "execution_plan_digest": request.execution_plan_digest,
        "expected_metainfo_digest": request.expected_metainfo_digest,
        "torrent_payload_digest": prepared.torrent_payload_digest,
        "expected_hashes": list(prepared.expected_hashes),
        "remote_save_path": prepared.remote_save_path,
        "verification_level": request.verification_level.value,
        "paused": True,
        "skip_checking": request.skip_checking,
        "category": request.category,
        "tags": list(request.tags),
        "ownership_tag": prepared.ownership_tag,
    }


def _state_snapshot(state: QbittorrentTorrentState, ownership_tag: str) -> dict[str, Any]:
    return {
        "torrent_hash": state.torrent_hash,
        "save_path": state.save_path,
        "content_path": state.content_path,
        "state": state.state,
        "progress": state.progress,
        "ownership_tag": ownership_tag,
        "tags": list(state.tags),
    }


def _journal_view(journal: Any) -> _JournalView:
    return _JournalView(
        id=str(journal.id),
        task_id=str(journal.task_id),
        idempotency_key=str(journal.idempotency_key),
        operation_type=str(journal.operation_type),
        target=deepcopy(journal.target),
        intent=deepcopy(journal.intent),
        status=OperationStatus(journal.status),
        before_snapshot=deepcopy(journal.before_snapshot),
        after_snapshot=deepcopy(journal.after_snapshot),
    )


def _result(
    journal: _JournalView,
    state: QbittorrentTorrentState,
    prepared: _PreparedAdd,
    *,
    replayed: bool,
    recovered: bool,
) -> QbittorrentAddOperationResult:
    return QbittorrentAddOperationResult(
        journal_id=journal.id,
        torrent_hash=state.torrent_hash,
        save_path=state.save_path,
        state=state.state,
        ownership_tag=prepared.ownership_tag,
        skip_checking=prepared.request.skip_checking,
        replayed=replayed,
        recovered_after_unknown_result=recovered,
    )


def _required_text(payload: dict[str, Any] | None, key: str) -> str:
    value = payload.get(key) if payload is not None else None
    if not isinstance(value, str) or not value:
        raise _state_mismatch("qBittorrent operation journal after snapshot 格式无效")
    return value


def _adapter_application_error(exc: DownloaderAdapterError, title: str) -> ApplicationError:
    return ApplicationError(
        code=exc.code,
        status=502,
        title=title,
        detail=str(exc),
    )


def _state_mismatch(detail: str) -> ApplicationError:
    return ApplicationError(
        code="DOWNLOADER_STATE_MISMATCH",
        status=409,
        title="qBittorrent 实际状态与登记证据不一致",
        detail=detail,
    )


def _journal_not_executable(journal: _JournalView) -> ApplicationError:
    return ApplicationError(
        code="DOWNLOADER_OPERATION_RECONCILE_REQUIRED",
        status=409,
        title="qBittorrent operation journal 需要人工对账",
        detail=f"当前 journal 状态为 {journal.status.value}，不能自动重复执行",
    )


def _qbit_reconcile_state_invalid() -> ApplicationError:
    return ApplicationError(
        code="OPERATION_RECONCILE_STATE_INVALID",
        status=409,
        title="operation journal 当前不需要重新验证",
        detail="新的 qBittorrent 对账只接受 RECONCILE_REQUIRED；幂等重放只验证已恢复的 APPLIED",
    )


def _qbit_reconcile_unprovable() -> ApplicationError:
    return ApplicationError(
        code="OPERATION_RECONCILE_UNPROVABLE",
        status=409,
        title="qBittorrent 完成证据不足",
        detail="operation journal 缺少可安全绑定到当前下载器状态的历史完成快照",
    )


def _qbit_reconcile_blocked() -> ApplicationError:
    return ApplicationError(
        code="OPERATION_RECONCILE_BLOCKED",
        status=409,
        title="qBittorrent 当前状态不能确认原操作结果",
        detail=(
            "当前 torrent 的所有权、保存路径或操作后状态与已登记完成证据不一致；"
            "journal 保持安全阻断"
        ),
    )


def _operation_lock(operation_key: str) -> asyncio.Lock:
    """单进程按幂等键串行化外部写动作；崩溃后的跨进程恢复仍由 journal 负责。"""

    lock = _OPERATION_LOCKS.get(operation_key)
    if lock is None:
        lock = asyncio.Lock()
        _OPERATION_LOCKS[operation_key] = lock
    return lock
