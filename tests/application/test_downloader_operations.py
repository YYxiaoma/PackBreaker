from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.downloader_operations import (
    QBITTORRENT_ADD_OPERATION,
    QbittorrentAddOperationRequest,
    QbittorrentAddOperationService,
)
from backend.app.application.errors import ApplicationError
from backend.app.domain.operation import OperationStatus
from backend.app.domain.verification import VerificationLevel
from backend.app.infrastructure.adapters.downloaders import (
    DownloaderAdapterError,
    QbittorrentAddRequest,
    QbittorrentAddResult,
    QbittorrentTorrentState,
    QbittorrentWriteAdapter,
)
from backend.app.infrastructure.persistence.base import Base
from backend.app.infrastructure.persistence.database import (
    create_session_factory,
    create_sqlite_engine,
)
from backend.app.infrastructure.persistence.models import OperationJournal
from backend.app.infrastructure.persistence.repositories import TaskCreate, TaskRepository
from backend.app.infrastructure.torrent_parser import parse_torrent


class _FakeQbittorrent:
    def __init__(self) -> None:
        self.states: dict[str, QbittorrentTorrentState] = {}
        self.add_calls = 0
        self.stop_calls = 0
        self.apply_on_add = True
        self.raise_after_apply_once = False
        self.force_save_path: str | None = None
        self.force_active_after_add = False

    async def add_torrent(self, request: QbittorrentAddRequest) -> QbittorrentAddResult:
        self.add_calls += 1
        meta = parse_torrent(request.torrent_content)
        torrent_hash = meta.v1_info_hash or meta.v2_info_hash
        assert torrent_hash is not None
        if self.apply_on_add:
            self.states[torrent_hash] = QbittorrentTorrentState(
                torrent_hash=torrent_hash,
                save_path=self.force_save_path or request.save_path,
                content_path=None,
                state="downloading" if self.force_active_after_add else "stoppedUP",
                tags=request.tags,
            )
        if self.raise_after_apply_once:
            self.raise_after_apply_once = False
            raise DownloaderAdapterError("DOWNLOADER_UNAVAILABLE", "qBittorrent 响应丢失")
        return QbittorrentAddResult(1, 0, 0, (torrent_hash,), "2.15.1")

    async def get_torrents(
        self, torrent_hashes: tuple[str, ...]
    ) -> tuple[QbittorrentTorrentState, ...]:
        return tuple(self.states[item] for item in torrent_hashes if item in self.states)

    async def stop_torrent(self, torrent_hash: str) -> None:
        self.stop_calls += 1
        state = self.states[torrent_hash]
        self.states[torrent_hash] = QbittorrentTorrentState(
            torrent_hash=state.torrent_hash,
            save_path=state.save_path,
            content_path=state.content_path,
            state="stoppedDL",
            tags=state.tags,
        )

    async def start_torrent(self, torrent_hash: str) -> None:
        raise AssertionError("该切片不应启动 torrent")

    async def recheck_torrent(self, torrent_hash: str) -> None:
        raise AssertionError("该切片不应启动客户端校验")


@dataclass(frozen=True, slots=True)
class _FakeBinding:
    adapter: QbittorrentWriteAdapter
    downloader_id: str = "target-qb"
    downloader_version: int = 7
    capabilities: dict[str, Any] = field(default_factory=lambda: {"supports_skip_checking": True})


@pytest.fixture
def operation_service(
    tmp_path: Path,
) -> tuple[QbittorrentAddOperationService, sessionmaker[Session], str]:
    engine = create_sqlite_engine(tmp_path / "packbreaker-qb-add.db")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    with factory() as session:
        task, _ = TaskRepository(session).create_or_get(
            TaskCreate(
                task_type="PACKAGE_UNPACK",
                source_downloader_id="source-downloader",
                source_hash="source-hash",
                normalized_unit_key=str(uuid4()),
                trace_id=str(uuid4()),
            )
        )
        session.commit()
        task_id = task.id
    return QbittorrentAddOperationService(factory), factory, task_id


def _bencode(value: object) -> bytes:
    if isinstance(value, int):
        return f"i{value}e".encode()
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        return b"l" + b"".join(_bencode(item) for item in value) + b"e"
    if isinstance(value, Mapping):
        items = sorted(value.items(), key=lambda item: item[0])
        return b"d" + b"".join(_bencode(key) + _bencode(item) for key, item in items) + b"e"
    raise TypeError(type(value).__name__)


def _torrent() -> bytes:
    content = b"synthetic-media-content"
    pieces = b"".join(
        hashlib.sha1(content[offset : offset + 4]).digest() for offset in range(0, len(content), 4)
    )
    return _bencode(
        {
            b"info": {
                b"length": len(content),
                b"name": b"movie.mkv",
                b"piece length": 4,
                b"pieces": pieces,
            }
        }
    )


def _request(task_id: str, *, skip_checking: bool = False) -> QbittorrentAddOperationRequest:
    torrent = _torrent()
    meta = parse_torrent(torrent)
    return QbittorrentAddOperationRequest(
        task_id=task_id,
        candidate_key="c" * 64,
        downloader_id="target-qb",
        downloader_version=7,
        execution_plan_id="plan-1",
        execution_plan_digest="p" * 64,
        expected_metainfo_digest=meta.metainfo_digest,
        torrent_content=torrent,
        remote_save_path="/downloads/seed",
        verification_level=VerificationLevel.FULL_VERIFIED,
        skip_checking=skip_checking,
        tags=("media",),
    )


def _journals(factory: sessionmaker[Session]) -> list[OperationJournal]:
    with factory() as session:
        return list(session.scalars(select(OperationJournal)))


@pytest.mark.asyncio
async def test_qb_add_is_journaled_and_ten_replays_do_not_add_again(
    operation_service: tuple[QbittorrentAddOperationService, sessionmaker[Session], str],
) -> None:
    service, factory, task_id = operation_service
    adapter = _FakeQbittorrent()
    request = _request(task_id, skip_checking=True)

    binding = _FakeBinding(adapter)
    first = await service.execute(request, binding)
    repeated = [await service.execute(request, binding) for _ in range(9)]

    assert first.skip_checking is True
    assert first.replayed is False
    assert all(item.journal_id == first.journal_id and item.replayed for item in repeated)
    assert adapter.add_calls == 1
    journals = _journals(factory)
    assert len(journals) == 1
    assert journals[0].operation_type == QBITTORRENT_ADD_OPERATION
    assert journals[0].status == OperationStatus.APPLIED.value
    assert "torrent_content" not in str(journals[0].intent)


@pytest.mark.asyncio
async def test_response_lost_recovers_by_hash_save_path_and_ownership_tag_without_second_add(
    operation_service: tuple[QbittorrentAddOperationService, sessionmaker[Session], str],
) -> None:
    service, factory, task_id = operation_service
    adapter = _FakeQbittorrent()
    adapter.raise_after_apply_once = True
    request = _request(task_id)

    binding = _FakeBinding(adapter)
    with pytest.raises(ApplicationError) as lost:
        await service.execute(request, binding)
    assert lost.value.code == "DOWNLOADER_UNAVAILABLE"
    assert _journals(factory)[0].status == OperationStatus.INTENT_RECORDED.value

    recovered = await service.execute(request, binding)
    assert recovered.recovered_after_unknown_result is True
    assert recovered.replayed is True
    assert adapter.add_calls == 1
    assert _journals(factory)[0].status == OperationStatus.APPLIED.value


@pytest.mark.asyncio
async def test_add_success_without_visible_torrent_remains_unconfirmed_intent(
    operation_service: tuple[QbittorrentAddOperationService, sessionmaker[Session], str],
) -> None:
    service, factory, task_id = operation_service
    adapter = _FakeQbittorrent()
    adapter.apply_on_add = False

    with pytest.raises(ApplicationError) as failure:
        await service.execute(_request(task_id), _FakeBinding(adapter))

    assert failure.value.code == "DOWNLOADER_ADD_NOT_CONFIRMED"
    assert adapter.add_calls == 1
    assert _journals(factory)[0].status == OperationStatus.INTENT_RECORDED.value


@pytest.mark.asyncio
async def test_preexisting_torrent_is_not_claimed_and_creates_no_journal(
    operation_service: tuple[QbittorrentAddOperationService, sessionmaker[Session], str],
) -> None:
    service, factory, task_id = operation_service
    adapter = _FakeQbittorrent()
    request = _request(task_id)
    torrent_hash = parse_torrent(request.torrent_content).v1_info_hash
    assert torrent_hash is not None
    adapter.states[torrent_hash] = QbittorrentTorrentState(
        torrent_hash=torrent_hash,
        save_path="/downloads/seed",
        content_path=None,
        state="stoppedUP",
        tags=("external",),
    )

    with pytest.raises(ApplicationError) as failure:
        await service.execute(request, _FakeBinding(adapter))

    assert failure.value.code == "DOWNLOADER_TORRENT_ALREADY_EXISTS"
    assert adapter.add_calls == 0
    assert _journals(factory) == []


@pytest.mark.asyncio
async def test_wrong_save_path_after_add_requires_reconciliation(
    operation_service: tuple[QbittorrentAddOperationService, sessionmaker[Session], str],
) -> None:
    service, factory, task_id = operation_service
    adapter = _FakeQbittorrent()
    adapter.force_save_path = "/downloads/wrong"

    with pytest.raises(ApplicationError) as failure:
        await service.execute(_request(task_id), _FakeBinding(adapter))

    assert failure.value.code == "DOWNLOADER_STATE_MISMATCH"
    assert _journals(factory)[0].status == OperationStatus.RECONCILE_REQUIRED.value


@pytest.mark.asyncio
async def test_active_torrent_after_paused_add_is_stopped_before_applied(
    operation_service: tuple[QbittorrentAddOperationService, sessionmaker[Session], str],
) -> None:
    service, factory, task_id = operation_service
    adapter = _FakeQbittorrent()
    adapter.force_active_after_add = True

    result = await service.execute(_request(task_id), _FakeBinding(adapter))

    assert result.state == "stoppedDL"
    assert adapter.stop_calls == 1
    assert _journals(factory)[0].status == OperationStatus.APPLIED.value


@pytest.mark.asyncio
async def test_non_full_verification_cannot_request_skip_checking(
    operation_service: tuple[QbittorrentAddOperationService, sessionmaker[Session], str],
) -> None:
    service, factory, task_id = operation_service
    adapter = _FakeQbittorrent()
    full = _request(task_id)
    request = QbittorrentAddOperationRequest(
        task_id=full.task_id,
        candidate_key=full.candidate_key,
        downloader_id=full.downloader_id,
        downloader_version=full.downloader_version,
        execution_plan_id=full.execution_plan_id,
        execution_plan_digest=full.execution_plan_digest,
        expected_metainfo_digest=full.expected_metainfo_digest,
        torrent_content=full.torrent_content,
        remote_save_path=full.remote_save_path,
        verification_level=VerificationLevel.CLIENT_CHECK_REQUIRED,
        skip_checking=True,
    )

    with pytest.raises(ApplicationError) as failure:
        await service.execute(request, _FakeBinding(adapter))

    assert failure.value.code == "DOWNLOADER_SKIP_CHECKING_BLOCKED"
    assert adapter.add_calls == 0
    assert _journals(factory) == []


@pytest.mark.asyncio
async def test_binding_id_or_version_mismatch_is_blocked_before_network(
    operation_service: tuple[QbittorrentAddOperationService, sessionmaker[Session], str],
) -> None:
    service, factory, task_id = operation_service
    adapter = _FakeQbittorrent()

    with pytest.raises(ApplicationError) as failure:
        await service.execute(
            _request(task_id),
            _FakeBinding(adapter, downloader_id="other-qb"),
        )

    assert failure.value.code == "DOWNLOADER_CONFIG_CHANGED"
    assert adapter.add_calls == 0
    assert _journals(factory) == []
