import asyncio
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.unpack_definitions import (
    UnpackDefinitionCreate,
    UnpackDefinitionService,
)
from backend.app.application.unpack_discovery import UnpackDiscoveryService
from backend.app.application.unpack_monitor import UnpackMonitorService
from backend.app.domain.unpack import (
    UnpackExecutionScopeKind,
    UnpackExecutionStatus,
    UnpackItemStatus,
    UnpackSourceKind,
    UnpackTriggerKind,
)
from backend.app.infrastructure.adapters.downloaders import DownloaderTorrent
from backend.app.infrastructure.authorized_paths import AuthorizedPathScope
from backend.app.infrastructure.persistence.base import Base
from backend.app.infrastructure.persistence.models import (
    Downloader,
    Site,
    UnpackDefinition,
    UnpackExecution,
    UnpackExecutionItem,
    utc_now,
)


class _FakeBinding:
    def __init__(self, mappings: dict[str, Path], *, fail: bool = False) -> None:
        self._mappings = mappings
        self._fail = fail

    def container_path(self, remote_path: str) -> Path:
        if self._fail:
            raise ValueError("synthetic mapping failure")
        return self._mappings[remote_path]


class _FakeDownloaderService:
    def __init__(
        self,
        torrents: tuple[DownloaderTorrent, ...],
        binding: _FakeBinding,
    ) -> None:
        self._torrents = torrents
        self._binding = binding

    async def list_all_torrents(self, downloader_id: str) -> tuple[DownloaderTorrent, ...]:
        assert downloader_id == "downloader-1"
        return self._torrents

    def write_binding(self, downloader_id: str) -> _FakeBinding:
        assert downloader_id == "downloader-1"
        return self._binding


def _services(
    tmp_path: Path,
    fake_downloader: _FakeDownloaderService,
) -> tuple[
    UnpackDefinitionService,
    UnpackMonitorService,
    UnpackDiscoveryService,
    sessionmaker[Session],
    Path,
    Path,
]:
    data_root = tmp_path / "data"
    output = data_root / "seeding"
    output.mkdir(parents=True)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory: sessionmaker[Session] = sessionmaker(
        bind=engine,
        expire_on_commit=False,
        autoflush=False,
    )
    with factory() as session:
        session.add_all(
            [
                Site(
                    id="site-1",
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
                    id="downloader-1",
                    name="qB",
                    type="QBITTORRENT",
                    base_url="http://qb.test",
                    monitor_rules={},
                    path_mappings=[],
                    capabilities={},
                    connection_status="OK",
                    path_mapping_status="OK",
                    enabled=True,
                    version=1,
                ),
            ]
        )
        session.commit()
    path_scope = AuthorizedPathScope.legacy_only(legacy_data_root=data_root)
    return (
        UnpackDefinitionService(
            factory,
            data_root=data_root,
            timezone="Asia/Shanghai",
            path_scope=path_scope,
        ),
        UnpackMonitorService(factory, fake_downloader),
        UnpackDiscoveryService(factory, path_scope=path_scope),
        factory,
        data_root,
        output,
    )


def _monitor_request(output: Path) -> UnpackDefinitionCreate:
    return UnpackDefinitionCreate(
        name="下载器持续监控",
        trigger_kind=UnpackTriggerKind.MONITOR,
        source_kind=UnpackSourceKind.DOWNLOADER,
        execution_scope_kind=UnpackExecutionScopeKind.ALL_MATCHING_MEDIA,
        source_config={
            "downloader_id": "downloader-1",
            "name_contains": "movie",
            "categories": ["films"],
            "tags": ["keep"],
        },
        file_filter={"extensions": [".mkv"]},
        site_ids=("site-1",),
        output_config={
            "output_directory": output.as_posix(),
            "storage_mode": "HARDLINK",
            "conflict_policy": "VERIFY_REUSE_OR_STOP",
        },
        cron_expression="*/10 * * * *",
        timezone="Asia/Shanghai",
    )


def _make_due(
    factory: sessionmaker[Session],
    definition_id: str,
) -> datetime:
    now = utc_now()
    with factory() as session:
        definition = session.get(UnpackDefinition, definition_id)
        assert definition is not None
        definition.next_run_at = now - timedelta(seconds=1)
        session.commit()
    return now


def test_monitor_downloader_freezes_only_completed_matching_torrents_and_discovers_media(
    tmp_path: Path,
) -> None:
    torrent_root = tmp_path / "data" / "downloads" / "Movie.Release"
    torrent_root.mkdir(parents=True)
    (torrent_root / "Movie.Release.2160p.mkv").write_bytes(b"movie")
    (torrent_root / "Movie.Release.nfo").write_text("metadata", encoding="utf-8")

    matching = DownloaderTorrent(
        torrent_hash="hash-complete",
        name="Movie Release",
        status="uploading",
        progress=1.0,
        size_bytes=5,
        category="films",
        tags=("keep", "4k"),
        tracker="https://tracker.test/announce",
        save_path="/remote",
        content_path="/remote/Movie.Release",
    )
    incomplete = DownloaderTorrent(
        torrent_hash="hash-incomplete",
        name="Movie Incomplete",
        status="downloading",
        progress=0.5,
        size_bytes=5,
        category="films",
        tags=("keep",),
        tracker=None,
        save_path="/remote",
        content_path="/remote/Movie.Incomplete",
    )
    wrong_category = DownloaderTorrent(
        torrent_hash="hash-other",
        name="Movie Other",
        status="uploading",
        progress=1.0,
        size_bytes=5,
        category="tv",
        tags=("keep",),
        tracker=None,
        save_path="/remote",
        content_path="/remote/Movie.Other",
    )
    fake = _FakeDownloaderService(
        (matching, incomplete, wrong_category),
        _FakeBinding({"/remote/Movie.Release": torrent_root}),
    )
    definitions, monitor, discovery, factory, _root, output = _services(tmp_path, fake)

    created = definitions.create(_monitor_request(output))
    assert created.output_config["target_downloader_id"] == "downloader-1"
    enabled = definitions.run(created.id)
    assert enabled.definition.next_run_at is not None
    now = _make_due(factory, created.id)

    due = monitor.list_due_definition_ids(now=now, limit=10)
    assert due == (created.id,)
    triggered = asyncio.run(monitor.trigger_due_definition(created.id, now=now))
    assert triggered.execution_id is not None
    assert triggered.skipped_overlap is False

    with factory() as session:
        execution = session.get(UnpackExecution, triggered.execution_id)
        assert execution is not None
        frozen = execution.config_snapshot["downloader_sources"]
        assert [item["torrent_hash"] for item in frozen] == ["hash-complete"]

    report = discovery.discover_next_page(triggered.execution_id, limit=100)
    assert report.total_count == 1
    assert report.error_count == 0
    assert report.execution_status is UnpackExecutionStatus.MATCHING

    with factory() as session:
        item = session.scalar(
            select(UnpackExecutionItem).where(
                UnpackExecutionItem.execution_id == triggered.execution_id
            )
        )
        assert item is not None
        assert item.status == UnpackItemStatus.MATCH_PENDING.value
        assert item.source_snapshot["torrent_hash"] == "hash-complete"
        assert item.source_snapshot["path"].endswith("Movie.Release.2160p.mkv")


def test_monitor_downloader_mapping_failure_becomes_visible_error_item(tmp_path: Path) -> None:
    matching = DownloaderTorrent(
        torrent_hash="hash-bad-map",
        name="Movie Bad Mapping",
        status="uploading",
        progress=1.0,
        size_bytes=5,
        category="films",
        tags=("keep",),
        tracker=None,
        save_path="/remote",
        content_path="/remote/Movie.Bad",
    )
    fake = _FakeDownloaderService((matching,), _FakeBinding({}, fail=True))
    definitions, monitor, discovery, factory, _root, output = _services(tmp_path, fake)

    created = definitions.create(_monitor_request(output))
    definitions.run(created.id)
    now = _make_due(factory, created.id)
    triggered = asyncio.run(monitor.trigger_due_definition(created.id, now=now))
    assert triggered.execution_id is not None

    report = discovery.discover_next_page(triggered.execution_id, limit=100)

    assert report.total_count == 1
    assert report.error_count == 1
    assert report.execution_status is UnpackExecutionStatus.COMPLETED_WITH_ERRORS
    with factory() as session:
        item = session.scalar(
            select(UnpackExecutionItem).where(
                UnpackExecutionItem.execution_id == triggered.execution_id
            )
        )
        assert item is not None
        assert item.status == UnpackItemStatus.MATCH_ERROR.value
        assert item.last_error_code == "UNPACK_DOWNLOADER_PATH_MAPPING_FAILED"


def test_monitor_overlap_is_skipped_and_rescheduled(tmp_path: Path) -> None:
    fake = _FakeDownloaderService((), _FakeBinding({}))
    definitions, monitor, _discovery, factory, _root, output = _services(tmp_path, fake)

    created = definitions.create(_monitor_request(output))
    definitions.run(created.id)
    now = _make_due(factory, created.id)
    first = asyncio.run(monitor.trigger_due_definition(created.id, now=now))
    assert first.execution_id is not None

    with factory() as session:
        definition = session.get(UnpackDefinition, created.id)
        assert definition is not None
        definition.next_run_at = now - timedelta(seconds=1)
        session.commit()

    second = asyncio.run(monitor.trigger_due_definition(created.id, now=now))

    assert second.execution_id is None
    assert second.skipped_overlap is True
    with factory() as session:
        executions = session.scalars(
            select(UnpackExecution).where(UnpackExecution.definition_id == created.id)
        ).all()
        assert len(executions) == 1
