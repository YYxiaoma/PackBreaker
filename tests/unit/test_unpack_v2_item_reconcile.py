from __future__ import annotations

import asyncio
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.downloaders import QbittorrentWriteBinding
from backend.app.application.unpack_item_reconcile import (
    ReconcileResolution,
    UnpackItemReconcileService,
)
from backend.app.domain.downloader import PathMappingRule
from backend.app.domain.task_definition import TaskConflictPolicy, TaskStorageMode
from backend.app.domain.unpack import UnpackExecutionStatus, UnpackItemStatus
from backend.app.domain.unpack_execution_plan import (
    UnpackExecutionAction,
    UnpackExecutionActionKind,
    UnpackExecutionPlan,
    unpack_execution_plan_to_payload,
)
from backend.app.domain.verification import VerificationLevel
from backend.app.infrastructure.adapters.downloaders import QbittorrentTorrentState
from backend.app.infrastructure.authorized_paths import AuthorizedPathScope
from backend.app.infrastructure.persistence.base import Base
from backend.app.infrastructure.persistence.database import configure_sqlite_engine
from backend.app.infrastructure.persistence.models import (
    UnpackDefinition,
    UnpackExecution,
    UnpackExecutionItem,
    UnpackExternalOperationJournal,
    UnpackReconciledOperationAudit,
    utc_now,
)
from backend.app.infrastructure.safe_filesystem import SafeFilesystemGateway
from backend.app.infrastructure.source_inventory import current_file_snapshot


class _FakeQbittorrentAdapter:
    def __init__(self, states: tuple[QbittorrentTorrentState, ...]) -> None:
        self._states = states

    async def get_torrents(self, hashes: tuple[str, ...]) -> tuple[QbittorrentTorrentState, ...]:
        wanted = set(hashes)
        return tuple(state for state in self._states if state.torrent_hash in wanted)


class _FakeDownloaderProvider:
    def __init__(
        self,
        adapter: _FakeQbittorrentAdapter,
        data_root: Path,
        container_prefix: Path | None = None,
        version: int = 3,
    ) -> None:
        self._adapter = adapter
        self._data_root = data_root
        self._container_prefix = container_prefix
        self._version = version

    def write_binding(self, downloader_id: str) -> QbittorrentWriteBinding:
        mappings = (
            (
                PathMappingRule(
                    remote_prefix="/downloads/seeding",
                    container_prefix=self._container_prefix.as_posix(),
                ),
            )
            if self._container_prefix is not None
            else ()
        )
        return QbittorrentWriteBinding(
            downloader_id=downloader_id,
            downloader_version=self._version,
            binding_digest="b" * 64,
            path_mappings=mappings,
            capabilities={"supports_skip_checking": True},
            adapter=cast(object, self._adapter),  # type: ignore[arg-type]
            data_root=self._data_root,
        )


def _fixture(
    tmp_path: Path,
    *,
    states: tuple[QbittorrentTorrentState, ...] = (),
    storage_mode: TaskStorageMode = TaskStorageMode.HARDLINK,
    downloader_version: int = 3,
) -> tuple[UnpackItemReconcileService, sessionmaker[Session], Path, Path, str]:
    data_root = tmp_path / "data"
    source = data_root / "source" / "Movie.mkv"
    output = data_root / "seeding"
    source.parent.mkdir(parents=True)
    output.mkdir(parents=True)
    (output / "Pack").mkdir()
    source.write_bytes(b"synthetic-content")
    snapshot = current_file_snapshot(source)
    now = utc_now()
    plan = UnpackExecutionPlan(
        item_id="item-1",
        item_version_before=4,
        candidate_id="candidate-1",
        candidate_generation=1,
        metainfo_digest="a" * 64,
        verification_level=VerificationLevel.SAMPLED_VERIFIED,
        output_directory=output.as_posix(),
        storage_mode=storage_mode,
        conflict_policy=TaskConflictPolicy.VERIFY_REUSE_OR_STOP,
        target_device=output.stat(follow_symlinks=False).st_dev,
        target_downloader_id="downloader-target",
        target_downloader_version=3,
        target_downloader_binding_digest="b" * 64,
        target_remote_save_path="/downloads/seeding",
        client_check_required=False,
        actions=(
            UnpackExecutionAction(
                torrent_path="Pack/Movie.mkv",
                kind=UnpackExecutionActionKind.MATERIALIZE,
                length=snapshot.size,
                source_path=source.as_posix(),
                source_snapshot=snapshot,
            ),
        ),
        create_directories=("Pack",),
        blocked_reasons=(),
        created_at=now,
    )
    target = output / "Pack" / "Movie.mkv"

    engine = configure_sqlite_engine(create_engine("sqlite+pysqlite:///:memory:"))
    Base.metadata.create_all(engine)
    factory: sessionmaker[Session] = sessionmaker(
        bind=engine, expire_on_commit=False, autoflush=False
    )
    with factory() as session:
        session.add(
            UnpackDefinition(
                id="definition-1",
                name="reconcile",
                trigger_kind="MANUAL",
                status="PENDING_EXECUTION",
                source_kind="DIRECTORY",
                execution_scope_kind="ALL_MATCHING_MEDIA",
                source_config={"directory_path": source.parent.as_posix()},
                file_filter={"extensions": [".mkv"]},
                site_ids=["site-1"],
                output_config={
                    "output_directory": output.as_posix(),
                    "storage_mode": storage_mode.value,
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
                status=UnpackExecutionStatus.EXECUTING.value,
                config_snapshot={},
                discovery_complete=True,
                total_count=1,
                matched_auto_count=1,
                review_count=0,
                content_verified_count=1,
                content_mismatch_count=0,
                timeout_count=0,
                error_count=1,
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
                source_snapshot={},
                media_identity={},
                status=UnpackItemStatus.EXECUTION_ERROR.value,
                selected_candidate_id="candidate-1",
                candidate_generation=1,
                retry_count=0,
                content_verification_level=VerificationLevel.SAMPLED_VERIFIED.value,
                torrent_metainfo_digest="a" * 64,
                execution_plan=unpack_execution_plan_to_payload(plan),
                execution_plan_digest=plan.plan_digest,
                execution_plan_created_at=now,
                execution_state={"stage": "FILES_MATERIALIZED", "plan_digest": plan.plan_digest},
                last_error_code="UNPACK_SEED_INVALID",
                last_error_message="只有 FULL_VERIFIED 才允许 qBittorrent skip_checking",
                version=9,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            UnpackExternalOperationJournal(
                id="journal-add",
                item_id="item-1",
                idempotency_key="k" * 64,
                operation_type="UNPACK_EXEC_QBITTORRENT_ADD",
                target={
                    "downloader_id": "downloader-target",
                    "remote_save_path": "/downloads/seeding",
                },
                intent={
                    "expected_hashes": ["c" * 40],
                    "ownership_tag": "pb-owner",
                    "skip_checking": True,
                },
                status="INTENT_RECORDED",
                before_snapshot={"torrent_absent": True},
                after_snapshot=None,
                last_error_code=None,
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()

    filesystem = SafeFilesystemGateway(
        data_root,
        path_scope=AuthorizedPathScope.legacy_only(legacy_data_root=data_root),
    )
    service = UnpackItemReconcileService(
        factory,
        _FakeDownloaderProvider(
            _FakeQbittorrentAdapter(states), data_root, output, version=downloader_version
        ),
        filesystem,
    )
    return service, factory, source, target, plan.plan_digest


def _add_materialize_journal(factory: sessionmaker[Session], *, status: str, target: Path) -> None:
    now = utc_now()
    with factory() as session:
        session.add(
            UnpackExternalOperationJournal(
                id="journal-materialize",
                item_id="item-1",
                idempotency_key="m" * 64,
                operation_type="UNPACK_EXEC_MATERIALIZE_HARDLINK",
                target={"path": target.as_posix(), "torrent_path": "Pack/Movie.mkv"},
                intent={"plan_digest": "x", "source_path": "/irrelevant"},
                status=status,
                before_snapshot={"exists": False},
                after_snapshot=None,
                last_error_code="UNPACK_EXEC_HARDLINK_RESULT_UNKNOWN",
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()


def test_add_journal_without_downloader_torrent_resumes_seeding(tmp_path: Path) -> None:
    service, factory, _source, _target, plan_digest = _fixture(tmp_path)

    report = asyncio.run(service.reconcile_item("item-1", expected_item_version=9))

    assert report.manual_count == 0
    assert report.resolved_count == 1
    assert report.resumed_stage == "FILES_MATERIALIZED"
    assert report.outcomes[0].resolution is ReconcileResolution.NO_EFFECT
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == UnpackItemStatus.EXECUTING.value
        assert item.last_error_code is None
        assert item.execution_state == {"stage": "FILES_MATERIALIZED", "plan_digest": plan_digest}
        # 已确认未生效的 journal 必须归档后移除，续跑时重新登记意图。
        assert session.get(UnpackExternalOperationJournal, "journal-add") is None
        audit = session.scalar(select(UnpackReconciledOperationAudit))
        assert audit is not None
        assert audit.operation_type == "UNPACK_EXEC_QBITTORRENT_ADD"
        assert audit.resolution == "NO_EFFECT"


def test_add_journal_with_owned_torrent_is_marked_applied(tmp_path: Path) -> None:
    state = QbittorrentTorrentState(
        torrent_hash="c" * 40,
        save_path="/downloads/seeding",
        content_path=None,
        state="pausedUP",
        tags=("pb-owner",),
        progress=1.0,
    )
    service, factory, _source, _target, _digest = _fixture(tmp_path, states=(state,))

    report = asyncio.run(service.reconcile_item("item-1", expected_item_version=9))

    assert report.manual_count == 0
    assert report.outcomes[0].resolution is ReconcileResolution.APPLIED
    with factory() as session:
        journal = session.get(UnpackExternalOperationJournal, "journal-add")
        assert journal is not None
        assert journal.status == "APPLIED"
        assert journal.after_snapshot is not None
        assert journal.after_snapshot["ownership_tag"] == "pb-owner"


def test_missing_materialize_target_restarts_materialization(tmp_path: Path) -> None:
    service, factory, _source, target, _digest = _fixture(tmp_path)
    _add_materialize_journal(factory, status="RECONCILE_REQUIRED", target=target)

    report = asyncio.run(service.reconcile_item("item-1", expected_item_version=9))

    assert report.manual_count == 0
    assert report.resumed_stage == "MATERIALIZING"
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None and item.execution_state is not None
        assert item.execution_state["stage"] == "MATERIALIZING"
        assert session.get(UnpackExternalOperationJournal, "journal-materialize") is None
        assert (
            session.scalars(
                select(UnpackReconciledOperationAudit).where(
                    UnpackReconciledOperationAudit.journal_id == "journal-materialize"
                )
            ).all()
            != []
        )


def test_conflicting_target_keeps_manual_review(tmp_path: Path) -> None:
    service, factory, source, target, _digest = _fixture(tmp_path)
    _add_materialize_journal(factory, status="RECONCILE_REQUIRED", target=target)
    # 同路径已存在内容不同的普通文件：不能证明由本次操作创建。
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"someone-else")
    assert target.read_bytes() != source.read_bytes()

    report = asyncio.run(service.reconcile_item("item-1", expected_item_version=9))

    assert report.manual_count == 1
    assert report.resumed_stage is None
    materialize = next(item for item in report.outcomes if item.journal_id == "journal-materialize")
    assert materialize.resolution is ReconcileResolution.MANUAL
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == UnpackItemStatus.EXECUTION_ERROR.value
        assert item.last_error_code == "UNPACK_RECONCILE_MANUAL_REVIEW"
        journal = session.get(UnpackExternalOperationJournal, "journal-materialize")
        assert journal is not None
        assert journal.status == "RECONCILE_REQUIRED"


def test_version_mismatch_is_rejected(tmp_path: Path) -> None:
    service, _factory, _source, _target, _digest = _fixture(tmp_path)

    with pytest.raises(Exception) as failure:
        asyncio.run(service.reconcile_item("item-1", expected_item_version=3))

    assert getattr(failure.value, "code", "") == "UNPACK_ITEM_VERSION_CONFLICT"


def test_stale_plan_binding_is_reset_to_replanning(tmp_path: Path) -> None:
    """下载器映射改过之后，冻结计划必须作废并按当前配置重新规划。"""

    # 当前绑定版本是 4，而冻结计划里是 3：等价于用户改过下载器配置。
    service, factory, _source, target, _digest = _fixture(tmp_path, downloader_version=4)
    _add_materialize_journal(factory, status="APPLIED", target=target)

    report = asyncio.run(service.reconcile_item("item-1", expected_item_version=9))

    assert report.manual_count == 0
    assert report.resumed_stage == "CONTENT_VERIFIED"
    assert report.outcomes[0].resolution is ReconcileResolution.PLAN_RESET
    with factory() as session:
        item = session.get(UnpackExecutionItem, "item-1")
        assert item is not None
        assert item.status == UnpackItemStatus.CONTENT_VERIFIED.value
        assert item.execution_plan is None
        assert item.execution_plan_digest is None
        assert item.execution_state is None
        assert item.last_error_code is None
        # 旧计划相关的 journal 全部归档后移除，避免新计划撞上旧证据。
        assert (
            session.scalars(
                select(UnpackExternalOperationJournal).where(
                    UnpackExternalOperationJournal.item_id == "item-1"
                )
            ).all()
            == []
        )
        audits = session.scalars(
            select(UnpackReconciledOperationAudit).where(
                UnpackReconciledOperationAudit.resolution == "PLAN_RESET"
            )
        ).all()
        assert len(audits) == 2
        assert all("冻结计划失效" in (audit.note or "") for audit in audits)
        assert audits[0].payload["intent"] is not None
