from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.unpack_definitions import (
    UnpackDefinitionCreate,
    UnpackDefinitionService,
    UnpackSelectedSourceCreate,
)
from backend.app.application.unpack_discovery import UnpackDiscoveryService
from backend.app.domain.unpack import (
    UnpackExecutionScopeKind,
    UnpackExecutionStatus,
    UnpackItemStatus,
    UnpackSourceKind,
    UnpackTriggerKind,
)
from backend.app.infrastructure.authorized_paths import AuthorizedPathScope
from backend.app.infrastructure.persistence.base import Base
from backend.app.infrastructure.persistence.models import (
    Downloader,
    Site,
    UnpackExecution,
    UnpackExecutionItem,
)


def _services(
    tmp_path: Path,
) -> tuple[
    UnpackDefinitionService,
    UnpackDiscoveryService,
    sessionmaker[Session],
    Path,
    Path,
]:
    root = tmp_path / "data"
    source = root / "movies"
    output = root / "seeding"
    source.mkdir(parents=True)
    output.mkdir()
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
    scope = AuthorizedPathScope.legacy_only(legacy_data_root=root)
    return (
        UnpackDefinitionService(
            factory,
            data_root=root,
            timezone="Asia/Shanghai",
            path_scope=scope,
        ),
        UnpackDiscoveryService(factory, path_scope=scope),
        factory,
        source,
        output,
    )


def _request(
    source: Path,
    output: Path,
    *,
    scope: UnpackExecutionScopeKind,
    selected: tuple[UnpackSelectedSourceCreate, ...] = (),
) -> UnpackDefinitionCreate:
    return UnpackDefinitionCreate(
        name="发现测试",
        trigger_kind=UnpackTriggerKind.MANUAL,
        source_kind=UnpackSourceKind.DIRECTORY,
        execution_scope_kind=scope,
        source_config={"directory_path": source.as_posix()},
        file_filter={"extensions": [".mkv"]},
        site_ids=("site-1",),
        output_config={
            "output_directory": output.as_posix(),
            "storage_mode": "HARDLINK",
            "conflict_policy": "VERIFY_REUSE_OR_STOP",
            "target_downloader_id": "downloader-target",
        },
        selected_sources=selected,
    )


def test_all_matching_media_discovers_pages_and_transitions_to_matching(tmp_path: Path) -> None:
    definitions, discovery, factory, source, output = _services(tmp_path)
    (source / "A.2026.2160p.mkv").write_bytes(b"a")
    (source / "B.2026.1080p.mkv").write_bytes(b"bb")
    (source / "C.2026.nfo").write_text("metadata", encoding="utf-8")

    definition = definitions.create(
        _request(
            source,
            output,
            scope=UnpackExecutionScopeKind.ALL_MATCHING_MEDIA,
        )
    )
    run = definitions.run(definition.id)
    assert run.execution_id is not None

    first = discovery.discover_next_page(run.execution_id, limit=1)
    assert first.created_count == 1
    assert first.discovery_complete is False
    assert first.execution_status is UnpackExecutionStatus.DISCOVERING

    second = discovery.discover_next_page(run.execution_id, limit=1)
    assert second.created_count == 1
    assert second.discovery_complete is False

    final = discovery.discover_next_page(run.execution_id, limit=1)
    assert final.created_count == 0
    assert final.discovery_complete is True
    assert final.total_count == 2
    assert final.execution_status is UnpackExecutionStatus.MATCHING

    with factory() as session:
        items = session.scalars(
            select(UnpackExecutionItem)
            .where(UnpackExecutionItem.execution_id == run.execution_id)
            .order_by(UnpackExecutionItem.source_object_key)
        ).all()
        assert len(items) == 2
        assert {item.status for item in items} == {UnpackItemStatus.MATCH_PENDING.value}

    repeated = discovery.discover_next_page(run.execution_id, limit=1)
    assert repeated.created_count == 0
    assert repeated.total_count == 2


def test_all_matching_media_empty_directory_completes_without_error(tmp_path: Path) -> None:
    definitions, discovery, _factory, source, output = _services(tmp_path)
    definition = definitions.create(
        _request(
            source,
            output,
            scope=UnpackExecutionScopeKind.ALL_MATCHING_MEDIA,
        )
    )
    run = definitions.run(definition.id)
    assert run.execution_id is not None

    report = discovery.discover_next_page(run.execution_id)

    assert report.discovery_complete is True
    assert report.total_count == 0
    assert report.execution_status is UnpackExecutionStatus.COMPLETED


def test_selected_media_does_not_expand_to_new_directory_files(tmp_path: Path) -> None:
    definitions, discovery, factory, source, output = _services(tmp_path)
    selected_file = source / "Selected.2026.2160p.mkv"
    selected_file.write_bytes(b"selected")
    selected = UnpackSelectedSourceCreate(
        source_object_key="selected-key",
        canonical_path_hint=selected_file.as_posix(),
        filename=selected_file.name,
        size_bytes_at_selection=selected_file.stat().st_size,
    )
    definition = definitions.create(
        _request(
            source,
            output,
            scope=UnpackExecutionScopeKind.SELECTED_MEDIA,
            selected=(selected,),
        )
    )

    (source / "Added.After.Save.2026.2160p.mkv").write_bytes(b"must-not-expand")
    run = definitions.run(definition.id)
    assert run.execution_id is not None
    report = discovery.discover_next_page(run.execution_id)

    assert report.total_count == 1
    assert report.error_count == 0
    assert report.execution_status is UnpackExecutionStatus.MATCHING
    with factory() as session:
        items = session.scalars(
            select(UnpackExecutionItem).where(UnpackExecutionItem.execution_id == run.execution_id)
        ).all()
        assert [item.source_object_key for item in items] == ["selected-key"]


def test_selected_media_replacement_is_blocked_as_source_changed(tmp_path: Path) -> None:
    definitions, discovery, factory, source, output = _services(tmp_path)
    selected_file = source / "Selected.2026.2160p.mkv"
    selected_file.write_bytes(b"original")
    selected = UnpackSelectedSourceCreate(
        source_object_key="selected-key",
        canonical_path_hint=selected_file.as_posix(),
        filename=selected_file.name,
        size_bytes_at_selection=selected_file.stat().st_size,
    )
    definition = definitions.create(
        _request(
            source,
            output,
            scope=UnpackExecutionScopeKind.SELECTED_MEDIA,
            selected=(selected,),
        )
    )

    selected_file.unlink()
    selected_file.write_bytes(b"replacement")
    run = definitions.run(definition.id)
    assert run.execution_id is not None
    report = discovery.discover_next_page(run.execution_id)

    assert report.total_count == 1
    assert report.error_count == 1
    assert report.execution_status is UnpackExecutionStatus.COMPLETED_WITH_ERRORS
    with factory() as session:
        item = session.scalar(
            select(UnpackExecutionItem).where(UnpackExecutionItem.execution_id == run.execution_id)
        )
        assert item is not None
        assert item.status == UnpackItemStatus.MATCH_ERROR.value
        assert item.last_error_code == "UNPACK_SOURCE_CHANGED"
        assert "已发生变化" in (item.last_error_message or "")


def test_selected_media_discovery_is_paginated(tmp_path: Path) -> None:
    definitions, discovery, factory, source, output = _services(tmp_path)
    selected_sources: list[UnpackSelectedSourceCreate] = []
    for index in range(3):
        path = source / f"Movie.{index}.2026.1080p.mkv"
        path.write_bytes(str(index).encode())
        selected_sources.append(
            UnpackSelectedSourceCreate(
                source_object_key=f"key-{index}",
                canonical_path_hint=path.as_posix(),
                filename=path.name,
                size_bytes_at_selection=path.stat().st_size,
            )
        )

    definition = definitions.create(
        _request(
            source,
            output,
            scope=UnpackExecutionScopeKind.SELECTED_MEDIA,
            selected=tuple(selected_sources),
        )
    )
    run = definitions.run(definition.id)
    assert run.execution_id is not None

    first = discovery.discover_next_page(run.execution_id, limit=2)
    assert first.total_count == 2
    assert first.discovery_complete is False
    assert first.next_cursor == "selected:2"

    final = discovery.discover_next_page(run.execution_id, limit=2)
    assert final.total_count == 3
    assert final.discovery_complete is True
    assert final.execution_status is UnpackExecutionStatus.MATCHING

    with factory() as session:
        execution = session.get(UnpackExecution, run.execution_id)
        assert execution is not None
        assert execution.discovery_cursor == "selected:3"
