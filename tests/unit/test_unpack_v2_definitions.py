import asyncio
import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.errors import ApplicationError
from backend.app.application.unpack_definitions import (
    UnpackDefinitionCreate,
    UnpackDefinitionService,
    UnpackSelectedSourceCreate,
)
from backend.app.application.unpack_manual_candidates import (
    UnpackManualCandidateService,
    UnpackManualSiteProvider,
)
from backend.app.application.unpack_manual_torrents import (
    ManualTorrentStore,
    ManualTorrentStoreError,
)
from backend.app.domain.media_matching import parse_media_name
from backend.app.domain.operation import OperationStatus
from backend.app.domain.site_adapter import TorrentDetails, TorrentPayload
from backend.app.domain.site_search import CandidateMeta
from backend.app.domain.unpack import (
    UnpackDefinitionStatus,
    UnpackExecutionScopeKind,
    UnpackExecutionStatus,
    UnpackItemStatus,
    UnpackSourceKind,
    UnpackTriggerKind,
)
from backend.app.infrastructure.persistence.base import Base
from backend.app.infrastructure.persistence.models import (
    Downloader,
    Site,
    UnpackDefinitionSelectedSource,
    UnpackDeletedItemAudit,
    UnpackExecution,
    UnpackExecutionItem,
    UnpackExternalOperationJournal,
    UnpackMatchCandidate,
    utc_now,
)
from backend.app.infrastructure.torrent_parser import parse_torrent


def _service(tmp_path: Path) -> tuple[UnpackDefinitionService, sessionmaker[Session], Path]:
    data_root = tmp_path / "data"
    source = data_root / "movies"
    output = data_root / "seeding"
    source.mkdir(parents=True)
    output.mkdir(parents=True)

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory: sessionmaker[Session] = sessionmaker(
        bind=engine,
        expire_on_commit=False,
    )
    with factory() as session:
        session.add_all(
            [
                Site(
                    id="site-mteam",
                    name="M-TEAM",
                    type="MTEAM",
                    base_url="https://kp.m-team.cc",
                    credential_kind="API_KEY",
                    capabilities={},
                    connection_status="OK",
                    enabled=True,
                    version=1,
                ),
                Downloader(
                    id="downloader-target",
                    name="目标 qB",
                    type="QBITTORRENT",
                    base_url="http://qb.test",
                    monitor_rules={},
                    path_mappings=[],
                    capabilities={"supports_selective_files": True},
                    connection_status="OK",
                    path_mapping_status="OK",
                    enabled=True,
                    version=1,
                ),
            ]
        )
        session.commit()

    return (
        UnpackDefinitionService(
            factory,
            data_root=data_root,
            timezone="Asia/Shanghai",
        ),
        factory,
        source,
    )


def _request(
    source: Path,
    *,
    scope: UnpackExecutionScopeKind = UnpackExecutionScopeKind.ALL_MATCHING_MEDIA,
    selected_sources: tuple[UnpackSelectedSourceCreate, ...] = (),
    auto_match_enabled: bool = True,
    target_category: str | None = None,
    target_tags: tuple[str, ...] = (),
    skip_downloader_check: bool = False,
) -> UnpackDefinitionCreate:
    return UnpackDefinitionCreate(
        name="电影库手动拆包",
        trigger_kind=UnpackTriggerKind.MANUAL,
        source_kind=UnpackSourceKind.DIRECTORY,
        execution_scope_kind=scope,
        source_config={"directory_path": source.as_posix()},
        file_filter={"extensions": ["MKV", ".mp4", ".MKV"]},
        site_ids=("site-mteam",),
        output_config={
            "output_directory": (source.parent / "seeding").as_posix(),
            "storage_mode": "HARDLINK",
            "conflict_policy": "VERIFY_REUSE_OR_STOP",
            "target_downloader_id": "downloader-target",
            "target_category": target_category,
            "target_tags": list(target_tags),
            "skip_downloader_check": skip_downloader_check,
        },
        auto_match_threshold_bps=9680,
        auto_match_enabled=auto_match_enabled,
        selected_sources=selected_sources,
    )


def test_create_manual_definition_is_pending_and_does_not_start_execution(
    tmp_path: Path,
) -> None:
    service, factory, source = _service(tmp_path)

    created = service.create(_request(source))

    assert created.status is UnpackDefinitionStatus.PENDING_EXECUTION
    assert created.file_filter["extensions"] == [".mkv", ".mp4"]
    assert created.auto_match_threshold_bps == 9680
    assert created.auto_match_enabled is True
    assert created.output_config["target_category"] is None
    assert created.output_config["target_tags"] == []
    assert created.output_config["skip_downloader_check"] is False
    with factory() as session:
        assert session.scalar(select(func.count(UnpackExecution.id))) == 0


def test_skip_downloader_check_setting_is_frozen_into_execution(tmp_path: Path) -> None:
    service, factory, source = _service(tmp_path)
    created = service.create(_request(source, skip_downloader_check=True))

    assert created.output_config["skip_downloader_check"] is True
    result = service.run(created.id)
    assert result.execution_id is not None
    with factory() as session:
        execution = session.get(UnpackExecution, result.execution_id)
        assert execution is not None
        assert execution.config_snapshot["output_config"]["skip_downloader_check"] is True


def test_edit_definition_keeps_id_and_rejects_stale_version(tmp_path: Path) -> None:
    service, _factory, source = _service(tmp_path)
    created = service.create(_request(source))
    updated = service.update(
        created.id,
        replace(_request(source), name="改名后手动拆包"),
        expected_version=created.version,
    )
    assert updated.id == created.id
    assert updated.name == "改名后手动拆包"
    assert updated.version == created.version + 1
    with pytest.raises(ApplicationError) as failure:
        service.update(created.id, _request(source), expected_version=created.version)
    assert failure.value.code == "UNPACK_DEFINITION_CONFLICT"


def test_delete_unrun_definition_and_preserve_pending_execution(tmp_path: Path) -> None:
    service, factory, source = _service(tmp_path)
    created = service.create(_request(source))
    with pytest.raises(ApplicationError):
        service.delete(created.id, expected_version=created.version + 1)
    service.delete(created.id, expected_version=created.version)
    with pytest.raises(ApplicationError) as missing:
        service.get(created.id)
    assert missing.value.status == 404

    created = service.create(_request(source))
    run = service.run(created.id)
    assert run.execution_id
    _set_execution_items(factory, run.execution_id, (UnpackItemStatus.MATCH_PENDING.value,))
    with pytest.raises(ApplicationError) as running:
        service.delete(created.id, expected_version=created.version)
    assert running.value.code == "UNPACK_DEFINITION_CONFLICT"


def _set_execution_items(
    factory: sessionmaker[Session], execution_id: str, statuses: tuple[str, ...]
) -> tuple[str, ...]:
    item_ids: list[str] = []
    with factory() as session:
        execution = session.get(UnpackExecution, execution_id)
        assert execution is not None
        execution.status = UnpackExecutionStatus.REVIEW_REQUIRED.value
        execution.total_count = len(statuses)
        execution.review_count = sum(
            status == UnpackItemStatus.REVIEW_REQUIRED.value for status in statuses
        )
        for index, status in enumerate(statuses):
            filename = f"Movie.{index + 1}.mkv"
            item = UnpackExecutionItem(
                execution_id=execution_id,
                source_object_key=f"source-{index}",
                source_snapshot={
                    "path": f"/movies/{filename}",
                    "relative_path": filename,
                    "size": 100,
                },
                media_identity={},
                status=status,
                candidate_generation=0,
                retry_count=0,
                version=1,
            )
            session.add(item)
            session.flush()
            item_ids.append(item.id)
        session.commit()
    return tuple(item_ids)


def test_delete_review_only_execution_when_all_items_are_reviewable(tmp_path: Path) -> None:
    service, factory, source = _service(tmp_path)
    created = service.create(_request(source))
    run = service.run(created.id)
    assert run.execution_id is not None
    _set_execution_items(
        factory,
        run.execution_id,
        (UnpackItemStatus.NO_MATCH.value, UnpackItemStatus.REVIEW_REQUIRED.value),
    )

    current = service.get(created.id)
    service.delete(created.id, expected_version=current.version)

    with pytest.raises(ApplicationError) as missing:
        service.get(created.id)
    assert missing.value.status == 404


@pytest.mark.parametrize(
    "active_status",
    (
        UnpackItemStatus.MATCH_PENDING.value,
        UnpackItemStatus.MATCHING.value,
    ),
)
def test_delete_review_execution_rejects_pending_or_matching_items(
    tmp_path: Path, active_status: str
) -> None:
    service, factory, source = _service(tmp_path)
    created = service.create(_request(source))
    run = service.run(created.id)
    assert run.execution_id is not None
    _set_execution_items(
        factory,
        run.execution_id,
        (UnpackItemStatus.NO_MATCH.value, active_status),
    )

    current = service.get(created.id)
    with pytest.raises(ApplicationError) as failure:
        service.delete(created.id, expected_version=current.version)

    assert failure.value.code == "UNPACK_DEFINITION_CONFLICT"
    assert service.get(created.id).id == created.id


def _manual_bencode(value: object) -> bytes:
    if isinstance(value, int):
        return f"i{value}e".encode()
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        return b"l" + b"".join(_manual_bencode(item) for item in value) + b"e"
    if isinstance(value, Mapping):
        items = sorted(value.items(), key=lambda item: item[0])
        return (
            b"d"
            + b"".join(_manual_bencode(key) + _manual_bencode(item) for key, item in items)
            + b"e"
        )
    raise TypeError(type(value).__name__)


def _manual_torrent_payload(
    announce: bytes = b"https://tracker.invalid/acceptance-passkey",
) -> bytes:
    media = b"small movie content"
    piece_length = 4
    pieces = b"".join(
        hashlib.sha1(media[offset : offset + piece_length]).digest()
        for offset in range(0, len(media), piece_length)
    )
    info = {
        b"length": len(media),
        b"name": b"Movie.2024.1080p.mkv",
        b"piece length": piece_length,
        b"pieces": pieces,
        b"private": 1,
    }
    return _manual_bencode({b"announce": announce, b"info": info})


class _ManualCandidateAdapter:
    def __init__(self, torrent_content: bytes) -> None:
        self.torrent_content = torrent_content

    async def fetch_details(self, torrent_id: str) -> TorrentDetails:
        descriptor = parse_media_name(
            "Movie.2024.1080p.BluRay-MNHD",
            total_size=len(b"small movie content"),
        )
        return TorrentDetails(
            CandidateMeta(
                site_id="mteam",
                torrent_id=torrent_id,
                display_name="Movie.2024.1080p.BluRay-MNHD",
                descriptor=descriptor,
                total_size=len(b"small movie content"),
                seeders=7,
            )
        )

    async def fetch_torrent(self, torrent_id: str) -> TorrentPayload:
        return TorrentPayload("mteam", torrent_id, self.torrent_content)


class _ManualCandidateSiteProvider:
    def __init__(self, adapter: _ManualCandidateAdapter) -> None:
        self.adapter = adapter

    def get(self, site_id: str) -> SimpleNamespace:
        assert site_id == "site-mteam"
        return SimpleNamespace(
            id=site_id,
            name="M-TEAM",
            base_url="https://mteam.example",
            enabled=True,
        )

    def enabled_adapters(self) -> tuple[SimpleNamespace, ...]:
        return (
            SimpleNamespace(
                config_id="site-mteam",
                config_version=1,
                site_id="mteam",
                adapter=self.adapter,
            ),
        )


def test_manual_torrent_preview_is_read_only_then_confirm_persists_candidate(
    tmp_path: Path,
) -> None:
    definition_service, factory, source = _service(tmp_path)
    created = definition_service.create(_request(source))
    run = definition_service.run(created.id)
    assert run.execution_id is not None
    item_id = _set_execution_items(
        factory, run.execution_id, (UnpackItemStatus.REVIEW_REQUIRED.value,)
    )[0]
    torrent_content = _manual_torrent_payload()
    store = ManualTorrentStore(source.parent)
    manual_service = UnpackManualCandidateService(
        factory,
        cast(
            UnpackManualSiteProvider,
            _ManualCandidateSiteProvider(_ManualCandidateAdapter(torrent_content)),
        ),
        store,
    )

    preview = asyncio.run(
        manual_service.preview(
            item_id,
            site_id="site-mteam",
            expected_generation=0,
            expected_item_version=1,
            source_url=None,
            torrent_content=torrent_content,
        )
    )

    assert preview.source_kind == "FILE"
    assert preview.candidate_key.startswith("manual-")
    assert preview.file_count == 1
    assert not (source.parent / "unpack-manual-torrents").exists()

    added = asyncio.run(
        manual_service.add(
            item_id,
            site_id="site-mteam",
            expected_generation=0,
            expected_item_version=1,
            expected_metainfo_digest=preview.metainfo_digest,
            expected_payload_sha256=preview.payload_sha256,
            source_url=None,
            torrent_content=torrent_content,
        )
    )

    storage_key = hashlib.sha256(torrent_content).hexdigest()
    assert store.load(storage_key) == torrent_content
    with factory() as session:
        candidate = session.get(UnpackMatchCandidate, added.candidate_id)
        item = session.get(UnpackExecutionItem, item_id)
        assert candidate is not None
        assert item is not None
        assert candidate.raw_ref["manual_torrent_storage_key"] == storage_key
        assert candidate.metainfo_digest == preview.metainfo_digest
        assert item.version == 2


def test_manual_torrent_confirmation_rejects_payload_changed_after_preview(
    tmp_path: Path,
) -> None:
    definition_service, factory, source = _service(tmp_path)
    created = definition_service.create(_request(source))
    run = definition_service.run(created.id)
    assert run.execution_id is not None
    item_id = _set_execution_items(
        factory, run.execution_id, (UnpackItemStatus.REVIEW_REQUIRED.value,)
    )[0]
    original = _manual_torrent_payload()
    changed = _manual_torrent_payload(b"https://tracker.invalid/different-passkey")
    assert parse_torrent(original).metainfo_digest == parse_torrent(changed).metainfo_digest
    store = ManualTorrentStore(source.parent)
    manual_service = UnpackManualCandidateService(
        factory,
        cast(
            UnpackManualSiteProvider,
            _ManualCandidateSiteProvider(_ManualCandidateAdapter(original)),
        ),
        store,
    )
    preview = asyncio.run(
        manual_service.preview(
            item_id,
            site_id="site-mteam",
            expected_generation=0,
            expected_item_version=1,
            source_url=None,
            torrent_content=original,
        )
    )

    with pytest.raises(ApplicationError) as failure:
        asyncio.run(
            manual_service.add(
                item_id,
                site_id="site-mteam",
                expected_generation=0,
                expected_item_version=1,
                expected_metainfo_digest=preview.metainfo_digest,
                expected_payload_sha256=preview.payload_sha256,
                source_url=None,
                torrent_content=changed,
            )
        )

    assert failure.value.status == 409
    assert not (source.parent / "unpack-manual-torrents").exists()


def test_manual_torrent_url_must_belong_to_selected_site(tmp_path: Path) -> None:
    definition_service, factory, source = _service(tmp_path)
    created = definition_service.create(_request(source))
    run = definition_service.run(created.id)
    assert run.execution_id is not None
    item_id = _set_execution_items(
        factory, run.execution_id, (UnpackItemStatus.REVIEW_REQUIRED.value,)
    )[0]
    torrent_content = _manual_torrent_payload()
    manual_service = UnpackManualCandidateService(
        factory,
        cast(
            UnpackManualSiteProvider,
            _ManualCandidateSiteProvider(_ManualCandidateAdapter(torrent_content)),
        ),
        ManualTorrentStore(source.parent),
    )

    preview = asyncio.run(
        manual_service.preview(
            item_id,
            site_id="site-mteam",
            expected_generation=0,
            expected_item_version=1,
            source_url="https://mteam.example/details.php?id=12345&hit=1",
            torrent_content=None,
        )
    )
    assert preview.source_kind == "URL"
    assert preview.candidate_key == "12345"

    with pytest.raises(ApplicationError) as failure:
        asyncio.run(
            manual_service.preview(
                item_id,
                site_id="site-mteam",
                expected_generation=0,
                expected_item_version=1,
                source_url="https://attacker.invalid/details.php?id=12345",
                torrent_content=None,
            )
        )
    assert failure.value.code == "UNPACK_MANUAL_CANDIDATE_URL_INVALID"


def test_manual_run_creates_execution_with_frozen_snapshot(tmp_path: Path) -> None:
    service, factory, source = _service(tmp_path)
    created = service.create(_request(source))

    result = service.run(created.id)

    assert result.execution_id is not None
    with factory() as session:
        execution = session.get(UnpackExecution, result.execution_id)
        assert execution is not None
        assert execution.status == "DISCOVERING"
        assert execution.config_snapshot["matching"]["auto_match_threshold_bps"] == 9680
        assert execution.config_snapshot["matching"]["auto_match_enabled"] is True
        assert execution.config_snapshot["execution_scope_kind"] == "ALL_MATCHING_MEDIA"


def test_auto_match_switch_and_target_metadata_are_persisted_and_frozen(
    tmp_path: Path,
) -> None:
    service, factory, source = _service(tmp_path)
    created = service.create(
        _request(
            source,
            auto_match_enabled=False,
            target_category="  Movie Library  ",
            target_tags=("4K", " seed", "4K"),
        )
    )

    assert created.auto_match_enabled is False
    assert created.output_config["target_category"] == "Movie Library"
    assert created.output_config["target_tags"] == ["4K", "seed"]
    result = service.run(created.id)
    assert result.execution_id is not None
    with factory() as session:
        execution = session.get(UnpackExecution, result.execution_id)
        assert execution is not None
        assert execution.config_snapshot["matching"]["auto_match_enabled"] is False
        assert execution.config_snapshot["output_config"]["target_category"] == "Movie Library"
        assert execution.config_snapshot["output_config"]["target_tags"] == ["4K", "seed"]


def test_selected_media_is_persisted_and_frozen_into_execution(tmp_path: Path) -> None:
    service, factory, source = _service(tmp_path)
    movie = source / "Movie.2026.2160p.mkv"
    movie.write_bytes(b"synthetic")
    selected = UnpackSelectedSourceCreate(
        source_object_key="movie-key",
        canonical_path_hint=movie.as_posix(),
        filename=movie.name,
        size_bytes_at_selection=movie.stat().st_size,
    )
    created = service.create(
        _request(
            source,
            scope=UnpackExecutionScopeKind.SELECTED_MEDIA,
            selected_sources=(selected,),
        )
    )

    assert created.selected_source_count == 1
    with factory() as session:
        persisted = session.scalar(
            select(UnpackDefinitionSelectedSource).where(
                UnpackDefinitionSelectedSource.definition_id == created.id
            )
        )
        assert persisted is not None
        assert persisted.source_object_key == "movie-key"

    result = service.run(created.id)
    with factory() as session:
        execution = session.get(UnpackExecution, result.execution_id)
        assert execution is not None
        selected_snapshot = execution.config_snapshot["selected_sources"]
        assert len(selected_snapshot) == 1
        assert selected_snapshot[0]["source_object_key"] == "movie-key"
        assert selected_snapshot[0]["canonical_path_hint"] == movie.as_posix()
        assert selected_snapshot[0]["filename"] == movie.name
        assert selected_snapshot[0]["size_bytes_at_selection"] == len(b"synthetic")
        assert selected_snapshot[0]["source_snapshot"]["size"] == len(b"synthetic")
        assert selected_snapshot[0]["source_snapshot"]["file_type"] == "regular"
        assert isinstance(selected_snapshot[0]["source_snapshot"]["mtime_ns"], str)


def test_selected_media_requires_at_least_one_selection(tmp_path: Path) -> None:
    service, _factory, source = _service(tmp_path)

    with pytest.raises(ApplicationError, match="至少需要勾选一个影视文件") as caught:
        service.create(
            _request(
                source,
                scope=UnpackExecutionScopeKind.SELECTED_MEDIA,
            )
        )

    assert caught.value.code == "UNPACK_DEFINITION_INVALID"


def test_monitor_save_is_pending_and_run_only_enables_schedule(tmp_path: Path) -> None:
    service, factory, source = _service(tmp_path)
    request = UnpackDefinitionCreate(
        name="目录持续监控",
        trigger_kind=UnpackTriggerKind.MONITOR,
        source_kind=UnpackSourceKind.DIRECTORY,
        execution_scope_kind=UnpackExecutionScopeKind.ALL_MATCHING_MEDIA,
        source_config={"directory_path": source.as_posix()},
        file_filter={"extensions": [".mkv"]},
        site_ids=("site-mteam",),
        output_config={
            "output_directory": (source.parent / "seeding").as_posix(),
            "storage_mode": "HARDLINK",
            "conflict_policy": "VERIFY_REUSE_OR_STOP",
            "target_downloader_id": "downloader-target",
        },
        cron_expression="*/10 * * * *",
    )

    created = service.create(request)
    assert created.status is UnpackDefinitionStatus.PENDING_EXECUTION
    result = service.run(created.id)

    assert result.execution_id is None
    assert result.definition.status is UnpackDefinitionStatus.ENABLED
    assert result.definition.next_run_at is not None
    assert result.definition.last_triggered_at is None
    with factory() as session:
        assert session.scalar(select(func.count(UnpackExecution.id))) == 0


def test_monitor_rejects_invalid_timezone(tmp_path: Path) -> None:
    service, _factory, source = _service(tmp_path)
    request = UnpackDefinitionCreate(
        name="无效时区监控",
        trigger_kind=UnpackTriggerKind.MONITOR,
        source_kind=UnpackSourceKind.DIRECTORY,
        execution_scope_kind=UnpackExecutionScopeKind.ALL_MATCHING_MEDIA,
        source_config={"directory_path": source.as_posix()},
        file_filter={"extensions": [".mkv"]},
        site_ids=("site-mteam",),
        output_config={
            "output_directory": (source.parent / "seeding").as_posix(),
            "storage_mode": "HARDLINK",
            "conflict_policy": "VERIFY_REUSE_OR_STOP",
            "target_downloader_id": "downloader-target",
        },
        cron_expression="*/10 * * * *",
        timezone="Mars/Olympus_Mons",
    )

    with pytest.raises(ApplicationError, match="时区或 Cron 无效"):
        service.create(request)


def test_manual_directory_rejects_cron(tmp_path: Path) -> None:
    service, _factory, source = _service(tmp_path)
    request = _request(source)
    invalid = replace(request, cron_expression="*/10 * * * *")

    with pytest.raises(ApplicationError, match="不能配置 Cron"):
        service.create(invalid)


def test_manual_directory_requires_explicit_target_downloader(tmp_path: Path) -> None:
    service, _factory, source = _service(tmp_path)
    request = _request(source)
    invalid = replace(
        request,
        output_config={
            key: value
            for key, value in request.output_config.items()
            if key != "target_downloader_id"
        },
    )

    with pytest.raises(ApplicationError, match="必须选择目标下载器"):
        service.create(invalid)


def test_skip_downloader_check_rejects_non_boolean_values(tmp_path: Path) -> None:
    service, _factory, source = _service(tmp_path)
    request = _request(source)
    invalid = replace(
        request,
        output_config={**request.output_config, "skip_downloader_check": "true"},
    )

    with pytest.raises(ApplicationError, match="跳过下载器检查必须是布尔值"):
        service.create(invalid)


@pytest.mark.parametrize(
    "allowed_status",
    (
        UnpackItemStatus.DISCOVERED.value,
        UnpackItemStatus.MATCHED_AUTO.value,
        UnpackItemStatus.MATCHED_MANUAL.value,
        UnpackItemStatus.REVIEW_REQUIRED.value,
        UnpackItemStatus.NO_MATCH.value,
        UnpackItemStatus.EXECUTING.value,
        UnpackItemStatus.EXECUTION_ERROR.value,
        UnpackItemStatus.COMPLETED.value,
        UnpackItemStatus.CANCELLED.value,
    ),
)
def test_delete_definition_when_no_items_pending_or_matching(
    tmp_path: Path, allowed_status: str
) -> None:
    service, factory, source = _service(tmp_path)
    created = service.create(_request(source))
    run = service.run(created.id)
    assert run.execution_id
    _set_execution_items(factory, run.execution_id, (allowed_status,))
    current = service.get(created.id)
    service.delete(created.id, expected_version=current.version)
    with pytest.raises(ApplicationError) as missing:
        service.get(created.id)
    assert missing.value.status == 404


def test_definition_deletion_cleans_unreferenced_verified_torrent_payload(tmp_path: Path) -> None:
    service, factory, source = _service(tmp_path)
    created = service.create(_request(source))
    run = service.run(created.id)
    assert run.execution_id
    (item_id,) = _set_execution_items(
        factory, run.execution_id, (UnpackItemStatus.REVIEW_REQUIRED.value,)
    )
    store = ManualTorrentStore(source.parent)
    torrent_content = _manual_torrent_payload()
    storage_key = store.store(torrent_content)
    with factory() as session:
        session.add(
            UnpackMatchCandidate(
                id="cached-site-candidate",
                item_id=item_id,
                generation=0,
                site_id="site-mteam",
                candidate_key="12345",
                title="Verified cached torrent",
                size_bytes=100,
                score_bps=10_000,
                is_exact_match=True,
                evidence={},
                raw_ref={"verified_torrent_storage_key": storage_key},
                created_at=utc_now(),
            )
        )
        session.commit()

    current = service.get(created.id)
    service.delete(created.id, expected_version=current.version)

    with pytest.raises(ManualTorrentStoreError):
        store.load(storage_key)


def test_definition_deletion_preserves_real_operation_journal_as_independent_audit(
    tmp_path: Path,
) -> None:
    service, factory, source = _service(tmp_path)
    created = service.create(_request(source))
    run = service.run(created.id)
    assert run.execution_id
    (item_id,) = _set_execution_items(
        factory, run.execution_id, (UnpackItemStatus.EXECUTION_ERROR.value,)
    )
    now = utc_now()
    with factory() as session:
        session.add(
            UnpackExternalOperationJournal(
                id="materialized-journal",
                item_id=item_id,
                idempotency_key="e" * 64,
                operation_type="UNPACK_EXEC_MATERIALIZE_HARDLINK",
                target={"path": "/data/isolated/media.mkv"},
                intent={"purpose": "test"},
                before_snapshot={"inode": 20},
                after_snapshot={"inode": 25},
                status=OperationStatus.APPLIED.value,
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()
    current = service.get(created.id)
    service.delete(created.id, expected_version=current.version)
    with factory() as session:
        assert session.get(UnpackExecutionItem, item_id) is None
        archive = session.scalar(
            select(UnpackDeletedItemAudit).where(UnpackDeletedItemAudit.item_id == item_id)
        )
        assert archive is not None
        assert archive.definition_id == created.id
        assert archive.payload["operations"][0]["after_snapshot"]["inode"] == 25
        assert archive.payload["operations"][0]["idempotency_key"] == "e" * 64
        assert session.get(UnpackExternalOperationJournal, "materialized-journal") is None
        assert session.get(UnpackExecution, run.execution_id) is None
