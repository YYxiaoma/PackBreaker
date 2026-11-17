from __future__ import annotations

import hashlib
import json
import zipfile
from io import BytesIO
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.errors import ApplicationError
from backend.app.application.unpack_template_transfer import (
    ORIGIN_BUILTIN,
    export_execution_template,
    import_template_file,
    install_builtin_templates,
    parse_template_archive,
    preview_template_file,
)
from backend.app.domain.site_config import SiteCredentialKind
from backend.app.domain.task_definition import TaskConflictPolicy, TaskStorageMode
from backend.app.domain.unpack import UnpackExecutionStatus, UnpackItemStatus
from backend.app.domain.unpack_execution_plan import (
    UnpackExecutionAction,
    UnpackExecutionActionKind,
    UnpackExecutionPlan,
    unpack_execution_plan_to_payload,
)
from backend.app.domain.verification import VerificationLevel
from backend.app.infrastructure.persistence.base import Base
from backend.app.infrastructure.persistence.models import (
    Site,
    UnpackDefinition,
    UnpackExecution,
    UnpackExecutionItem,
    UnpackMatchCandidate,
    UnpackTemplate,
    UnpackTemplateSeed,
    UnpackTemplateVersion,
    utc_now,
)
from backend.app.infrastructure.source_inventory import current_file_snapshot

COLLECTION = "周星驰.Stephen.Chow.1988-2017"
MOVIE_DIR = "整蛊专家.Tricky.Brains.1991"
MOVIE_FILE = "Tricky.Brains.1991.BluRay.mkv"


def _factory() -> tuple[sessionmaker[Session], Path]:
    root = Path(__file__).resolve().parent / "_tmpl_tmp"
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory: sessionmaker[Session] = sessionmaker(
        bind=engine, expire_on_commit=False, autoflush=False
    )
    return factory, root


def _seed_execution(
    factory: sessionmaker[Session],
    tmp_path: Path,
    *,
    execution_status: str = UnpackExecutionStatus.COMPLETED.value,
    item_status: str = UnpackItemStatus.COMPLETED.value,
    site_type: str = "MTEAM",
    second_site_type: str | None = None,
) -> None:
    source_root = tmp_path / "源目录" / COLLECTION
    movie_dir = source_root / MOVIE_DIR
    movie_dir.mkdir(parents=True)
    movie_file = movie_dir / MOVIE_FILE
    movie_file.write_bytes(b"synthetic-video")
    snapshot = current_file_snapshot(movie_file)
    now = utc_now()

    plan = UnpackExecutionPlan(
        item_id="item-1",
        item_version_before=1,
        candidate_id="candidate-1",
        candidate_generation=1,
        metainfo_digest="a" * 64,
        verification_level=VerificationLevel.SAMPLED_VERIFIED,
        output_directory=(tmp_path / "输出").as_posix(),
        storage_mode=TaskStorageMode.HARDLINK,
        conflict_policy=TaskConflictPolicy.VERIFY_REUSE_OR_STOP,
        target_device=1,
        target_downloader_id="downloader-1",
        target_downloader_version=1,
        target_downloader_binding_digest="b" * 64,
        target_remote_save_path="/downloads/测试",
        client_check_required=False,
        actions=(
            UnpackExecutionAction(
                torrent_path=f"{MOVIE_DIR}/{MOVIE_FILE}",
                kind=UnpackExecutionActionKind.MATERIALIZE,
                length=snapshot.size,
                source_path=movie_file.as_posix(),
                source_snapshot=snapshot,
            ),
        ),
        create_directories=(MOVIE_DIR,),
        blocked_reasons=(),
        created_at=now,
    )

    with factory() as session:
        session.add(
            Site(
                id="site-1",
                name="M-Team",
                type=site_type,
                base_url="https://kp.m-team.cc",
                credential_kind=SiteCredentialKind.COOKIE.value,
                capabilities={},
                connection_status="OK",
                enabled=True,
                version=1,
                created_at=now,
                updated_at=now,
            )
        )
        if second_site_type is not None:
            session.add(
                Site(
                    id="site-2",
                    name="Second",
                    type=second_site_type,
                    base_url="https://www.tangpt.top",
                    credential_kind=SiteCredentialKind.COOKIE.value,
                    capabilities={},
                    connection_status="OK",
                    enabled=True,
                    version=1,
                    created_at=now,
                    updated_at=now,
                )
            )
        session.add(
            UnpackDefinition(
                id="definition-1",
                name="测试2",
                trigger_kind="MANUAL",
                status="PENDING_EXECUTION",
                source_kind="DIRECTORY",
                execution_scope_kind="ALL_MATCHING_MEDIA",
                source_config={"directory_path": source_root.as_posix()},
                file_filter={"extensions": [".mkv"]},
                site_ids=["site-1"],
                output_config={"output_directory": (tmp_path / "输出").as_posix()},
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
                status=execution_status,
                config_snapshot={},
                discovery_complete=True,
                total_count=2,
                matched_auto_count=1,
                review_count=0,
                content_verified_count=0,
                content_mismatch_count=0,
                timeout_count=0,
                error_count=0,
                completed_count=1,
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
                source_snapshot={"path": movie_file.as_posix()},
                media_identity={"title": MOVIE_DIR},
                status=item_status,
                selected_candidate_id="candidate-1",
                candidate_generation=1,
                retry_count=0,
                content_verification_level=VerificationLevel.SAMPLED_VERIFIED.value,
                torrent_metainfo_digest="a" * 64,
                execution_plan=unpack_execution_plan_to_payload(plan),
                execution_plan_digest=plan.plan_digest,
                execution_plan_created_at=now,
                version=3,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            UnpackExecutionItem(
                id="item-2",
                execution_id="execution-1",
                source_object_key="source-2",
                source_snapshot={},
                media_identity={"title": "无匹配影片"},
                status=UnpackItemStatus.NO_MATCH.value,
                selected_candidate_id=None,
                candidate_generation=1,
                retry_count=0,
                version=1,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            UnpackMatchCandidate(
                id="candidate-1",
                item_id="item-1",
                generation=1,
                site_id="site-1",
                candidate_key="123456",
                title=MOVIE_DIR,
                size_bytes=snapshot.size,
                seeders=10,
                score_bps=10000,
                is_exact_match=True,
                evidence={},
                raw_ref={},
                verification_status="VERIFIED",
                imdb_id="tt0103035",
            )
        )
        session.commit()


def test_export_derives_collection_root_without_absolute_prefix(tmp_path: Path) -> None:
    factory, _ = _factory()
    _seed_execution(factory, tmp_path)

    summary = export_execution_template(factory, "execution-1")

    assert summary.site_key == "MTEAM"
    assert summary.seed_count == 1
    # 只导出完成项：NO_MATCH 那条被跳过
    assert summary.skipped_count == 0
    assert summary.collection_root_name == COLLECTION

    parsed = parse_template_archive(summary.payload)
    assert parsed.site_key == "MTEAM"
    assert len(parsed.seeds) == 1
    seed = parsed.seeds[0]
    assert seed.remote_torrent_id == "123456"
    assert seed.mappings[0]["collection_root_name"] == COLLECTION
    assert seed.mappings[0]["relative_directory"] == MOVIE_DIR
    assert seed.mappings[0]["relative_file_name"] == MOVIE_FILE
    # 文件清单来自冻结计划，字段口径与站点详情一致
    assert seed.file_summary[0]["basename"] == MOVIE_FILE
    assert all("/" not in item["collection_root_name"] for item in seed.mappings)


def test_export_rejects_running_execution(tmp_path: Path) -> None:
    factory, _ = _factory()
    _seed_execution(factory, tmp_path, execution_status=UnpackExecutionStatus.EXECUTING.value)

    with pytest.raises(ApplicationError) as failure:
        export_execution_template(factory, "execution-1")

    assert failure.value.code == "UNPACK_TEMPLATE_EXPORT_NOT_FINISHED"


def test_export_skips_multi_site_until_site_chosen(tmp_path: Path) -> None:
    factory, _ = _factory()
    _seed_execution(factory, tmp_path, second_site_type="TANGPT")
    now = utc_now()
    with factory() as session:
        first = session.get(UnpackExecutionItem, "item-1")
        assert first is not None and first.execution_plan is not None
        # 第二个完成项落在另一个站点，构成跨站点执行
        session.add(
            UnpackExecutionItem(
                id="item-3",
                execution_id="execution-1",
                source_object_key="source-3",
                source_snapshot=dict(first.source_snapshot),
                media_identity={"title": MOVIE_DIR},
                status=UnpackItemStatus.COMPLETED.value,
                selected_candidate_id="candidate-3",
                candidate_generation=1,
                retry_count=0,
                execution_plan=dict(first.execution_plan),
                execution_plan_digest=first.execution_plan_digest,
                version=3,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            UnpackMatchCandidate(
                id="candidate-3",
                item_id="item-3",
                generation=1,
                site_id="site-2",
                candidate_key="999",
                title=MOVIE_DIR,
                size_bytes=1024,
                seeders=1,
                score_bps=10000,
                is_exact_match=True,
                evidence={},
                raw_ref={},
                verification_status="VERIFIED",
            )
        )
        session.commit()

    with pytest.raises(ApplicationError) as failure:
        export_execution_template(factory, "execution-1")
    assert failure.value.code == "UNPACK_TEMPLATE_EXPORT_SITE_AMBIGUOUS"

    tangpt = export_execution_template(factory, "execution-1", target_site_key="TANGPT")
    assert tangpt.site_key == "TANGPT"
    assert tangpt.seed_count == 1
    assert parse_template_archive(tangpt.payload).seeds[0].remote_torrent_id == "999"


def test_import_roundtrip_creates_template_and_renames_conflict(tmp_path: Path) -> None:
    factory, _ = _factory()
    _seed_execution(factory, tmp_path)
    summary = export_execution_template(factory, "execution-1")
    parsed = parse_template_archive(summary.payload)

    preview = preview_template_file(factory, parsed)
    assert preview.renamed is False
    assert preview.seed_count == 1
    assert preview.mapped_media_count == 1

    first = import_template_file(factory, parsed)
    assert first.status == "READY"
    assert first.seed_count == 1

    second = import_template_file(factory, parsed)
    assert second.renamed is True
    assert second.name.endswith("（导入 2）")

    with factory() as session:
        templates = session.scalars(select(UnpackTemplate)).all()
        assert len(templates) == 2
        versions = session.scalars(select(UnpackTemplateVersion)).all()
        assert {version.status for version in versions} == {"READY"}
        seeds = session.scalars(select(UnpackTemplateSeed)).all()
        assert len(seeds) == 2


def test_archive_validation_rejects_tampering() -> None:
    payload = json.dumps(
        {
            "schema_version": 1,
            "site_key": "MTEAM",
            "seeds": [
                {
                    "remote_torrent_id": "1",
                    "display_name": "movie",
                    "file_summary": [{"basename": "a.mkv", "length": 1}],
                }
            ],
        },
        ensure_ascii=False,
    ).encode()
    manifest = {
        "schema_version": 1,
        "kind": "packbreaker.unpack.template",
        "name": "tampered",
        "site_key": "MTEAM",
        "payload_sha256": hashlib.sha256(payload).hexdigest(),
    }
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr("payload.json", payload)
    assert parse_template_archive(buffer.getvalue()).name == "tampered"

    # 摘要被改动 → 拒绝
    manifest["payload_sha256"] = "0" * 64
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr("payload.json", payload)
    with pytest.raises(ApplicationError) as failure:
        parse_template_archive(buffer.getvalue())
    assert failure.value.code == "UNPACK_TEMPLATE_FILE_DIGEST"

    # 多余成员 → 拒绝
    manifest["payload_sha256"] = hashlib.sha256(payload).hexdigest()
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr("payload.json", payload)
        archive.writestr("evil.txt", b"x")
    with pytest.raises(ApplicationError) as failure:
        parse_template_archive(buffer.getvalue())
    assert failure.value.code == "UNPACK_TEMPLATE_FILE_MEMBERS"


def test_builtin_install_is_idempotent_and_upgrades(tmp_path: Path) -> None:
    factory, _ = _factory()
    _seed_execution(factory, tmp_path)
    summary = export_execution_template(factory, "execution-1")
    official = tmp_path / "official"
    official.mkdir()
    file_name = "builtin.pbtemplate"
    (official / file_name).write_bytes(summary.payload)

    def write_index(version: int) -> None:
        (official / "index.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "templates": [
                        {
                            "builtin_key": "demo@MTEAM",
                            "name": "内置模板",
                            "site_key": "MTEAM",
                            "version": version,
                            "file": file_name,
                            "sha256": hashlib.sha256(summary.payload).hexdigest(),
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    write_index(1)
    assert install_builtin_templates(factory, official) == 1
    assert install_builtin_templates(factory, official) == 0

    write_index(2)
    assert install_builtin_templates(factory, official) == 1

    with factory() as session:
        template = session.scalar(
            select(UnpackTemplate).where(UnpackTemplate.builtin_key == "demo@MTEAM")
        )
        assert template is not None
        assert template.origin == ORIGIN_BUILTIN
        assert template.current_version == 2
        versions = session.scalars(
            select(UnpackTemplateVersion).where(UnpackTemplateVersion.template_id == template.id)
        ).all()
        # 升级新增版本，旧版本保留
        assert sorted(version.version for version in versions) == [1, 2]
