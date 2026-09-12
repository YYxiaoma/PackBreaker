from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from typing import Any
from weakref import WeakValueDictionary

from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.downloader_operations import (
    QBITTORRENT_ADD_OPERATION,
    QBITTORRENT_RECHECK_OPERATION,
)
from backend.app.application.downloaders import QbittorrentWriteBinding, TransmissionWriteBinding
from backend.app.application.errors import ApplicationError
from backend.app.application.task_adding import CLIENT_VERIFICATION_CHECKPOINT_SCHEMA_VERSION
from backend.app.application.task_repairs import (
    REPAIR_DOWNLOAD_CHECKPOINT_SCHEMA_VERSION,
    REPAIR_STAGE_DOWNLOAD_PENDING,
    REPAIR_STAGE_DOWNLOADING,
)
from backend.app.application.transmission_operations import (
    TRANSMISSION_ADD_OPERATION,
    TRANSMISSION_VERIFY_OPERATION,
)
from backend.app.domain.downloader import normalize_remote_path
from backend.app.domain.idempotency import downloader_operation_key
from backend.app.domain.operation import OperationStatus
from backend.app.domain.task_state import TaskStatus
from backend.app.infrastructure.adapters.downloaders import DownloaderAdapterError
from backend.app.infrastructure.persistence.repositories import (
    OperationIntent,
    OperationJournalRepository,
    TaskRepository,
)

QBITTORRENT_REPAIR_START_OPERATION = "QBITTORRENT_REPAIR_START"
QBITTORRENT_REPAIR_STOP_OPERATION = "QBITTORRENT_REPAIR_STOP"
TRANSMISSION_REPAIR_START_OPERATION = "TRANSMISSION_REPAIR_START"
TRANSMISSION_REPAIR_STOP_OPERATION = "TRANSMISSION_REPAIR_STOP"
REPAIR_DOWNLOADER_OPERATION_SCHEMA_VERSION = "packbreaker-repair-downloader-operation-v1"

_OPERATION_LOCKS: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()


@dataclass(frozen=True, slots=True)
class RepairDownloadStartRequest:
    task_id: str
    candidate_key: str
    downloader_id: str
    downloader_version: int
    execution_plan_id: str
    add_journal_id: str
    source_verification_journal_id: str
    torrent_hash: str
    remote_save_path: str
    ownership_tag: str
    repair_evidence_digest: str


@dataclass(frozen=True, slots=True)
class RepairDownloadStopRequest:
    task_id: str
    candidate_key: str
    downloader_id: str
    downloader_version: int
    execution_plan_id: str
    add_journal_id: str
    source_verification_journal_id: str
    repair_start_journal_id: str
    torrent_hash: str
    remote_save_path: str
    ownership_tag: str
    repair_evidence_digest: str


@dataclass(frozen=True, slots=True)
class RepairDownloadOperationResult:
    journal_id: str
    operation_type: str
    torrent_hash: str
    state: str
    progress: float
    stopped: bool
    complete: bool
    active: bool
    replayed: bool
    recovered_after_unknown_result: bool


@dataclass(frozen=True, slots=True)
class _ObservedState:
    torrent_hash: str
    save_path: str
    state: str
    progress: float
    stopped: bool
    checking: bool
    active: bool

    @property
    def complete(self) -> bool:
        return self.progress == 1.0

    def to_payload(self, ownership_tag: str) -> dict[str, Any]:
        return {
            "torrent_hash": self.torrent_hash,
            "save_path": self.save_path,
            "state": self.state,
            "progress": self.progress,
            "ownership_tag": ownership_tag,
            "stopped": self.stopped,
            "complete": self.complete,
        }


@dataclass(frozen=True, slots=True)
class _PreparedStart:
    request: RepairDownloadStartRequest
    operation_key: str
    operation_type: str
    torrent_hash: str
    remote_save_path: str
    ownership_tag: str


@dataclass(frozen=True, slots=True)
class _PreparedStop:
    request: RepairDownloadStopRequest
    operation_key: str
    operation_type: str
    start_operation_type: str
    torrent_hash: str
    remote_save_path: str
    ownership_tag: str


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


class RepairDownloadOperationService:
    """以独立 journal 包围修复下载的 start/stop；不承担 recheck/verify。"""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    async def start(
        self,
        request: RepairDownloadStartRequest,
        binding: QbittorrentWriteBinding | TransmissionWriteBinding,
    ) -> RepairDownloadOperationResult:
        prepared = _prepare_start(request, binding)
        self._assert_start_authorization(prepared, binding)
        async with _operation_lock(prepared.operation_key):
            return await self._execute_start(prepared, binding)

    async def stop(
        self,
        request: RepairDownloadStopRequest,
        binding: QbittorrentWriteBinding | TransmissionWriteBinding,
    ) -> RepairDownloadOperationResult:
        prepared = _prepare_stop(request, binding)
        self._assert_stop_authorization(prepared)
        async with _operation_lock(prepared.operation_key):
            return await self._execute_stop(prepared, binding)

    async def _execute_start(
        self,
        prepared: _PreparedStart,
        binding: QbittorrentWriteBinding | TransmissionWriteBinding,
    ) -> RepairDownloadOperationResult:
        existing = self._load_by_key(prepared.operation_key)
        if existing is not None:
            _assert_same_start_intent(existing, prepared)
            state = await _owned_state(binding, prepared)
            if existing.status is OperationStatus.APPLIED:
                return _result(existing, state, replayed=True, recovered=False)
            if existing.status is not OperationStatus.INTENT_RECORDED:
                raise _journal_not_executable(existing)
            if _start_effect_observed(existing, state):
                applied = self._transition(
                    existing.id,
                    OperationStatus.INTENT_RECORDED,
                    OperationStatus.APPLIED,
                    after_snapshot=state.to_payload(prepared.ownership_tag),
                )
                return _result(applied, state, replayed=True, recovered=True)
            journal = existing
            replayed = True
        else:
            before = await _owned_state(binding, prepared)
            if not before.stopped or before.checking or before.complete:
                raise ApplicationError(
                    code="REPAIR_DOWNLOAD_START_STATE_INVALID",
                    status=409,
                    title="修复下载启动状态无效",
                    detail=(
                        "只有停止、未校验中且 progress<1 的 PackBreaker torrent 才允许启动修复下载"
                    ),
                )
            journal = self._record_start_intent(prepared, before)
            replayed = False

        try:
            await binding.adapter.start_torrent(prepared.torrent_hash)
        except DownloaderAdapterError as exc:
            raise _adapter_error(exc, "修复下载 start 结果未知") from exc

        after = await _owned_state(binding, prepared)
        if after.checking:
            self._mark_reconcile(journal.id)
            raise _state_mismatch("修复下载 start 后 torrent 意外进入 checking 状态")
        if not _start_effect_observed(journal, after):
            raise ApplicationError(
                code="REPAIR_DOWNLOAD_START_NOT_CONFIRMED",
                status=409,
                title="修复下载启动尚未确认",
                detail="start 已发送但 torrent 仍保持原停止/不完整状态；后续重试会先读取真实状态",
            )
        applied = self._transition(
            journal.id,
            OperationStatus.INTENT_RECORDED,
            OperationStatus.APPLIED,
            after_snapshot=after.to_payload(prepared.ownership_tag),
        )
        return _result(applied, after, replayed=replayed, recovered=False)

    async def _execute_stop(
        self,
        prepared: _PreparedStop,
        binding: QbittorrentWriteBinding | TransmissionWriteBinding,
    ) -> RepairDownloadOperationResult:
        existing = self._load_by_key(prepared.operation_key)
        if existing is not None:
            _assert_same_stop_intent(existing, prepared)
            state = await _owned_state(binding, prepared)
            if existing.status in {OperationStatus.APPLIED, OperationStatus.NOOP}:
                if not state.stopped or not state.complete:
                    self._mark_reconcile(existing.id)
                    raise _state_mismatch("已确认完成的 repair stop 当前不再是停止且完整状态")
                return _result(existing, state, replayed=True, recovered=False)
            if existing.status is not OperationStatus.INTENT_RECORDED:
                raise _journal_not_executable(existing)
            if state.stopped and state.complete:
                applied = self._transition(
                    existing.id,
                    OperationStatus.INTENT_RECORDED,
                    OperationStatus.APPLIED,
                    after_snapshot=state.to_payload(prepared.ownership_tag),
                )
                return _result(applied, state, replayed=True, recovered=True)
            if not state.complete or state.checking:
                self._mark_reconcile(existing.id)
                raise _state_mismatch("repair stop intent 存在时 torrent 已不再处于完整可停止状态")
            journal = existing
            replayed = True
        else:
            before = await _owned_state(binding, prepared)
            if before.stopped:
                if not before.complete or before.checking:
                    raise ApplicationError(
                        code="REPAIR_DOWNLOAD_STOP_STATE_INVALID",
                        status=409,
                        title="修复下载停止状态无效",
                        detail="已停止 torrent 只有在完整且未 checking 时才能登记 repair stop NOOP",
                    )
                journal = self._record_stop_intent(prepared, before)
                no_op = self._transition(
                    journal.id,
                    OperationStatus.INTENT_RECORDED,
                    OperationStatus.NOOP,
                )
                return _result(no_op, before, replayed=False, recovered=False)
            if not before.complete or before.checking:
                raise ApplicationError(
                    code="REPAIR_DOWNLOAD_STOP_STATE_INVALID",
                    status=409,
                    title="修复下载停止状态无效",
                    detail=(
                        "只有已补齐到 100% 且未处于 checking 的 owned torrent "
                        "才允许执行 repair stop"
                    ),
                )
            journal = self._record_stop_intent(prepared, before)
            replayed = False

        try:
            await binding.adapter.stop_torrent(prepared.torrent_hash)
        except DownloaderAdapterError as exc:
            raise _adapter_error(exc, "修复下载 stop 结果未知") from exc
        after = await _owned_state(binding, prepared)
        if not after.stopped or not after.complete:
            if not after.complete or after.checking:
                self._mark_reconcile(journal.id)
                raise _state_mismatch("repair stop 后 torrent 不再满足完整度/状态安全约束")
            raise ApplicationError(
                code="REPAIR_DOWNLOAD_STOP_NOT_CONFIRMED",
                status=409,
                title="修复下载停止尚未确认",
                detail="stop 已发送但 torrent 仍处于完整活动状态；后续重试会先读取真实状态",
            )
        applied = self._transition(
            journal.id,
            OperationStatus.INTENT_RECORDED,
            OperationStatus.APPLIED,
            after_snapshot=after.to_payload(prepared.ownership_tag),
        )
        return _result(applied, after, replayed=replayed, recovered=False)

    def _assert_start_authorization(
        self,
        prepared: _PreparedStart,
        binding: QbittorrentWriteBinding | TransmissionWriteBinding,
    ) -> None:
        request = prepared.request
        if (
            binding.downloader_id != request.downloader_id
            or binding.downloader_version != request.downloader_version
        ):
            raise _authorization_invalid("下载器 ID/version 与 repair checkpoint 不一致")
        add_type, verification_type, add_reference_key = _authorization_types(binding)
        with self._session_factory() as session:
            repository = OperationJournalRepository(session)
            add = repository.get(request.add_journal_id)
            verification = repository.get(request.source_verification_journal_id)
            task = TaskRepository(session).get(request.task_id)
            add_valid = (
                add is not None
                and add.task_id == request.task_id
                and add.operation_type == add_type
                and OperationStatus(add.status) is OperationStatus.APPLIED
                and add.intent.get("execution_plan_id") == request.execution_plan_id
                and add.target.get("downloader_id") == request.downloader_id
                and add.after_snapshot is not None
                and add.after_snapshot.get("torrent_hash") == prepared.torrent_hash
                and add.after_snapshot.get("save_path") == prepared.remote_save_path
                and add.after_snapshot.get("ownership_tag") == prepared.ownership_tag
            )
            verification_valid = (
                verification is not None
                and verification.task_id == request.task_id
                and verification.operation_type == verification_type
                and OperationStatus(verification.status) is OperationStatus.APPLIED
                and verification.intent.get("execution_plan_id") == request.execution_plan_id
                and verification.intent.get(add_reference_key) == request.add_journal_id
                and verification.intent.get("torrent_hash") == prepared.torrent_hash
                and verification.intent.get("remote_save_path") == prepared.remote_save_path
                and verification.intent.get("ownership_tag") == prepared.ownership_tag
            )
            if not add_valid or not verification_valid:
                raise _authorization_invalid(
                    "repair download 必须绑定同一 execution plan 的 APPLIED add "
                    "与停止/不完整 verify journal"
                )
            _assert_repair_checkpoint(
                task,
                request=request,
                repository=repository,
            )

    def _assert_stop_authorization(self, prepared: _PreparedStop) -> None:
        request = prepared.request
        with self._session_factory() as session:
            repository = OperationJournalRepository(session)
            start = repository.get(request.repair_start_journal_id)
            task = TaskRepository(session).get(request.task_id)
            if (
                start is None
                or start.task_id != request.task_id
                or start.operation_type != prepared.start_operation_type
                or OperationStatus(start.status) is not OperationStatus.APPLIED
                or start.intent.get("execution_plan_id") != request.execution_plan_id
                or start.intent.get("add_journal_id") != request.add_journal_id
                or start.intent.get("source_verification_journal_id")
                != request.source_verification_journal_id
                or start.intent.get("repair_evidence_digest") != request.repair_evidence_digest
                or start.intent.get("torrent_hash") != prepared.torrent_hash
                or start.intent.get("remote_save_path") != prepared.remote_save_path
                or start.intent.get("ownership_tag") != prepared.ownership_tag
            ):
                raise _authorization_invalid(
                    "repair stop 必须绑定同一修复轮次已 APPLIED 的 repair start journal"
                )
            _assert_repair_checkpoint(
                task,
                request=request,
                repository=repository,
            )

    def _record_start_intent(
        self,
        prepared: _PreparedStart,
        before: _ObservedState,
    ) -> _JournalView:
        return self._record_intent(
            OperationIntent(
                task_id=prepared.request.task_id,
                idempotency_key=prepared.operation_key,
                operation_type=prepared.operation_type,
                target={
                    "downloader_id": prepared.request.downloader_id,
                    "torrent_hash": prepared.torrent_hash,
                },
                intent=_start_intent_payload(prepared),
                before_snapshot=before.to_payload(prepared.ownership_tag),
            )
        )

    def _record_stop_intent(
        self,
        prepared: _PreparedStop,
        before: _ObservedState,
    ) -> _JournalView:
        return self._record_intent(
            OperationIntent(
                task_id=prepared.request.task_id,
                idempotency_key=prepared.operation_key,
                operation_type=prepared.operation_type,
                target={
                    "downloader_id": prepared.request.downloader_id,
                    "torrent_hash": prepared.torrent_hash,
                },
                intent=_stop_intent_payload(prepared),
                before_snapshot=before.to_payload(prepared.ownership_tag),
            )
        )

    def _record_intent(self, intent: OperationIntent) -> _JournalView:
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


async def _owned_state(
    binding: QbittorrentWriteBinding | TransmissionWriteBinding,
    prepared: _PreparedStart | _PreparedStop,
) -> _ObservedState:
    if isinstance(binding, QbittorrentWriteBinding):
        try:
            observed = await binding.adapter.get_torrents((prepared.torrent_hash,))
        except DownloaderAdapterError as exc:
            raise _adapter_error(exc, "无法确认 qBittorrent repair torrent 状态") from exc
        matching_qbit = tuple(
            state
            for state in observed
            if state.torrent_hash == prepared.torrent_hash
            and state.save_path == prepared.remote_save_path
            and prepared.ownership_tag in state.tags
        )
        if len(observed) != 1 or len(matching_qbit) != 1:
            raise _state_mismatch("repair torrent 的 hash、save path、ownership tag 或存在性不匹配")
        state_qbit = matching_qbit[0]
        return _ObservedState(
            state_qbit.torrent_hash,
            state_qbit.save_path,
            state_qbit.state,
            state_qbit.progress,
            state_qbit.stopped,
            state_qbit.checking,
            not state_qbit.stopped and not state_qbit.checking,
        )
    try:
        observed_transmission = await binding.adapter.get_torrents((prepared.torrent_hash,))
    except DownloaderAdapterError as exc:
        raise _adapter_error(exc, "无法确认 Transmission repair torrent 状态") from exc
    matching_transmission = tuple(
        state
        for state in observed_transmission
        if state.torrent_hash == prepared.torrent_hash
        and state.download_dir == prepared.remote_save_path
        and prepared.ownership_tag in state.labels
    )
    if len(observed_transmission) != 1 or len(matching_transmission) != 1:
        raise _state_mismatch("repair torrent 的 hash、save path、ownership label 或存在性不匹配")
    state_transmission = matching_transmission[0]
    return _ObservedState(
        state_transmission.torrent_hash,
        state_transmission.download_dir,
        str(state_transmission.status),
        state_transmission.percent_done,
        state_transmission.stopped,
        state_transmission.checking,
        not state_transmission.stopped and not state_transmission.checking,
    )


def _prepare_start(
    request: RepairDownloadStartRequest,
    binding: QbittorrentWriteBinding | TransmissionWriteBinding,
) -> _PreparedStart:
    _validate_common(
        request.candidate_key, request.downloader_version, request.repair_evidence_digest
    )
    torrent_hash = _normalize_hash(request.torrent_hash)
    remote_save_path = normalize_remote_path(request.remote_save_path)
    ownership_tag = _normalize_ownership(request.ownership_tag)
    operation_type = (
        QBITTORRENT_REPAIR_START_OPERATION
        if isinstance(binding, QbittorrentWriteBinding)
        else TRANSMISSION_REPAIR_START_OPERATION
    )
    return _PreparedStart(
        request=request,
        operation_key=downloader_operation_key(
            candidate_key=request.candidate_key,
            operation_type=operation_type,
            downloader_id=request.downloader_id,
        ),
        operation_type=operation_type,
        torrent_hash=torrent_hash,
        remote_save_path=remote_save_path,
        ownership_tag=ownership_tag,
    )


def _prepare_stop(
    request: RepairDownloadStopRequest,
    binding: QbittorrentWriteBinding | TransmissionWriteBinding,
) -> _PreparedStop:
    _validate_common(
        request.candidate_key, request.downloader_version, request.repair_evidence_digest
    )
    torrent_hash = _normalize_hash(request.torrent_hash)
    remote_save_path = normalize_remote_path(request.remote_save_path)
    ownership_tag = _normalize_ownership(request.ownership_tag)
    if isinstance(binding, QbittorrentWriteBinding):
        operation_type = QBITTORRENT_REPAIR_STOP_OPERATION
        start_operation_type = QBITTORRENT_REPAIR_START_OPERATION
    else:
        operation_type = TRANSMISSION_REPAIR_STOP_OPERATION
        start_operation_type = TRANSMISSION_REPAIR_START_OPERATION
    return _PreparedStop(
        request=request,
        operation_key=downloader_operation_key(
            candidate_key=request.candidate_key,
            operation_type=operation_type,
            downloader_id=request.downloader_id,
        ),
        operation_type=operation_type,
        start_operation_type=start_operation_type,
        torrent_hash=torrent_hash,
        remote_save_path=remote_save_path,
        ownership_tag=ownership_tag,
    )


def _validate_common(candidate_key: str, downloader_version: int, evidence_digest: str) -> None:
    if downloader_version < 1:
        raise ValueError("downloader version 必须大于等于 1")
    if not _is_digest(candidate_key):
        raise ValueError("repair candidate_key 必须是 64 位十六进制摘要")
    if not _is_digest(evidence_digest):
        raise ValueError("repair evidence digest 必须是 64 位十六进制摘要")


def _normalize_hash(value: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) not in {40, 64} or any(
        char not in "0123456789abcdef" for char in normalized
    ):
        raise ValueError("torrent_hash 格式无效")
    return normalized


def _normalize_ownership(value: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 128 or "\x00" in normalized or "," in normalized:
        raise ValueError("ownership tag/label 格式无效")
    return normalized


def _authorization_types(
    binding: QbittorrentWriteBinding | TransmissionWriteBinding,
) -> tuple[str, str, str]:
    if isinstance(binding, QbittorrentWriteBinding):
        return QBITTORRENT_ADD_OPERATION, QBITTORRENT_RECHECK_OPERATION, "qbit_add_journal_id"
    return TRANSMISSION_ADD_OPERATION, TRANSMISSION_VERIFY_OPERATION, "add_journal_id"


def _assert_repair_checkpoint(
    task: Any,
    *,
    request: RepairDownloadStartRequest | RepairDownloadStopRequest,
    repository: OperationJournalRepository,
) -> None:
    if task is None or task.status != TaskStatus.CLIENT_VERIFYING.value:
        raise _authorization_invalid("task 未处于已冻结 repair download 的 CLIENT_VERIFYING 状态")
    checkpoint = task.checkpoint
    if (
        checkpoint.get("schema_version") != CLIENT_VERIFICATION_CHECKPOINT_SCHEMA_VERSION
        or checkpoint.get("stage") != TaskStatus.CLIENT_VERIFYING.value
        or checkpoint.get("repair_schema_version") != REPAIR_DOWNLOAD_CHECKPOINT_SCHEMA_VERSION
        or checkpoint.get("repair_stage")
        not in {REPAIR_STAGE_DOWNLOAD_PENDING, REPAIR_STAGE_DOWNLOADING}
        or checkpoint.get("execution_plan_id") != request.execution_plan_id
        or checkpoint.get("add_journal_id") != request.add_journal_id
        or checkpoint.get("repair_source_verification_journal_id")
        != request.source_verification_journal_id
        or checkpoint.get("repair_candidate_key") != request.candidate_key
        or checkpoint.get("repair_evidence_digest") != request.repair_evidence_digest
        or checkpoint.get("target_downloader_id") != request.downloader_id
        or checkpoint.get("target_downloader_version") != request.downloader_version
        or checkpoint.get("torrent_hash") != request.torrent_hash
        or checkpoint.get("remote_save_path") != request.remote_save_path
        or checkpoint.get("ownership_tag") != request.ownership_tag
    ):
        raise _authorization_invalid("repair download 请求与当前持久化 repair checkpoint 不一致")

    isolation_ids = checkpoint.get("repair_isolation_journal_ids")
    if not isinstance(isolation_ids, list) or any(
        not isinstance(journal_id, str) or not journal_id for journal_id in isolation_ids
    ):
        raise _authorization_invalid("repair checkpoint 缺少有效 isolation journal 列表")
    if len(set(isolation_ids)) != len(isolation_ids):
        raise _authorization_invalid("repair checkpoint 包含重复 isolation journal")
    for journal_id in isolation_ids:
        isolation = repository.get(journal_id)
        if (
            isolation is None
            or isolation.task_id != request.task_id
            or isolation.operation_type != "ISOLATE_REPAIR_TARGET"
            or OperationStatus(isolation.status) is not OperationStatus.APPLIED
        ):
            raise _authorization_invalid(
                "repair download 前必须重新证明所有登记 isolation journal 均为 APPLIED"
            )

    if isinstance(request, RepairDownloadStopRequest):
        checkpoint_start = checkpoint.get("repair_start_journal_id")
        if checkpoint.get("repair_stage") == REPAIR_STAGE_DOWNLOADING and (
            checkpoint_start != request.repair_start_journal_id
        ):
            raise _authorization_invalid("repair stop 与 checkpoint 冻结的 start journal 不一致")
        if checkpoint_start is not None and checkpoint_start != request.repair_start_journal_id:
            raise _authorization_invalid("repair stop 引用的 start journal 已发生漂移")


def _start_intent_payload(prepared: _PreparedStart) -> dict[str, Any]:
    request = prepared.request
    return {
        "schema_version": REPAIR_DOWNLOADER_OPERATION_SCHEMA_VERSION,
        "downloader_version": request.downloader_version,
        "execution_plan_id": request.execution_plan_id,
        "add_journal_id": request.add_journal_id,
        "source_verification_journal_id": request.source_verification_journal_id,
        "repair_evidence_digest": request.repair_evidence_digest,
        "torrent_hash": prepared.torrent_hash,
        "remote_save_path": prepared.remote_save_path,
        "ownership_tag": prepared.ownership_tag,
    }


def _stop_intent_payload(prepared: _PreparedStop) -> dict[str, Any]:
    payload = _start_intent_payload(
        _PreparedStart(
            request=RepairDownloadStartRequest(
                task_id=prepared.request.task_id,
                candidate_key=prepared.request.candidate_key,
                downloader_id=prepared.request.downloader_id,
                downloader_version=prepared.request.downloader_version,
                execution_plan_id=prepared.request.execution_plan_id,
                add_journal_id=prepared.request.add_journal_id,
                source_verification_journal_id=prepared.request.source_verification_journal_id,
                torrent_hash=prepared.torrent_hash,
                remote_save_path=prepared.remote_save_path,
                ownership_tag=prepared.ownership_tag,
                repair_evidence_digest=prepared.request.repair_evidence_digest,
            ),
            operation_key="",
            operation_type=prepared.start_operation_type,
            torrent_hash=prepared.torrent_hash,
            remote_save_path=prepared.remote_save_path,
            ownership_tag=prepared.ownership_tag,
        )
    )
    payload["repair_start_journal_id"] = prepared.request.repair_start_journal_id
    return payload


def _assert_same_start_intent(journal: _JournalView, prepared: _PreparedStart) -> None:
    if (
        journal.task_id != prepared.request.task_id
        or journal.operation_type != prepared.operation_type
        or journal.target
        != {
            "downloader_id": prepared.request.downloader_id,
            "torrent_hash": prepared.torrent_hash,
        }
        or journal.intent != _start_intent_payload(prepared)
    ):
        raise _idempotency_conflict("repair start")


def _assert_same_stop_intent(journal: _JournalView, prepared: _PreparedStop) -> None:
    if (
        journal.task_id != prepared.request.task_id
        or journal.operation_type != prepared.operation_type
        or journal.target
        != {
            "downloader_id": prepared.request.downloader_id,
            "torrent_hash": prepared.torrent_hash,
        }
        or journal.intent != _stop_intent_payload(prepared)
    ):
        raise _idempotency_conflict("repair stop")


def _start_effect_observed(journal: _JournalView, state: _ObservedState) -> bool:
    if state.checking:
        return False
    before = journal.before_snapshot
    if before is None:
        return state.active or state.complete
    return (
        state.active
        or state.complete
        or before.get("state") != state.state
        or before.get("progress") != state.progress
    )


def _result(
    journal: _JournalView,
    state: _ObservedState,
    *,
    replayed: bool,
    recovered: bool,
) -> RepairDownloadOperationResult:
    return RepairDownloadOperationResult(
        journal_id=journal.id,
        operation_type=journal.operation_type,
        torrent_hash=state.torrent_hash,
        state=state.state,
        progress=state.progress,
        stopped=state.stopped,
        complete=state.complete,
        active=state.active,
        replayed=replayed,
        recovered_after_unknown_result=recovered,
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


def _operation_lock(operation_key: str) -> asyncio.Lock:
    lock = _OPERATION_LOCKS.get(operation_key)
    if lock is None:
        lock = asyncio.Lock()
        _OPERATION_LOCKS[operation_key] = lock
    return lock


def _is_digest(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _authorization_invalid(detail: str) -> ApplicationError:
    return ApplicationError(
        code="REPAIR_DOWNLOAD_AUTHORIZATION_INVALID",
        status=409,
        title="修复下载授权证据无效",
        detail=detail,
    )


def _idempotency_conflict(action: str) -> ApplicationError:
    return ApplicationError(
        code="REPAIR_DOWNLOAD_IDEMPOTENCY_CONFLICT",
        status=409,
        title="修复下载幂等键冲突",
        detail=f"同一修复轮次对应了不同的 {action} intent",
    )


def _state_mismatch(detail: str) -> ApplicationError:
    return ApplicationError(
        code="REPAIR_DOWNLOAD_STATE_MISMATCH",
        status=409,
        title="修复下载真实状态与安全证据不一致",
        detail=detail,
    )


def _journal_not_executable(journal: _JournalView) -> ApplicationError:
    return ApplicationError(
        code="REPAIR_DOWNLOAD_JOURNAL_NOT_EXECUTABLE",
        status=409,
        title="修复下载 operation journal 不能继续执行",
        detail=f"journal 当前状态为 {journal.status.value}",
    )


def _adapter_error(exc: DownloaderAdapterError, detail: str) -> ApplicationError:
    return ApplicationError(
        code="REPAIR_DOWNLOAD_DOWNLOADER_UNAVAILABLE",
        status=502,
        title="修复下载器调用失败",
        detail=f"{detail}；下载器返回稳定错误码：{exc.code}",
    )
