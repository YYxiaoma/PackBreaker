from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.errors import ApplicationError
from backend.app.application.unpack_definitions import (
    UnpackDefinitionCreate,
    UnpackDefinitionService,
    UnpackSelectedSourceCreate,
)
from backend.app.domain.unpack import (
    UnpackDefinitionStatus,
    UnpackExecutionScopeKind,
    UnpackSourceKind,
    UnpackTriggerKind,
)
from backend.app.infrastructure.persistence.base import Base
from backend.app.infrastructure.persistence.models import (
    Downloader,
    Site,
    UnpackDefinitionSelectedSource,
    UnpackExecution,
)


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
        },
        auto_match_threshold_bps=9680,
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
    with factory() as session:
        assert session.scalar(select(func.count(UnpackExecution.id))) == 0


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


def test_delete_unrun_definition_and_preserve_active_execution(tmp_path: Path) -> None:
    service, _factory, source = _service(tmp_path)
    created = service.create(_request(source))
    with pytest.raises(ApplicationError):
        service.delete(created.id, expected_version=created.version + 1)
    service.delete(created.id, expected_version=created.version)
    with pytest.raises(ApplicationError) as missing:
        service.get(created.id)
    assert missing.value.status == 404

    created = service.create(_request(source))
    service.run(created.id)
    with pytest.raises(ApplicationError) as running:
        service.delete(created.id, expected_version=created.version)
    assert running.value.code == "UNPACK_DEFINITION_CONFLICT"


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
        assert execution.config_snapshot["execution_scope_kind"] == "ALL_MATCHING_MEDIA"


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
