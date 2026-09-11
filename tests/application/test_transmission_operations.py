from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.errors import ApplicationError
from backend.app.application.transmission_operations import (
    TRANSMISSION_ADD_OPERATION,
    TRANSMISSION_START_OPERATION,
    TRANSMISSION_VERIFY_OPERATION,
    TransmissionAddOperationRequest,
    TransmissionAddOperationService,
    TransmissionStartOperationRequest,
    TransmissionStartOperationService,
    TransmissionVerifyOperationRequest,
    TransmissionVerifyOperationResult,
    TransmissionVerifyOperationService,
)
from backend.app.domain.operation import OperationStatus
from backend.app.infrastructure.adapters.downloaders import (
    DownloaderAdapterError,
    TransmissionAddRequest,
    TransmissionAddResult,
    TransmissionTorrentState,
    TransmissionWriteAdapter,
)
from backend.app.infrastructure.persistence.base import Base
from backend.app.infrastructure.persistence.database import (
    create_session_factory,
    create_sqlite_engine,
)
from backend.app.infrastructure.persistence.models import OperationJournal
from backend.app.infrastructure.persistence.repositories import (
    TaskCreate,
    TaskRepository,
)
from backend.app.infrastructure.torrent_parser import parse_torrent


class _FakeTransmission:
    def __init__(self) -> None:
        self.states: dict[str, TransmissionTorrentState] = {}
        self.add_calls = 0
        self.verify_calls = 0
        self.start_calls = 0
        self.stop_calls = 0
        self.raise_after_add_apply_once = False
        self.raise_after_verify_apply_once = False
        self.raise_after_start_apply_once = False
        self.delay_add = False
        self.delay_start = False
        self.apply_on_start = True
        self.start_status = 5

    async def add_torrent(self, request: TransmissionAddRequest) -> TransmissionAddResult:
        self.add_calls += 1
        if self.delay_add:
            await asyncio.sleep(0.01)
        meta = parse_torrent(request.torrent_content)
        torrent_hash = meta.v1_info_hash or meta.v2_info_hash
        assert torrent_hash is not None
        if torrent_hash in self.states:
            return TransmissionAddResult(torrent_hash=torrent_hash, duplicate=True)
        self.states[torrent_hash] = TransmissionTorrentState(
            torrent_hash=torrent_hash,
            download_dir=request.save_path,
            status=0,
            labels=request.labels,
            percent_done=1.0,
            recheck_progress=0.0,
        )
        if self.raise_after_add_apply_once:
            self.raise_after_add_apply_once = False
            raise DownloaderAdapterError("DOWNLOADER_UNAVAILABLE", "synthetic add response lost")
        return TransmissionAddResult(torrent_hash=torrent_hash, duplicate=False)

    async def get_torrents(
        self, torrent_hashes: tuple[str, ...]
    ) -> tuple[TransmissionTorrentState, ...]:
        return tuple(self.states[item] for item in torrent_hashes if item in self.states)

    async def stop_torrent(self, torrent_hash: str) -> None:
        self.stop_calls += 1
        self.states[torrent_hash] = replace(self.states[torrent_hash], status=0)

    async def start_torrent(self, torrent_hash: str) -> None:
        self.start_calls += 1
        if self.delay_start:
            await asyncio.sleep(0.01)
        if self.apply_on_start:
            self.states[torrent_hash] = replace(
                self.states[torrent_hash],
                status=self.start_status,
            )
        if self.raise_after_start_apply_once:
            self.raise_after_start_apply_once = False
            raise DownloaderAdapterError("DOWNLOADER_UNAVAILABLE", "synthetic start response lost")

    async def verify_torrent(self, torrent_hash: str) -> None:
        self.verify_calls += 1
        self.states[torrent_hash] = replace(
            self.states[torrent_hash],
            status=2,
            recheck_progress=0.1,
        )
        if self.raise_after_verify_apply_once:
            self.raise_after_verify_apply_once = False
            raise DownloaderAdapterError("DOWNLOADER_UNAVAILABLE", "synthetic verify response lost")

    async def remove_torrent_keep_files(self, torrent_hash: str) -> None:
        self.states.pop(torrent_hash, None)


@dataclass(frozen=True, slots=True)
class _Binding:
    adapter: TransmissionWriteAdapter
    downloader_id: str = "target-tr"
    downloader_version: int = 4
    capabilities: dict[str, Any] = field(
        default_factory=lambda: {
            "supports_skip_checking": False,
            "supports_force_recheck": True,
            "supports_verify_progress": True,
        }
    )


@pytest.fixture
def operation_fixture(
    tmp_path: Path,
) -> tuple[sessionmaker[Session], str, _FakeTransmission, _Binding, bytes]:
    engine = create_sqlite_engine(tmp_path / "transmission-operations.db")
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
    adapter = _FakeTransmission()
    binding = _Binding(adapter=adapter)
    return factory, task_id, adapter, binding, _torrent()


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
    content = b"synthetic-transmission-content"
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


def _add_request(task_id: str, torrent: bytes) -> TransmissionAddOperationRequest:
    return TransmissionAddOperationRequest(
        task_id=task_id,
        candidate_key="a" * 64,
        downloader_id="target-tr",
        downloader_version=4,
        execution_plan_id="plan-tr",
        execution_plan_digest="b" * 64,
        expected_metainfo_digest=parse_torrent(torrent).metainfo_digest,
        torrent_content=torrent,
        remote_save_path="/downloads/target",
    )


def _start_request(
    task_id: str,
    add_journal_id: str,
    verify_result: TransmissionVerifyOperationResult,
) -> TransmissionStartOperationRequest:
    return TransmissionStartOperationRequest(
        task_id=task_id,
        candidate_key="a" * 64,
        downloader_id="target-tr",
        downloader_version=4,
        execution_plan_id="plan-tr",
        add_journal_id=add_journal_id,
        verification_journal_id=verify_result.journal_id,
        torrent_hash=verify_result.torrent_hash,
        remote_save_path=verify_result.save_path,
        ownership_tag=verify_result.ownership_tag,
    )


async def _verified_chain(
    factory: sessionmaker[Session],
    task_id: str,
    adapter: _FakeTransmission,
    binding: _Binding,
    torrent: bytes,
) -> tuple[str, TransmissionVerifyOperationResult]:
    add_result = await TransmissionAddOperationService(factory).execute(
        _add_request(task_id, torrent), binding
    )
    verify_request = TransmissionVerifyOperationRequest(
        task_id=task_id,
        candidate_key="a" * 64,
        downloader_id=binding.downloader_id,
        downloader_version=binding.downloader_version,
        execution_plan_id="plan-tr",
        add_journal_id=add_result.journal_id,
        torrent_hash=add_result.torrent_hash,
        remote_save_path=add_result.save_path,
        ownership_tag=add_result.ownership_tag,
    )
    verify_service = TransmissionVerifyOperationService(factory)
    await verify_service.execute(verify_request, binding)
    adapter.states[add_result.torrent_hash] = replace(
        adapter.states[add_result.torrent_hash],
        status=0,
        percent_done=1.0,
        recheck_progress=1.0,
    )
    verified = await verify_service.execute(verify_request, binding)
    assert verified.verification_complete is True
    return add_result.journal_id, verified


@pytest.mark.asyncio
async def test_transmission_add_ten_concurrent_replays_create_one_remote_task(
    operation_fixture: tuple[sessionmaker[Session], str, _FakeTransmission, _Binding, bytes],
) -> None:
    factory, task_id, adapter, binding, torrent = operation_fixture
    adapter.delay_add = True
    service = TransmissionAddOperationService(factory)

    results = await asyncio.gather(
        *(service.execute(_add_request(task_id, torrent), binding) for _ in range(10))
    )

    assert adapter.add_calls == 1
    assert len({item.journal_id for item in results}) == 1
    assert sum(item.replayed for item in results) == 9
    assert all(item.skip_checking is False for item in results)
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(OperationJournal)) == 1
        journal = session.scalar(select(OperationJournal))
        assert journal is not None
        assert journal.operation_type == TRANSMISSION_ADD_OPERATION
        assert journal.status == OperationStatus.APPLIED.value


@pytest.mark.asyncio
async def test_transmission_add_response_loss_recovers_from_owned_label_without_second_add(
    operation_fixture: tuple[sessionmaker[Session], str, _FakeTransmission, _Binding, bytes],
) -> None:
    factory, task_id, adapter, binding, torrent = operation_fixture
    adapter.raise_after_add_apply_once = True
    service = TransmissionAddOperationService(factory)

    with pytest.raises(ApplicationError) as failure:
        await service.execute(_add_request(task_id, torrent), binding)
    assert failure.value.code == "DOWNLOADER_UNAVAILABLE"
    assert adapter.add_calls == 1

    recovered = await service.execute(_add_request(task_id, torrent), binding)

    assert recovered.recovered_after_unknown_result is True
    assert adapter.add_calls == 1
    assert recovered.ownership_tag in next(iter(adapter.states.values())).labels


@pytest.mark.asyncio
async def test_transmission_preexisting_torrent_is_never_claimed(
    operation_fixture: tuple[sessionmaker[Session], str, _FakeTransmission, _Binding, bytes],
) -> None:
    factory, task_id, adapter, binding, torrent = operation_fixture
    meta = parse_torrent(torrent)
    torrent_hash = meta.v1_info_hash or meta.v2_info_hash
    assert torrent_hash is not None
    adapter.states[torrent_hash] = TransmissionTorrentState(
        torrent_hash=torrent_hash,
        download_dir="/downloads/target",
        status=0,
        labels=("external",),
        percent_done=1.0,
        recheck_progress=0.0,
    )

    with pytest.raises(ApplicationError) as failure:
        await TransmissionAddOperationService(factory).execute(
            _add_request(task_id, torrent), binding
        )

    assert failure.value.code == "DOWNLOADER_TORRENT_ALREADY_EXISTS"
    assert adapter.add_calls == 0
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(OperationJournal)) == 0


@pytest.mark.asyncio
async def test_transmission_verify_response_loss_recovers_and_completion_is_proven(
    operation_fixture: tuple[sessionmaker[Session], str, _FakeTransmission, _Binding, bytes],
) -> None:
    factory, task_id, adapter, binding, torrent = operation_fixture
    add_result = await TransmissionAddOperationService(factory).execute(
        _add_request(task_id, torrent), binding
    )
    verify_request = TransmissionVerifyOperationRequest(
        task_id=task_id,
        candidate_key="a" * 64,
        downloader_id=binding.downloader_id,
        downloader_version=binding.downloader_version,
        execution_plan_id="plan-tr",
        add_journal_id=add_result.journal_id,
        torrent_hash=add_result.torrent_hash,
        remote_save_path=add_result.save_path,
        ownership_tag=add_result.ownership_tag,
    )
    verify_service = TransmissionVerifyOperationService(factory)
    adapter.raise_after_verify_apply_once = True

    with pytest.raises(ApplicationError) as failure:
        await verify_service.execute(verify_request, binding)
    assert failure.value.code == "DOWNLOADER_UNAVAILABLE"
    assert adapter.verify_calls == 1

    recovered = await verify_service.execute(verify_request, binding)
    assert recovered.checking is True
    assert recovered.checking_observed is True
    assert recovered.recovered_after_unknown_result is True
    assert adapter.verify_calls == 1

    adapter.states[add_result.torrent_hash] = replace(
        adapter.states[add_result.torrent_hash],
        status=0,
        percent_done=1.0,
        recheck_progress=1.0,
    )
    completed = await verify_service.execute(verify_request, binding)

    assert completed.verification_complete is True
    assert completed.completion_proven is True
    assert adapter.verify_calls == 1
    with factory() as session:
        journals = session.scalars(
            select(OperationJournal).order_by(OperationJournal.created_at)
        ).all()
        assert [item.operation_type for item in journals] == [
            TRANSMISSION_ADD_OPERATION,
            TRANSMISSION_VERIFY_OPERATION,
        ]
        assert all(item.status == OperationStatus.APPLIED.value for item in journals)


@pytest.mark.asyncio
async def test_transmission_start_ten_concurrent_replays_start_once_and_accept_queued_seed(
    operation_fixture: tuple[sessionmaker[Session], str, _FakeTransmission, _Binding, bytes],
) -> None:
    factory, task_id, adapter, binding, torrent = operation_fixture
    add_journal_id, verified = await _verified_chain(factory, task_id, adapter, binding, torrent)
    adapter.delay_start = True
    service = TransmissionStartOperationService(factory)
    request = _start_request(task_id, add_journal_id, verified)

    results = await asyncio.gather(*(service.execute(request, binding) for _ in range(10)))

    assert adapter.start_calls == 1
    assert adapter.states[verified.torrent_hash].status == 5
    assert all(item.seeding and item.progress == 1.0 for item in results)
    assert len({item.journal_id for item in results}) == 1
    assert sum(item.replayed for item in results) == 9
    with factory() as session:
        journals = session.scalars(
            select(OperationJournal).order_by(OperationJournal.created_at)
        ).all()
        assert [item.operation_type for item in journals] == [
            TRANSMISSION_ADD_OPERATION,
            TRANSMISSION_VERIFY_OPERATION,
            TRANSMISSION_START_OPERATION,
        ]
        assert all(item.status == OperationStatus.APPLIED.value for item in journals)


@pytest.mark.asyncio
async def test_transmission_start_response_loss_recovers_without_second_start(
    operation_fixture: tuple[sessionmaker[Session], str, _FakeTransmission, _Binding, bytes],
) -> None:
    factory, task_id, adapter, binding, torrent = operation_fixture
    add_journal_id, verified = await _verified_chain(factory, task_id, adapter, binding, torrent)
    service = TransmissionStartOperationService(factory)
    request = _start_request(task_id, add_journal_id, verified)
    adapter.raise_after_start_apply_once = True

    with pytest.raises(ApplicationError) as failure:
        await service.execute(request, binding)
    assert failure.value.code == "DOWNLOADER_UNAVAILABLE"
    assert adapter.start_calls == 1

    recovered = await service.execute(request, binding)

    assert recovered.seeding is True
    assert recovered.replayed is True
    assert recovered.recovered_after_unknown_result is True
    assert adapter.start_calls == 1


@pytest.mark.asyncio
async def test_transmission_start_requires_matching_verify_journal_before_write(
    operation_fixture: tuple[sessionmaker[Session], str, _FakeTransmission, _Binding, bytes],
) -> None:
    factory, task_id, adapter, binding, torrent = operation_fixture
    add_journal_id, verified = await _verified_chain(factory, task_id, adapter, binding, torrent)
    request = replace(
        _start_request(task_id, add_journal_id, verified),
        verification_journal_id=add_journal_id,
    )

    with pytest.raises(ApplicationError) as failure:
        await TransmissionStartOperationService(factory).execute(request, binding)

    assert failure.value.code == "DOWNLOADER_START_AUTHORIZATION_INVALID"
    assert adapter.start_calls == 0
