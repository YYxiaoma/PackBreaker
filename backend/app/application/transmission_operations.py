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
from backend.app.infrastructure.adapters.downloaders import (
    DownloaderAdapterError,
    TransmissionAddRequest,
    TransmissionTorrentState,
    TransmissionWriteAdapter,
)
from backend.app.infrastructure.persistence.repositories import (
    OperationIntent,
    OperationJournalRepository,
)
from backend.app.infrastructure.torrent_parser import parse_torrent

TRANSMISSION_ADD_OPERATION = "TRANSMISSION_ADD"
TRANSMISSION_ADD_SCHEMA_VERSION = "packbreaker-transmission-add-v1"
TRANSMISSION_VERIFY_OPERATION = "TRANSMISSION_VERIFY"
TRANSMISSION_VERIFY_SCHEMA_VERSION = "packbreaker-transmission-verify-v1"
TRANSMISSION_START_OPERATION = "TRANSMISSION_START"
TRANSMISSION_START_SCHEMA_VERSION = "packbreaker-transmission-start-v1"
TRANSMISSION_REMOVE_OPERATION = "TRANSMISSION_REMOVE"
TRANSMISSION_REMOVE_SCHEMA_VERSION = "packbreaker-transmission-remove-v1"

_OPERATION_LOCKS: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()


@dataclass(frozen=True, slots=True)
class TransmissionAddOperationRequest:
    task_id: str
    candidate_key: str
    downloader_id: str
    downloader_version: int
    execution_plan_id: str
    execution_plan_digest: str
    expected_metainfo_digest: str
    torrent_content: bytes
    remote_save_path: str


@dataclass(frozen=True, slots=True)
class TransmissionAddOperationResult:
    journal_id: str
    torrent_hash: str
    save_path: str
    state: str
    ownership_tag: str
    skip_checking: bool
    replayed: bool
    recovered_after_unknown_result: bool


@dataclass(frozen=True, slots=True)
class TransmissionVerifyOperationRequest:
    task_id: str
    candidate_key: str
    downloader_id: str
    downloader_version: int
    execution_plan_id: str
    add_journal_id: str
    torrent_hash: str
    remote_save_path: str
    ownership_tag: str


@dataclass(frozen=True, slots=True)
class TransmissionVerifyOperationResult:
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
class TransmissionStartOperationRequest:
    task_id: str
    candidate_key: str
    downloader_id: str
    downloader_version: int
    execution_plan_id: str
    add_journal_id: str
    verification_journal_id: str
    torrent_hash: str
    remote_save_path: str
    ownership_tag: str


@dataclass(frozen=True, slots=True)
class TransmissionStartOperationResult:
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
class TransmissionRemoveOperationRequest:
    task_id: str
    candidate_key: str
    downloader_id: str
    downloader_version: int
    execution_plan_id: str
    add_journal_id: str
    torrent_hash: str
    remote_save_path: str
    ownership_tag: str


@dataclass(frozen=True, slots=True)
class TransmissionRemoveOperationResult:
    journal_id: str
    torrent_hash: str
    removed: bool
    replayed: bool
    recovered_after_unknown_result: bool


class TransmissionWriteBindingPort(Protocol):
    @property
    def downloader_id(self) -> str: ...

    @property
    def downloader_version(self) -> int: ...

    @property
    def capabilities(self) -> Mapping[str, Any]: ...

    @property
    def adapter(self) -> TransmissionWriteAdapter: ...


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


@dataclass(frozen=True, slots=True)
class _PreparedAdd:
    request: TransmissionAddOperationRequest
    operation_key: str
    ownership_tag: str
    remote_save_path: str
    expected_hashes: tuple[str, ...]
    torrent_payload_digest: str


@dataclass(frozen=True, slots=True)
class _PreparedVerify:
    request: TransmissionVerifyOperationRequest
    operation_key: str
    torrent_hash: str
    remote_save_path: str
    ownership_tag: str


@dataclass(frozen=True, slots=True)
class _PreparedStart:
    request: TransmissionStartOperationRequest
    operation_key: str
    torrent_hash: str
    remote_save_path: str
    ownership_tag: str


@dataclass(frozen=True, slots=True)
class _PreparedRemove:
    request: TransmissionRemoveOperationRequest
    operation_key: str
    torrent_hash: str
    remote_save_path: str
    ownership_tag: str


class TransmissionAddOperationService:
    """以 operation journal 包围 Transmission 暂停添加，并用 label 证明所有权。"""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    async def execute(
        self,
        request: TransmissionAddOperationRequest,
        binding: TransmissionWriteBindingPort,
    ) -> TransmissionAddOperationResult:
        _assert_binding(request.downloader_id, request.downloader_version, binding)
        prepared = _prepare_add(request)
        async with _operation_lock(prepared.operation_key):
            return await self._execute_prepared(prepared, binding)

    async def _execute_prepared(
        self,
        prepared: _PreparedAdd,
        binding: TransmissionWriteBindingPort,
    ) -> TransmissionAddOperationResult:
        adapter = binding.adapter
        existing = self._load_by_key(prepared.operation_key)
        if existing is not None:
            _assert_same_add_intent(existing, prepared)
            if existing.status is OperationStatus.APPLIED:
                state = await self._verify_applied(existing, prepared, adapter)
                return _add_result(existing, state, prepared, replayed=True, recovered=False)
            if existing.status is not OperationStatus.INTENT_RECORDED:
                raise _journal_not_executable(existing)

            observed = await _get_states(adapter, prepared.expected_hashes)
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
                return _add_result(applied, state, prepared, replayed=True, recovered=True)
            journal = existing
            replayed = True
        else:
            observed = await _get_states(adapter, prepared.expected_hashes)
            if observed:
                raise ApplicationError(
                    code="DOWNLOADER_TORRENT_ALREADY_EXISTS",
                    status=409,
                    title="Transmission 已存在同一 torrent",
                    detail="添加 intent 建立前已发现相同 torrent，PackBreaker 不会认领外部任务",
                )
            journal = self._record_intent(prepared)
            replayed = False

        try:
            add_result = await adapter.add_torrent(
                TransmissionAddRequest(
                    torrent_content=prepared.request.torrent_content,
                    save_path=prepared.remote_save_path,
                    labels=(prepared.ownership_tag,),
                    paused=True,
                )
            )
        except DownloaderAdapterError as exc:
            raise _adapter_application_error(exc, "Transmission 添加结果未知") from exc

        if add_result.duplicate:
            self._mark_reconcile(journal.id)
            raise ApplicationError(
                code="DOWNLOADER_TORRENT_ALREADY_EXISTS",
                status=409,
                title="Transmission 报告 torrent 已存在",
                detail="暂停添加返回 duplicate；当前 intent 不足以证明已有任务归 PackBreaker 所有",
            )
        if add_result.torrent_hash not in prepared.expected_hashes:
            self._mark_reconcile(journal.id)
            raise _state_mismatch(
                "Transmission 返回的 torrent hash 与 execution plan metainfo 不一致"
            )

        observed = await _get_states(adapter, prepared.expected_hashes)
        if not observed:
            raise ApplicationError(
                code="DOWNLOADER_ADD_NOT_CONFIRMED",
                status=502,
                title="Transmission 添加未确认",
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
        return _add_result(applied, state, prepared, replayed=replayed, recovered=False)

    async def _verify_applied(
        self,
        journal: _JournalView,
        prepared: _PreparedAdd,
        adapter: TransmissionWriteAdapter,
    ) -> TransmissionTorrentState:
        observed = await _get_states(adapter, prepared.expected_hashes)
        expected_hash = _required_text(journal.after_snapshot, "torrent_hash")
        expected_save_path = _required_text(journal.after_snapshot, "save_path")
        matching = tuple(state for state in observed if state.torrent_hash == expected_hash)
        if (
            len(observed) != 1
            or len(matching) != 1
            or matching[0].download_dir != expected_save_path
            or prepared.ownership_tag not in matching[0].labels
            or not matching[0].stopped
        ):
            self._transition_if_current(
                journal.id,
                OperationStatus.APPLIED,
                OperationStatus.RECONCILE_REQUIRED,
            )
            raise _state_mismatch(
                "已登记 Transmission 任务的身份、保存路径、ownership label 或停止状态发生变化"
            )
        return matching[0]

    async def _confirm_owned_state(
        self,
        journal: _JournalView,
        prepared: _PreparedAdd,
        adapter: TransmissionWriteAdapter,
        observed: tuple[TransmissionTorrentState, ...],
        *,
        unknown_result: bool,
    ) -> TransmissionTorrentState:
        owned = tuple(
            state
            for state in observed
            if state.torrent_hash in prepared.expected_hashes
            and prepared.ownership_tag in state.labels
        )
        if (
            len(observed) != 1
            or len(owned) != 1
            or owned[0].download_dir != prepared.remote_save_path
        ):
            self._mark_reconcile(journal.id)
            raise _state_mismatch(
                "Transmission 中出现相同 torrent，但无法用 ownership label 与保存路径证明归属"
                if unknown_result
                else "Transmission 添加后的 torrent 身份、ownership label 或保存路径不匹配"
            )
        state = owned[0]
        if state.stopped:
            return state
        try:
            await adapter.stop_torrent(state.torrent_hash)
            refreshed = await _get_states(adapter, (state.torrent_hash,))
        except DownloaderAdapterError as exc:
            self._mark_reconcile(journal.id)
            raise _adapter_application_error(exc, "Transmission 未保持停止状态") from exc
        stopped = tuple(
            item
            for item in refreshed
            if item.torrent_hash == state.torrent_hash
            and item.download_dir == prepared.remote_save_path
            and prepared.ownership_tag in item.labels
            and item.stopped
        )
        if len(stopped) != 1:
            self._mark_reconcile(journal.id)
            raise _state_mismatch("Transmission 未按暂停添加约束保持停止状态")
        return stopped[0]

    def _record_intent(self, prepared: _PreparedAdd) -> _JournalView:
        request = prepared.request
        intent = OperationIntent(
            task_id=request.task_id,
            idempotency_key=prepared.operation_key,
            operation_type=TRANSMISSION_ADD_OPERATION,
            target={
                "downloader_id": request.downloader_id,
                "remote_save_path": prepared.remote_save_path,
            },
            intent=_add_intent_payload(prepared),
            before_snapshot={
                "torrent_absent": True,
                "checked_hashes": list(prepared.expected_hashes),
            },
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


class TransmissionVerifyOperationService:
    """以独立 journal 包围 torrent_verify；未知结果没有校验证据时绝不盲目重复。"""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    async def execute(
        self,
        request: TransmissionVerifyOperationRequest,
        binding: TransmissionWriteBindingPort,
    ) -> TransmissionVerifyOperationResult:
        _assert_binding(request.downloader_id, request.downloader_version, binding)
        if (
            binding.capabilities.get("supports_force_recheck") is not True
            or binding.capabilities.get("supports_verify_progress") is not True
        ):
            raise ApplicationError(
                code="DOWNLOADER_RECHECK_UNSUPPORTED",
                status=409,
                title="Transmission 客户端校验能力不可用",
                detail="torrent_verify 与校验进度能力必须同时经过探测确认",
            )
        prepared = _prepare_verify(request)
        self._assert_add_journal(prepared)
        async with _operation_lock(prepared.operation_key):
            return await self._execute_prepared(prepared, binding)

    async def _execute_prepared(
        self,
        prepared: _PreparedVerify,
        binding: TransmissionWriteBindingPort,
    ) -> TransmissionVerifyOperationResult:
        adapter = binding.adapter
        existing = self._load_by_key(prepared.operation_key)
        if existing is not None:
            _assert_same_verify_intent(existing, prepared)
            if existing.status is OperationStatus.APPLIED:
                state = await self._owned_state(adapter, prepared, reconcile_journal_id=existing.id)
                return _verify_result(
                    existing,
                    state,
                    prepared,
                    replayed=True,
                    recovered=False,
                )
            if existing.status is not OperationStatus.INTENT_RECORDED:
                raise _journal_not_executable(existing)

            state = await self._owned_state(adapter, prepared, reconcile_journal_id=existing.id)
            if _verify_unknown_result_proves_applied(existing, state):
                applied = self._transition(
                    existing.id,
                    OperationStatus.INTENT_RECORDED,
                    OperationStatus.APPLIED,
                    after_snapshot=_state_snapshot(state, prepared.ownership_tag),
                )
                return _verify_result(
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
                title="Transmission verify 结果无法判定",
                detail="verify 响应丢失且当前状态不能证明命令已执行，禁止自动重复发起校验",
            )

        before = await self._owned_state(adapter, prepared, reconcile_journal_id=None)
        if not before.stopped:
            raise ApplicationError(
                code="DOWNLOADER_RECHECK_STATE_INVALID",
                status=409,
                title="Transmission 状态不允许启动 verify",
                detail="新的客户端校验只能从 PackBreaker 所有且处于停止状态的 torrent 发起",
            )
        journal = self._record_intent(prepared, before)
        try:
            await adapter.verify_torrent(prepared.torrent_hash)
        except DownloaderAdapterError as exc:
            raise _adapter_application_error(exc, "Transmission verify 结果未知") from exc

        after = await self._owned_state(adapter, prepared, reconcile_journal_id=journal.id)
        applied = self._transition(
            journal.id,
            OperationStatus.INTENT_RECORDED,
            OperationStatus.APPLIED,
            after_snapshot=_state_snapshot(after, prepared.ownership_tag),
        )
        return _verify_result(applied, after, prepared, replayed=False, recovered=False)

    async def _owned_state(
        self,
        adapter: TransmissionWriteAdapter,
        prepared: _PreparedVerify,
        *,
        reconcile_journal_id: str | None,
    ) -> TransmissionTorrentState:
        try:
            observed = await adapter.get_torrents((prepared.torrent_hash,))
        except DownloaderAdapterError as exc:
            raise _adapter_application_error(exc, "无法确认 Transmission verify 状态") from exc
        matching = tuple(
            state
            for state in observed
            if state.torrent_hash == prepared.torrent_hash
            and state.download_dir == prepared.remote_save_path
            and prepared.ownership_tag in state.labels
        )
        if len(observed) != 1 or len(matching) != 1:
            if reconcile_journal_id is not None:
                self._mark_reconcile(reconcile_journal_id)
            raise _state_mismatch(
                "verify torrent 的 hash、save path、ownership label 或存在性不匹配"
            )
        return matching[0]

    def _assert_add_journal(self, prepared: _PreparedVerify) -> None:
        with self._session_factory() as session:
            journal = OperationJournalRepository(session).get(prepared.request.add_journal_id)
            if (
                journal is None
                or journal.task_id != prepared.request.task_id
                or journal.operation_type != TRANSMISSION_ADD_OPERATION
                or OperationStatus(journal.status) is not OperationStatus.APPLIED
                or journal.intent.get("execution_plan_id") != prepared.request.execution_plan_id
                or journal.target.get("downloader_id") != prepared.request.downloader_id
                or journal.after_snapshot is None
                or journal.after_snapshot.get("torrent_hash") != prepared.torrent_hash
                or journal.after_snapshot.get("save_path") != prepared.remote_save_path
                or journal.after_snapshot.get("ownership_tag") != prepared.ownership_tag
            ):
                raise ApplicationError(
                    code="DOWNLOADER_OWNERSHIP_UNPROVEN",
                    status=409,
                    title="Transmission torrent 所有权证据不足",
                    detail=(
                        "verify 必须绑定同一 execution plan 已确认 APPLIED 的 "
                        "Transmission add journal"
                    ),
                )

    def _record_intent(
        self,
        prepared: _PreparedVerify,
        before: TransmissionTorrentState,
    ) -> _JournalView:
        request = prepared.request
        intent = OperationIntent(
            task_id=request.task_id,
            idempotency_key=prepared.operation_key,
            operation_type=TRANSMISSION_VERIFY_OPERATION,
            target={
                "downloader_id": request.downloader_id,
                "torrent_hash": prepared.torrent_hash,
            },
            intent=_verify_intent_payload(prepared),
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


class TransmissionStartOperationService:
    """以独立 journal 包围 torrent_start；只有已校验且归属明确的任务才能启动。"""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    async def execute(
        self,
        request: TransmissionStartOperationRequest,
        binding: TransmissionWriteBindingPort,
    ) -> TransmissionStartOperationResult:
        _assert_binding(request.downloader_id, request.downloader_version, binding)
        prepared = _prepare_start(request)
        self._assert_authorization_journals(prepared)
        async with _operation_lock(prepared.operation_key):
            return await self._execute_prepared(prepared, binding)

    async def _execute_prepared(
        self,
        prepared: _PreparedStart,
        binding: TransmissionWriteBindingPort,
    ) -> TransmissionStartOperationResult:
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
                    raise _state_mismatch("已确认启动的 Transmission torrent 已离开完整做种状态")
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
                    "start intent 存在时 Transmission torrent 已不再处于停止且完整的可启动状态"
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
                    title="Transmission torrent 已在做种",
                    detail="start intent 建立前 torrent 已被外部启动，PackBreaker 不会认领",
                )
            if not before.verification_complete:
                raise ApplicationError(
                    code="DOWNLOADER_START_STATE_INVALID",
                    status=409,
                    title="Transmission 状态不允许开始做种",
                    detail="只有停止且 percent_done=1 的已校验 PackBreaker torrent 才允许 start",
                )
            journal = self._record_intent(prepared, before)

        try:
            await adapter.start_torrent(prepared.torrent_hash)
        except DownloaderAdapterError as exc:
            raise _adapter_application_error(exc, "Transmission start 结果未知") from exc

        after = await self._owned_state(adapter, prepared, reconcile_journal_id=journal.id)
        if not after.seeding:
            if after.verification_complete:
                raise ApplicationError(
                    code="DOWNLOADER_START_NOT_CONFIRMED",
                    status=409,
                    title="Transmission start 尚未确认",
                    detail=(
                        "torrent_start 已发送但任务仍处于停止且完整状态；"
                        "后续 tick 会先查询真实状态再安全重试"
                    ),
                )
            self._mark_reconcile(journal.id)
            raise _state_mismatch("start 后 Transmission torrent 未进入可解释的完整做种状态")

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
        adapter: TransmissionWriteAdapter,
        prepared: _PreparedStart,
        *,
        reconcile_journal_id: str | None,
    ) -> TransmissionTorrentState:
        try:
            observed = await adapter.get_torrents((prepared.torrent_hash,))
        except DownloaderAdapterError as exc:
            raise _adapter_application_error(exc, "无法确认 Transmission start 状态") from exc
        matching = tuple(
            state
            for state in observed
            if state.torrent_hash == prepared.torrent_hash
            and state.download_dir == prepared.remote_save_path
            and prepared.ownership_tag in state.labels
        )
        if len(observed) != 1 or len(matching) != 1:
            if reconcile_journal_id is not None:
                self._mark_reconcile(reconcile_journal_id)
            raise _state_mismatch(
                "start torrent 的 hash、save path、ownership label 或存在性不匹配"
            )
        return matching[0]

    def _assert_authorization_journals(self, prepared: _PreparedStart) -> None:
        request = prepared.request
        with self._session_factory() as session:
            repository = OperationJournalRepository(session)
            add_journal = repository.get(request.add_journal_id)
            verify_journal = repository.get(request.verification_journal_id)
            add_valid = (
                add_journal is not None
                and add_journal.task_id == request.task_id
                and add_journal.operation_type == TRANSMISSION_ADD_OPERATION
                and OperationStatus(add_journal.status) is OperationStatus.APPLIED
                and add_journal.intent.get("execution_plan_id") == request.execution_plan_id
                and add_journal.target.get("downloader_id") == request.downloader_id
                and add_journal.after_snapshot is not None
                and add_journal.after_snapshot.get("torrent_hash") == prepared.torrent_hash
                and add_journal.after_snapshot.get("save_path") == prepared.remote_save_path
                and add_journal.after_snapshot.get("ownership_tag") == prepared.ownership_tag
            )
            verify_valid = (
                verify_journal is not None
                and verify_journal.task_id == request.task_id
                and verify_journal.operation_type == TRANSMISSION_VERIFY_OPERATION
                and OperationStatus(verify_journal.status) is OperationStatus.APPLIED
                and verify_journal.intent.get("execution_plan_id") == request.execution_plan_id
                and verify_journal.target.get("downloader_id") == request.downloader_id
                and verify_journal.target.get("torrent_hash") == prepared.torrent_hash
                and verify_journal.intent.get("add_journal_id") == request.add_journal_id
                and verify_journal.intent.get("torrent_hash") == prepared.torrent_hash
                and verify_journal.intent.get("remote_save_path") == prepared.remote_save_path
                and verify_journal.intent.get("ownership_tag") == prepared.ownership_tag
            )
            if not add_valid or not verify_valid:
                raise ApplicationError(
                    code="DOWNLOADER_START_AUTHORIZATION_INVALID",
                    status=409,
                    title="Transmission 做种启动证据无效",
                    detail=(
                        "torrent_start 必须绑定同一 execution plan 的 APPLIED add 与 verify journal"
                    ),
                )

    def _record_intent(
        self,
        prepared: _PreparedStart,
        before: TransmissionTorrentState,
    ) -> _JournalView:
        request = prepared.request
        intent = OperationIntent(
            task_id=request.task_id,
            idempotency_key=prepared.operation_key,
            operation_type=TRANSMISSION_START_OPERATION,
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


class TransmissionRemoveOperationService:
    """以独立 journal 包围 torrent_remove；永远保留本地数据，并先验证 PackBreaker 所有权。"""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    async def execute(
        self,
        request: TransmissionRemoveOperationRequest,
        binding: TransmissionWriteBindingPort,
    ) -> TransmissionRemoveOperationResult:
        _assert_binding(request.downloader_id, request.downloader_version, binding)
        prepared = _prepare_remove(request)
        self._assert_add_journal(prepared)
        async with _operation_lock(prepared.operation_key):
            return await self._execute_prepared(prepared, binding)

    async def _execute_prepared(
        self,
        prepared: _PreparedRemove,
        binding: TransmissionWriteBindingPort,
    ) -> TransmissionRemoveOperationResult:
        adapter = binding.adapter
        existing = self._load_by_key(prepared.operation_key)
        if existing is not None:
            _assert_same_remove_intent(existing, prepared)
            if existing.status is OperationStatus.NOOP:
                return _remove_result(existing, prepared, replayed=True, recovered=False)
            if existing.status is OperationStatus.APPLIED:
                observed = await self._get_state(
                    adapter,
                    prepared,
                    reconcile_journal_id=existing.id,
                )
                if observed is not None:
                    self._mark_reconcile(existing.id)
                    raise _state_mismatch("已确认移除的 Transmission torrent 再次出现")
                return _remove_result(existing, prepared, replayed=True, recovered=False)
            if existing.status is not OperationStatus.INTENT_RECORDED:
                raise _journal_not_executable(existing)

            observed = await self._get_state(
                adapter,
                prepared,
                reconcile_journal_id=existing.id,
            )
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
                raise _adapter_application_error(exc, "Transmission stop 结果未知") from exc
            observed = await self._get_state(
                adapter,
                prepared,
                reconcile_journal_id=journal.id,
            )
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
                    title="Transmission stop 尚未确认",
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
            raise _adapter_application_error(exc, "Transmission remove 结果未知") from exc

        after = await self._get_state(adapter, prepared, reconcile_journal_id=journal.id)
        if after is not None:
            raise ApplicationError(
                code="DOWNLOADER_REMOVE_NOT_CONFIRMED",
                status=409,
                title="Transmission remove 尚未确认",
                detail=("delete_local_data=false 请求后 torrent 仍可见；后续重试会先查询真实状态"),
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
        adapter: TransmissionWriteAdapter,
        prepared: _PreparedRemove,
        *,
        reconcile_journal_id: str | None,
    ) -> TransmissionTorrentState | None:
        try:
            observed = await adapter.get_torrents((prepared.torrent_hash,))
        except DownloaderAdapterError as exc:
            raise _adapter_application_error(exc, "无法确认 Transmission remove 状态") from exc
        if not observed:
            return None
        matching = tuple(
            state
            for state in observed
            if state.torrent_hash == prepared.torrent_hash
            and state.download_dir == prepared.remote_save_path
            and prepared.ownership_tag in state.labels
        )
        if len(observed) != 1 or len(matching) != 1:
            if reconcile_journal_id is not None:
                self._mark_reconcile(reconcile_journal_id)
            raise _state_mismatch(
                "remove torrent 的 hash、save path、ownership label 或存在性不匹配"
            )
        return matching[0]

    def _assert_add_journal(self, prepared: _PreparedRemove) -> None:
        with self._session_factory() as session:
            journal = OperationJournalRepository(session).get(prepared.request.add_journal_id)
            if (
                journal is None
                or journal.task_id != prepared.request.task_id
                or journal.operation_type != TRANSMISSION_ADD_OPERATION
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
                    title="Transmission 移除所有权证据无效",
                    detail="只有 APPLIED 的 PackBreaker Transmission add journal 才能授权 remove",
                )

    def _record_intent(
        self,
        prepared: _PreparedRemove,
        *,
        before: TransmissionTorrentState | None,
    ) -> _JournalView:
        request = prepared.request
        intent = OperationIntent(
            task_id=request.task_id,
            idempotency_key=prepared.operation_key,
            operation_type=TRANSMISSION_REMOVE_OPERATION,
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


def _assert_binding(
    downloader_id: str,
    downloader_version: int,
    binding: TransmissionWriteBindingPort,
) -> None:
    if binding.downloader_id != downloader_id or binding.downloader_version != downloader_version:
        raise ApplicationError(
            code="DOWNLOADER_CONFIG_CHANGED",
            status=409,
            title="Transmission 配置已经变化",
            detail="operation intent 必须绑定同一个下载器 ID 与配置版本",
        )


def _prepare_add(request: TransmissionAddOperationRequest) -> _PreparedAdd:
    if request.downloader_version < 1:
        raise ValueError("downloader version 必须大于等于 1")
    _validate_candidate_key(request.candidate_key)
    try:
        meta = parse_torrent(request.torrent_content)
        remote_save_path = normalize_remote_path(request.remote_save_path)
    except (DomainViolation, ValueError) as exc:
        raise ApplicationError(
            code="DOWNLOADER_ADD_INPUT_INVALID",
            status=422,
            title="Transmission 添加输入无效",
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
        operation_type=TRANSMISSION_ADD_OPERATION,
        downloader_id=request.downloader_id,
    )
    return _PreparedAdd(
        request=request,
        operation_key=operation_key,
        ownership_tag=f"packbreaker-{operation_key[:16]}",
        remote_save_path=remote_save_path,
        expected_hashes=expected_hashes,
        torrent_payload_digest=sha256(request.torrent_content).hexdigest(),
    )


def _prepare_verify(request: TransmissionVerifyOperationRequest) -> _PreparedVerify:
    if request.downloader_version < 1:
        raise ValueError("downloader version 必须大于等于 1")
    _validate_candidate_key(request.candidate_key)
    torrent_hash = _normalize_hash(request.torrent_hash)
    if not request.add_journal_id.strip() or not request.execution_plan_id.strip():
        raise ValueError("verify 必须绑定 add journal 与 execution plan")
    ownership_tag = request.ownership_tag.strip()
    if not ownership_tag or len(ownership_tag) > 128 or "\x00" in ownership_tag:
        raise ValueError("ownership label 格式无效")
    remote_save_path = normalize_remote_path(request.remote_save_path)
    normalized_request = TransmissionVerifyOperationRequest(
        task_id=request.task_id,
        candidate_key=request.candidate_key,
        downloader_id=request.downloader_id,
        downloader_version=request.downloader_version,
        execution_plan_id=request.execution_plan_id,
        add_journal_id=request.add_journal_id,
        torrent_hash=torrent_hash,
        remote_save_path=remote_save_path,
        ownership_tag=ownership_tag,
    )
    return _PreparedVerify(
        request=normalized_request,
        operation_key=downloader_operation_key(
            candidate_key=request.candidate_key,
            operation_type=TRANSMISSION_VERIFY_OPERATION,
            downloader_id=request.downloader_id,
        ),
        torrent_hash=torrent_hash,
        remote_save_path=remote_save_path,
        ownership_tag=ownership_tag,
    )


def _prepare_start(request: TransmissionStartOperationRequest) -> _PreparedStart:
    if request.downloader_version < 1:
        raise ValueError("downloader version 必须大于等于 1")
    _validate_candidate_key(request.candidate_key)
    torrent_hash = _normalize_hash(request.torrent_hash)
    add_journal_id = request.add_journal_id.strip()
    verification_journal_id = request.verification_journal_id.strip()
    execution_plan_id = request.execution_plan_id.strip()
    if not add_journal_id or not verification_journal_id or not execution_plan_id:
        raise ValueError("start 必须绑定 add、verify journal 与 execution plan")
    ownership_tag = request.ownership_tag.strip()
    if not ownership_tag or len(ownership_tag) > 128 or "\x00" in ownership_tag:
        raise ValueError("ownership label 格式无效")
    remote_save_path = normalize_remote_path(request.remote_save_path)
    normalized_request = TransmissionStartOperationRequest(
        task_id=request.task_id,
        candidate_key=request.candidate_key,
        downloader_id=request.downloader_id,
        downloader_version=request.downloader_version,
        execution_plan_id=execution_plan_id,
        add_journal_id=add_journal_id,
        verification_journal_id=verification_journal_id,
        torrent_hash=torrent_hash,
        remote_save_path=remote_save_path,
        ownership_tag=ownership_tag,
    )
    return _PreparedStart(
        request=normalized_request,
        operation_key=downloader_operation_key(
            candidate_key=request.candidate_key,
            operation_type=TRANSMISSION_START_OPERATION,
            downloader_id=request.downloader_id,
        ),
        torrent_hash=torrent_hash,
        remote_save_path=remote_save_path,
        ownership_tag=ownership_tag,
    )


def _prepare_remove(request: TransmissionRemoveOperationRequest) -> _PreparedRemove:
    if request.downloader_version < 1:
        raise ValueError("downloader version 必须大于等于 1")
    _validate_candidate_key(request.candidate_key)
    torrent_hash = _normalize_hash(request.torrent_hash)
    add_journal_id = request.add_journal_id.strip()
    execution_plan_id = request.execution_plan_id.strip()
    if not add_journal_id or not execution_plan_id:
        raise ValueError("remove 必须绑定 add journal 与 execution plan")
    ownership_tag = request.ownership_tag.strip()
    if not ownership_tag or len(ownership_tag) > 128 or "\x00" in ownership_tag:
        raise ValueError("ownership label 格式无效")
    remote_save_path = normalize_remote_path(request.remote_save_path)
    normalized_request = TransmissionRemoveOperationRequest(
        task_id=request.task_id,
        candidate_key=request.candidate_key,
        downloader_id=request.downloader_id,
        downloader_version=request.downloader_version,
        execution_plan_id=execution_plan_id,
        add_journal_id=add_journal_id,
        torrent_hash=torrent_hash,
        remote_save_path=remote_save_path,
        ownership_tag=ownership_tag,
    )
    return _PreparedRemove(
        request=normalized_request,
        operation_key=downloader_operation_key(
            candidate_key=request.candidate_key,
            operation_type=TRANSMISSION_REMOVE_OPERATION,
            downloader_id=request.downloader_id,
        ),
        torrent_hash=torrent_hash,
        remote_save_path=remote_save_path,
        ownership_tag=ownership_tag,
    )


def _add_intent_payload(prepared: _PreparedAdd) -> dict[str, Any]:
    request = prepared.request
    return {
        "schema_version": TRANSMISSION_ADD_SCHEMA_VERSION,
        "downloader_version": request.downloader_version,
        "execution_plan_id": request.execution_plan_id,
        "execution_plan_digest": request.execution_plan_digest,
        "expected_metainfo_digest": request.expected_metainfo_digest,
        "torrent_payload_digest": prepared.torrent_payload_digest,
        "expected_hashes": list(prepared.expected_hashes),
        "remote_save_path": prepared.remote_save_path,
        "paused": True,
        "skip_checking": False,
        "ownership_tag": prepared.ownership_tag,
    }


def _verify_intent_payload(prepared: _PreparedVerify) -> dict[str, Any]:
    request = prepared.request
    return {
        "schema_version": TRANSMISSION_VERIFY_SCHEMA_VERSION,
        "downloader_version": request.downloader_version,
        "execution_plan_id": request.execution_plan_id,
        "add_journal_id": request.add_journal_id,
        "torrent_hash": prepared.torrent_hash,
        "remote_save_path": prepared.remote_save_path,
        "ownership_tag": prepared.ownership_tag,
    }


def _start_intent_payload(prepared: _PreparedStart) -> dict[str, Any]:
    request = prepared.request
    return {
        "schema_version": TRANSMISSION_START_SCHEMA_VERSION,
        "downloader_version": request.downloader_version,
        "execution_plan_id": request.execution_plan_id,
        "add_journal_id": request.add_journal_id,
        "verification_journal_id": request.verification_journal_id,
        "torrent_hash": prepared.torrent_hash,
        "remote_save_path": prepared.remote_save_path,
        "ownership_tag": prepared.ownership_tag,
    }


def _remove_intent_payload(prepared: _PreparedRemove) -> dict[str, Any]:
    request = prepared.request
    return {
        "schema_version": TRANSMISSION_REMOVE_SCHEMA_VERSION,
        "downloader_version": request.downloader_version,
        "execution_plan_id": request.execution_plan_id,
        "add_journal_id": request.add_journal_id,
        "torrent_hash": prepared.torrent_hash,
        "remote_save_path": prepared.remote_save_path,
        "ownership_tag": prepared.ownership_tag,
        "delete_local_data": False,
    }


def _assert_same_add_intent(journal: _JournalView, prepared: _PreparedAdd) -> None:
    if (
        journal.task_id != prepared.request.task_id
        or journal.operation_type != TRANSMISSION_ADD_OPERATION
        or journal.target
        != {
            "downloader_id": prepared.request.downloader_id,
            "remote_save_path": prepared.remote_save_path,
        }
        or journal.intent != _add_intent_payload(prepared)
        or journal.before_snapshot
        != {"torrent_absent": True, "checked_hashes": list(prepared.expected_hashes)}
    ):
        raise ApplicationError(
            code="DOWNLOADER_ADD_IDEMPOTENCY_CONFLICT",
            status=409,
            title="Transmission 添加幂等键冲突",
            detail="同一 candidate/downloader 对应了不同的添加意图",
        )


def _assert_same_verify_intent(journal: _JournalView, prepared: _PreparedVerify) -> None:
    if (
        journal.task_id != prepared.request.task_id
        or journal.operation_type != TRANSMISSION_VERIFY_OPERATION
        or journal.target
        != {
            "downloader_id": prepared.request.downloader_id,
            "torrent_hash": prepared.torrent_hash,
        }
        or journal.intent != _verify_intent_payload(prepared)
    ):
        raise ApplicationError(
            code="DOWNLOADER_RECHECK_IDEMPOTENCY_CONFLICT",
            status=409,
            title="Transmission verify 幂等键冲突",
            detail="同一 candidate/downloader 对应了不同的 verify 意图",
        )


def _assert_same_start_intent(journal: _JournalView, prepared: _PreparedStart) -> None:
    if (
        journal.task_id != prepared.request.task_id
        or journal.operation_type != TRANSMISSION_START_OPERATION
        or journal.target
        != {
            "downloader_id": prepared.request.downloader_id,
            "torrent_hash": prepared.torrent_hash,
        }
        or journal.intent != _start_intent_payload(prepared)
    ):
        raise ApplicationError(
            code="DOWNLOADER_START_IDEMPOTENCY_CONFLICT",
            status=409,
            title="Transmission start 幂等键冲突",
            detail="同一 candidate/downloader 对应了不同的 start 意图",
        )


def _assert_same_remove_intent(journal: _JournalView, prepared: _PreparedRemove) -> None:
    if (
        journal.task_id != prepared.request.task_id
        or journal.operation_type != TRANSMISSION_REMOVE_OPERATION
        or journal.target
        != {
            "downloader_id": prepared.request.downloader_id,
            "torrent_hash": prepared.torrent_hash,
        }
        or journal.intent != _remove_intent_payload(prepared)
    ):
        raise ApplicationError(
            code="DOWNLOADER_REMOVE_IDEMPOTENCY_CONFLICT",
            status=409,
            title="Transmission remove 幂等键冲突",
            detail="同一 candidate/downloader 对应了不同的 remove 意图",
        )


async def _get_states(
    adapter: TransmissionWriteAdapter,
    hashes: tuple[str, ...],
) -> tuple[TransmissionTorrentState, ...]:
    try:
        return await adapter.get_torrents(hashes)
    except DownloaderAdapterError as exc:
        raise _adapter_application_error(exc, "无法确认 Transmission torrent 状态") from exc


def _verify_unknown_result_proves_applied(
    journal: _JournalView,
    state: TransmissionTorrentState,
) -> bool:
    if state.checking:
        return True
    before = journal.before_snapshot
    if before is None or not state.verification_complete:
        return False
    return _state_differs_from_snapshot(state, before)


def _verify_result(
    journal: _JournalView,
    state: TransmissionTorrentState,
    prepared: _PreparedVerify,
    *,
    replayed: bool,
    recovered: bool,
) -> TransmissionVerifyOperationResult:
    checking_observed = state.checking or _snapshot_was_checking(journal.after_snapshot)
    completion_proven = state.verification_complete and (
        checking_observed or _state_differs_from_snapshot(state, journal.before_snapshot)
    )
    return TransmissionVerifyOperationResult(
        journal_id=journal.id,
        torrent_hash=state.torrent_hash,
        save_path=state.download_dir,
        state=str(state.status),
        progress=state.percent_done,
        ownership_tag=prepared.ownership_tag,
        checking=state.checking,
        verification_complete=state.verification_complete,
        verification_incomplete=state.verification_incomplete,
        checking_observed=checking_observed,
        completion_proven=completion_proven,
        replayed=replayed,
        recovered_after_unknown_result=recovered,
    )


def _add_result(
    journal: _JournalView,
    state: TransmissionTorrentState,
    prepared: _PreparedAdd,
    *,
    replayed: bool,
    recovered: bool,
) -> TransmissionAddOperationResult:
    return TransmissionAddOperationResult(
        journal_id=journal.id,
        torrent_hash=state.torrent_hash,
        save_path=state.download_dir,
        state=str(state.status),
        ownership_tag=prepared.ownership_tag,
        skip_checking=False,
        replayed=replayed,
        recovered_after_unknown_result=recovered,
    )


def _start_result(
    journal: _JournalView,
    state: TransmissionTorrentState,
    prepared: _PreparedStart,
    *,
    replayed: bool,
    recovered: bool,
) -> TransmissionStartOperationResult:
    return TransmissionStartOperationResult(
        journal_id=journal.id,
        torrent_hash=state.torrent_hash,
        save_path=state.download_dir,
        state=str(state.status),
        progress=state.percent_done,
        ownership_tag=prepared.ownership_tag,
        seeding=state.seeding,
        replayed=replayed,
        recovered_after_unknown_result=recovered,
    )


def _removed_snapshot(prepared: _PreparedRemove) -> dict[str, Any]:
    return {
        "torrent_absent": True,
        "torrent_hash": prepared.torrent_hash,
        "delete_local_data": False,
    }


def _remove_result(
    journal: _JournalView,
    prepared: _PreparedRemove,
    *,
    replayed: bool,
    recovered: bool,
) -> TransmissionRemoveOperationResult:
    return TransmissionRemoveOperationResult(
        journal_id=journal.id,
        torrent_hash=prepared.torrent_hash,
        removed=journal.status in {OperationStatus.APPLIED, OperationStatus.NOOP},
        replayed=replayed,
        recovered_after_unknown_result=recovered,
    )


def _state_snapshot(state: TransmissionTorrentState, ownership_tag: str) -> dict[str, Any]:
    return {
        "torrent_hash": state.torrent_hash,
        "save_path": state.download_dir,
        "state": str(state.status),
        "status": state.status,
        "progress": state.percent_done,
        "recheck_progress": state.recheck_progress,
        "checking": state.checking,
        "ownership_tag": ownership_tag,
        "labels": list(state.labels),
    }


def _snapshot_was_checking(snapshot: dict[str, Any] | None) -> bool:
    return snapshot is not None and snapshot.get("checking") is True


def _state_differs_from_snapshot(
    state: TransmissionTorrentState,
    snapshot: dict[str, Any] | None,
) -> bool:
    return snapshot is not None and (
        snapshot.get("status") != state.status
        or snapshot.get("progress") != state.percent_done
        or snapshot.get("recheck_progress") != state.recheck_progress
    )


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


def _validate_candidate_key(value: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError("candidate_key 必须是 64 位十六进制摘要")


def _normalize_hash(value: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) not in {40, 64} or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError("torrent_hash 格式无效")
    return normalized


def _required_text(payload: dict[str, Any] | None, key: str) -> str:
    value = payload.get(key) if payload is not None else None
    if not isinstance(value, str) or not value:
        raise _state_mismatch("Transmission operation journal after snapshot 格式无效")
    return value


def _adapter_application_error(exc: DownloaderAdapterError, title: str) -> ApplicationError:
    return ApplicationError(code=exc.code, status=502, title=title, detail=str(exc))


def _state_mismatch(detail: str) -> ApplicationError:
    return ApplicationError(
        code="DOWNLOADER_STATE_MISMATCH",
        status=409,
        title="Transmission 实际状态与登记证据不一致",
        detail=detail,
    )


def _journal_not_executable(journal: _JournalView) -> ApplicationError:
    return ApplicationError(
        code="DOWNLOADER_OPERATION_RECONCILE_REQUIRED",
        status=409,
        title="Transmission operation journal 需要人工对账",
        detail=f"当前 journal 状态为 {journal.status.value}，不能自动重复执行",
    )


def _operation_lock(operation_key: str) -> asyncio.Lock:
    lock = _OPERATION_LOCKS.get(operation_key)
    if lock is None:
        lock = asyncio.Lock()
        _OPERATION_LOCKS[operation_key] = lock
    return lock
