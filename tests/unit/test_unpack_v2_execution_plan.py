import asyncio
import hashlib
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.downloaders import (
    QbittorrentWriteBinding,
    TransmissionWriteBinding,
)
from backend.app.application.sites import EnabledSiteAdapter
from backend.app.application.unpack_execution_plans import UnpackExecutionPlanService
from backend.app.domain.downloader import PathMappingRule
from backend.app.domain.execution_plan import ExecutionPlanBlockReason
from backend.app.domain.site_adapter import SiteAdapter, TorrentPayload
from backend.app.domain.task_definition import TaskStorageMode
from backend.app.domain.unpack import (
    UnpackCandidateVerificationStatus,
    UnpackExecutionStatus,
    UnpackItemStatus,
)
from backend.app.domain.unpack_execution_plan import (
    UnpackExecutionAction,
    UnpackExecutionActionKind,
    unpack_execution_plan_from_payload,
)
from backend.app.domain.verification import FileSnapshot, VerificationLevel
from backend.app.infrastructure.adapters.downloaders import (
    QbittorrentWriteAdapter,
    TransmissionWriteAdapter,
)
from backend.app.infrastructure.authorized_paths import AuthorizedPathScope
from backend.app.infrastructure.persistence.base import Base
from backend.app.infrastructure.persistence.models import (
    UnpackDefinition,
    UnpackExecution,
    UnpackExecutionItem,
    UnpackMatchCandidate,
    utc_now,
)
from backend.app.infrastructure.source_inventory import current_file_snapshot
from backend.app.infrastructure.torrent_parser import parse_torrent


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


def _torrent(name: str, content: bytes) -> bytes:
    piece_length = 4
    pieces = b"".join(
        hashlib.sha1(content[offset : offset + piece_length]).digest()
        for offset in range(0, len(content), piece_length)
    )
    return _bencode(
        {
            b"info": {
                b"length": len(content),
                b"name": name.encode(),
                b"piece length": piece_length,
                b"pieces": pieces,
            }
        }
    )


class _FakeSite:
    def __init__(self, torrent: bytes) -> None:
        self._torrent = torrent
        self.fetch_calls: list[str] = []

    async def fetch_torrent(self, torrent_id: str) -> TorrentPayload:
        self.fetch_calls.append(torrent_id)
        return TorrentPayload("fake-site", torrent_id, self._torrent)


class _SiteProvider:
    def __init__(self, site: _FakeSite) -> None:
        self._site = site

    def enabled_adapters(self) -> tuple[EnabledSiteAdapter, ...]:
        return (
            EnabledSiteAdapter(
                config_id="site-config-1",
                config_version=7,
                site_id="fake-site",
                adapter=cast(SiteAdapter, self._site),
            ),
        )


class _DownloaderProvider:
    def __init__(
        self,
        binding: QbittorrentWriteBinding | TransmissionWriteBinding,
    ) -> None:
        self._binding = binding

    def write_binding(
        self,
        downloader_id: str,
    ) -> QbittorrentWriteBinding | TransmissionWriteBinding:
        assert downloader_id == "downloader-target"
        return self._binding


def _snapshot_payload(snapshot: FileSnapshot) -> dict[str, object]:
    return {
        "device": snapshot.device,
        "inode": snapshot.inode,
        "size": snapshot.size,
        "mtime_ns": str(snapshot.mtime_ns),
        "file_type": snapshot.file_type,
    }


def _fixture(
    tmp_path: Path,
    *,
    downloader_kind: str = "qb",
) -> tuple[
    UnpackExecutionPlanService,
    sessionmaker[Session],
    Path,
    Path,
    _FakeSite,
]:
    data_root = tmp_path / "data"
    source_root = data_root / "movies"
    output = data_root / "seeding"
    source_root.mkdir(parents=True)
    output.mkdir()
    content = b"abcdefgh"
    source = source_root / "Movie.mkv"
    source.write_bytes(content)
    source_snapshot = current_file_snapshot(source)
    torrent = _torrent("Movie.mkv", content)
    meta = parse_torrent(torrent)

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory: sessionmaker[Session] = sessionmaker(
        bind=engine,
        expire_on_commit=False,
        autoflush=False,
    )
    now = utc_now()
    with factory() as session:
        session.add(
            UnpackDefinition(
                id="definition-1",
                name="执行计划测试",
                trigger_kind="MANUAL",
                status="PENDING_EXECUTION",
                source_kind="DIRECTORY",
                execution_scope_kind="ALL_MATCHING_MEDIA",
                source_config={"directory_path": source_root.as_posix()},
                file_filter={"extensions": [".mkv"]},
                site_ids=["site-config-1"],
                output_config={
                    "output_directory": output.as_posix(),
                    "storage_mode": "HARDLINK",
                    "conflict_policy": "VERIFY_REUSE_OR_STOP",
                    "target_downloader_id": "downloader-target",
                },
                retry_enabled=True,
                max_retries=3,
                auto_match_threshold_bps=10000,
                version=1,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            UnpackExecution(
                id="execution-1",
                definition_id="definition-1",
                trigger="MANUAL",
                status=UnpackExecutionStatus.CONTENT_VERIFYING.value,
                config_snapshot={
                    "output_config": {
                        "output_directory": output.as_posix(),
                        "storage_mode": "HARDLINK",
                        "conflict_policy": "VERIFY_REUSE_OR_STOP",
                        "target_downloader_id": "downloader-target",
                    }
                },
                discovery_complete=True,
                total_count=1,
                matched_auto_count=1,
                review_count=0,
                content_verified_count=1,
                content_mismatch_count=0,
                timeout_count=0,
                error_count=0,
                completed_count=0,
                started_at=now,
                version=1,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            UnpackExecutionItem(
                id="item-1",
                execution_id="execution-1",
                source_object_key="source-1",
                source_snapshot={
                    "path": source.as_posix(),
                    "relative_path": source.name,
                    **_snapshot_payload(source_snapshot),
                },
                media_identity={},
                status=UnpackItemStatus.CONTENT_VERIFIED.value,
                selected_candidate_id="candidate-1",
                candidate_generation=1,
                retry_count=0,
                content_verification_level=VerificationLevel.FULL_VERIFIED.value,
                torrent_metainfo_digest=meta.metainfo_digest,
                version=4,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            UnpackMatchCandidate(
                id="candidate-1",
                item_id="item-1",
                generation=1,
                site_id="site-config-1",
                candidate_key="torrent-1",
                title="Movie",
                size_bytes=len(content),
                score_bps=10000,
                is_exact_match=True,
                evidence={
                    "hard_conflicts": [],
                    "content_verification": {
                        "level": VerificationLevel.FULL_VERIFIED.value,
                        "piece_mismatch": False,
                        "mappings": [
                            {
                                "torrent_path": "Movie.mkv",
                                "state": "MAPPED",
                                "method": "EXACT_PATH",
                                "source_path": source.as_posix(),
                                "snapshot": _snapshot_payload(source_snapshot),
                            }
                        ],
                    },
                },
                verification_status=UnpackCandidateVerificationStatus.VERIFIED.value,
                verification_level=VerificationLevel.FULL_VERIFIED.value,
                metainfo_digest=meta.metainfo_digest,
                raw_ref={
                    "torrent_id": "torrent-1",
                    "adapter_site_id": "fake-site",
                    "site_config_id": "site-config-1",
                    "site_config_version": 7,
                },
                created_at=now,
            )
        )
        session.commit()

    if downloader_kind == "qb":
        binding: QbittorrentWriteBinding | TransmissionWriteBinding = QbittorrentWriteBinding(
            downloader_id="downloader-target",
            downloader_version=3,
            binding_digest="b" * 64,
            path_mappings=(PathMappingRule("/downloads", data_root.as_posix()),),
            capabilities={"supports_skip_checking": True, "api_version": "2.11.0"},
            adapter=cast(QbittorrentWriteAdapter, object()),
            data_root=data_root,
        )
    else:
        binding = TransmissionWriteBinding(
            downloader_id="downloader-target",
            downloader_version=5,
            binding_digest="c" * 64,
            path_mappings=(PathMappingRule("/downloads", data_root.as_posix()),),
            capabilities={"api_version": "4.1.0"},
            adapter=cast(TransmissionWriteAdapter, object()),
            data_root=data_root,
        )
    site = _FakeSite(torrent)
    service = UnpackExecutionPlanService(
        factory,
        _SiteProvider(site),
        _DownloaderProvider(binding),
        path_scope=AuthorizedPathScope.legacy_only(legacy_data_root=data_root),
    )
    return service, factory, source, output, site


def test_planner_freezes_verified_mapping_and_qb_hardlink_can_skip_client_check(
    tmp_path: Path,
) -> None:
    service, factory, source, output, site = _fixture(tmp_path)

    report = asyncio.run(service.plan_next_batch("execution-1"))

    assert report.ready_count == 1
    assert report.blocked_count == 0
    assert site.fetch_calls == ["torrent-1"]
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == UnpackItemStatus.PLAN_PENDING.value
        assert item.execution_plan is not None
        plan = unpack_execution_plan_from_payload(item.execution_plan)
        assert plan.plan_digest == item.execution_plan_digest
        assert plan.ready is True
        assert plan.item_version_before == 4
        assert plan.output_directory == output.as_posix()
        assert plan.target_remote_save_path == "/downloads/seeding"
        assert plan.client_check_required is False
        assert len(plan.actions) == 1
        action = plan.actions[0]
        assert action.kind is UnpackExecutionActionKind.MATERIALIZE
        assert action.source_path == source.as_posix()
        assert action.source_snapshot == current_file_snapshot(source)


def test_target_exists_is_frozen_as_blocked_plan_without_writing_target(
    tmp_path: Path,
) -> None:
    service, factory, _source, output, _site = _fixture(tmp_path)
    existing = output / "Movie.mkv"
    existing.write_bytes(b"existing")

    report = asyncio.run(service.plan_next_batch("execution-1"))

    assert report.ready_count == 0
    assert report.blocked_count == 1
    assert existing.read_bytes() == b"existing"
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        plan = unpack_execution_plan_from_payload(item.execution_plan)
        assert ExecutionPlanBlockReason.TARGET_EXISTS in plan.blocked_reasons
        assert item.last_error_code == "UNPACK_EXECUTION_PLAN_BLOCKED"


@pytest.mark.parametrize("same_inode", [True, False])
def test_existing_identical_target_freezes_safe_reuse_proof(
    tmp_path: Path, same_inode: bool
) -> None:
    service, factory, source, output, _site = _fixture(tmp_path)
    target = output / "Movie.mkv"
    if same_inode:
        os.link(source, target)
    else:
        target.write_bytes(source.read_bytes())
    before = current_file_snapshot(target)

    report = asyncio.run(service.plan_next_batch("execution-1"))

    assert report.ready_count == 1
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == UnpackItemStatus.PLAN_PENDING.value
        plan = unpack_execution_plan_from_payload(item.execution_plan)
        assert plan.ready
        assert plan.actions[0].reuse_target_snapshot == before
        if same_inode:
            assert plan.actions[0].reuse_sha256 is None
        else:
            assert plan.actions[0].reuse_sha256 == hashlib.sha256(source.read_bytes()).hexdigest()
    assert current_file_snapshot(target) == before


def test_existing_symlink_target_does_not_pass_reuse_gate(tmp_path: Path) -> None:
    service, factory, source, output, _site = _fixture(tmp_path)
    (output / "Movie.mkv").symlink_to(source)
    report = asyncio.run(service.plan_next_batch("execution-1"))
    assert report.blocked_count == 1
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        plan = unpack_execution_plan_from_payload(item.execution_plan)
        assert ExecutionPlanBlockReason.TARGET_EXISTS in plan.blocked_reasons


def test_transmission_plan_always_requires_client_verification(tmp_path: Path) -> None:
    service, factory, _source, _output, _site = _fixture(
        tmp_path,
        downloader_kind="transmission",
    )

    report = asyncio.run(service.plan_next_batch("execution-1"))

    assert report.ready_count == 1
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        plan = unpack_execution_plan_from_payload(item.execution_plan)
        assert plan.client_check_required is True
        assert plan.target_downloader_version == 5


def test_hardlink_cross_device_is_blocked_but_symlink_layout_is_not(tmp_path: Path) -> None:
    service, _factory, source, output, _site = _fixture(tmp_path)
    current = current_file_snapshot(source)
    foreign = FileSnapshot(
        device=current.device + 1,
        inode=current.inode,
        size=current.size,
        mtime_ns=current.mtime_ns,
        file_type=current.file_type,
    )
    action = UnpackExecutionAction(
        torrent_path="Movie.mkv",
        kind=UnpackExecutionActionKind.MATERIALIZE,
        length=current.size,
        source_path=source.as_posix(),
        source_snapshot=foreign,
    )

    hardlink = service._inspect_target_layout(
        output,
        (action,),
        storage_mode=TaskStorageMode.HARDLINK,
    )
    symlink = service._inspect_target_layout(
        output,
        (action,),
        storage_mode=TaskStorageMode.SYMLINK,
    )

    assert ExecutionPlanBlockReason.CROSS_DEVICE in hardlink.blocked_reasons
    assert ExecutionPlanBlockReason.CROSS_DEVICE not in symlink.blocked_reasons
