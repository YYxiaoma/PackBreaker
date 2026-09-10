from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Protocol

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
        adapter = binding.adapter
        prepared = self._prepare(request)
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


@dataclass(frozen=True, slots=True)
class _PreparedAdd:
    operation_key: str
    ownership_tag: str
    remote_save_path: str
    expected_hashes: tuple[str, ...]
    torrent_payload_digest: str
    request: QbittorrentAddOperationRequest


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
