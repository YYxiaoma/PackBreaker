"""Synthetic task lifecycle through real pre-execution and linking coordinators.

The site is an in-process fake and no downloader is contacted. This tests
analysis, admin review, execution gate, plan, and the file-journal handoff,
without claiming that those steps ran in a real-client task E2E.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.app.application.errors import ApplicationError
from backend.app.application.filesystem_operations import (
    CREATE_HARDLINK_OPERATION,
    FilesystemOperationService,
)
from backend.app.application.sites import EnabledSiteAdapter
from backend.app.application.task_linking import TaskLinkingCoordinator
from backend.app.application.tasks import TaskAnalysisService
from backend.app.domain.task_state import TaskStatus
from backend.app.domain.task_units import SourceTaskFile, identify_task_units
from backend.app.domain.verification import DownloaderKind, VerificationLevel
from backend.app.infrastructure.persistence.base import Base
from backend.app.infrastructure.persistence.database import (
    create_session_factory,
    create_sqlite_engine,
)
from backend.app.infrastructure.persistence.models import Downloader, OperationJournal, UnpackTask
from backend.app.infrastructure.safe_filesystem import SafeFilesystemGateway
from tests.application.test_analysis import (
    _FakeAdapter,
    _FakeSiteProvider,
    _v1_torrent,
)


@pytest.mark.asyncio
async def test_synthetic_analysis_review_plan_and_linking_share_persisted_authorization(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    source = data_root / "source"
    target = data_root / "target"
    source.mkdir(parents=True)
    target.mkdir()
    content = b"0123456789abcdef"
    movie = source / "Movie.2026.mkv"
    movie.write_bytes(content)
    before = movie.stat(follow_symlinks=False)
    unit = identify_task_units((SourceTaskFile(movie.name, len(content)),))[0]
    torrent = _v1_torrent(movie.name.encode(), content, piece_length=16384)
    adapter = _FakeAdapter("synthetic", torrent)
    sites = _FakeSiteProvider((EnabledSiteAdapter("cfg-synthetic", 1, "synthetic", adapter),))

    engine = create_sqlite_engine(tmp_path / "pre-execution.db")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    service = TaskAnalysisService(factory, sites, data_root=data_root)
    try:
        created = service.create_task(
            task_type="PACKAGE_UNPACK",
            source_downloader_id="synthetic-source",
            source_hash="synthetic-source-hash",
            normalized_unit_key=unit.normalized_unit_key,
        )
        assert created.created and created.task.status == TaskStatus.PENDING.value
        preflight = await service.analyze(created.task.id, source_root="source")
        units = service.list_units(created.task.id)
        candidates = service.list_candidates(created.task.id)
        assert len(units) == 1 and len(candidates) == 1
        candidate = candidates[0]
        assert candidate.verification_level == VerificationLevel.FULL_VERIFIED.value
        assert candidate.metainfo_digest and not candidate.rejected
        assert service.latest_preflight(created.task.id).current
        assert service.get_task(created.task.id).status == TaskStatus.PREFLIGHT.value

        # Review/gate must reject missing or foreign authorization before any
        # filesystem journal exists. The only approval below uses the actual
        # preflight candidate, rather than inserting an approved fixture row.
        with pytest.raises(ApplicationError) as no_review:
            service.refresh_execution_gate(units[0].id)
        assert no_review.value.code == "REVIEW_NOT_FOUND"
        with pytest.raises(ApplicationError) as foreign_candidate:
            service.submit_review(
                units[0].id,
                expected_version=0,
                approved_candidate_id=str(uuid4()),
                rejected_candidate_ids=(),
                manual_mappings=(),
                note=None,
                actor_kind="ADMIN",
                actor_id="synthetic-admin",
            )
        assert foreign_candidate.value.code == "REVIEW_INPUT_INVALID"

        review = service.submit_review(
            units[0].id,
            expected_version=0,
            approved_candidate_id=candidate.id,
            rejected_candidate_ids=(),
            manual_mappings=(),
            note=None,
            actor_kind="ADMIN",
            actor_id="synthetic-admin",
        )
        assert review.approved_candidate_id == candidate.id
        assert not review.requires_reverification
        assert service.get_task(created.task.id).status == TaskStatus.AWAITING_CONFIRMATION.value
        gate = service.refresh_execution_gate(units[0].id)
        assert gate.eligible and gate.current and not gate.client_check_required
        assert gate.verification_level == VerificationLevel.FULL_VERIFIED.value
        assert gate.candidate_id == candidate.id and gate.review_revision_id == review.id

        # Configure only a synthetic, disconnected target binding. Planning
        # must remain read-only; the actual downloader is exercised in the
        # separate native ARM64 LINKING/ADDING integration tests.
        with factory() as session:
            session.add(
                Downloader(
                    id="synthetic-target",
                    name="Synthetic Transmission",
                    type=DownloaderKind.TRANSMISSION.value,
                    base_url="http://127.0.0.1:9091/transmission/rpc",
                    enabled=True,
                    version=1,
                    connection_status="OK",
                    path_mapping_status="OK",
                    path_mappings=[
                        {"remote_prefix": "/downloads", "container_prefix": str(data_root)}
                    ],
                    capabilities={
                        "supports_force_recheck": True,
                        "supports_verify_progress": True,
                    },
                )
            )
            session.commit()
        plan = await service.create_execution_plan(
            units[0].id, target_root="target", target_downloader_id="synthetic-target"
        )
        assert plan.ready and plan.current and plan.hardlink_count == 1
        assert plan.client_fetch_count == 0 and not plan.blocked_reasons
        assert plan.target_remote_save_path == "/downloads/target"
        assert service.get_execution_plan(units[0].id).plan_digest == plan.plan_digest

        after = movie.stat(follow_symlinks=False)
        assert (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) == (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        )
        assert movie.read_bytes() == content and list(target.iterdir()) == []
        with factory() as session:
            assert session.get(UnpackTask, created.task.id) is not None
            assert list(session.scalars(select(OperationJournal))) == []
        assert preflight.snapshot_digest

        linker = TaskLinkingCoordinator(
            factory,
            service,
            FilesystemOperationService(factory, SafeFilesystemGateway(data_root)),
            data_root=data_root,
        )
        linked = linker.execute(units[0].id, execution_plan_id=plan.id)
        repeated_link = linker.execute(units[0].id, execution_plan_id=plan.id)
        assert linked.linked_file_count == 1 and not linked.replayed
        assert repeated_link.replayed
        assert repeated_link.hardlink_journal_ids == linked.hardlink_journal_ids
        assert service.get_task(created.task.id).status == TaskStatus.ADDING.value
        assert (target / movie.name).stat(follow_symlinks=False).st_ino == before.st_ino
        assert movie.stat(follow_symlinks=False).st_mtime_ns == before.st_mtime_ns
        with factory() as session:
            journals = list(session.scalars(select(OperationJournal)))
            assert [item.operation_type for item in journals] == [CREATE_HARDLINK_OPERATION]
            assert journals[0].status == "APPLIED"
            assert journals[0].id == linked.hardlink_journal_ids[0]
    finally:
        engine.dispose()
