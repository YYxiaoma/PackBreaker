"""Isolated Rousi dual-secret adapter -> real analysis/review/plan/link journal.

All site responses are synthetic MockTransport traffic. The site remains
PENDING_ADAPTER in production; no real downloader or tracker is contacted.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import httpx2
import pytest
from sqlalchemy import select

from backend.app.application.downloader_operations import (
    QbittorrentAddOperationService,
    QbittorrentRecheckOperationService,
    QbittorrentRemoveOperationService,
    QbittorrentStartOperationService,
)
from backend.app.application.downloaders import TransmissionWriteBinding
from backend.app.application.errors import ApplicationError
from backend.app.application.filesystem_operations import (
    CREATE_HARDLINK_OPERATION,
    FilesystemOperationService,
)
from backend.app.application.sites import EnabledSiteAdapter
from backend.app.application.task_adding import TaskAddingCoordinator
from backend.app.application.task_cancellation import TaskCancellationCoordinator
from backend.app.application.task_client_verification import TaskClientVerificationCoordinator
from backend.app.application.task_linking import TaskLinkingCoordinator
from backend.app.application.task_seeding import TaskSeedingCoordinator
from backend.app.application.tasks import TaskAnalysisService
from backend.app.application.transmission_operations import (
    TRANSMISSION_ADD_OPERATION,
    TRANSMISSION_START_OPERATION,
    TRANSMISSION_VERIFY_OPERATION,
    TransmissionAddOperationService,
    TransmissionRemoveOperationService,
    TransmissionStartOperationService,
    TransmissionVerifyOperationService,
)
from backend.app.domain.downloader import (
    PathMappingRule,
    ProbeStatus,
    downloader_execution_binding_digest,
)
from backend.app.domain.site_config import SiteKind, site_profile
from backend.app.domain.task_state import TaskStatus
from backend.app.domain.task_units import SourceTaskFile, identify_task_units
from backend.app.domain.verification import DownloaderKind, VerificationLevel
from backend.app.infrastructure.adapters.sites import SiteAdapterFactory
from backend.app.infrastructure.persistence.base import Base
from backend.app.infrastructure.persistence.database import (
    create_session_factory,
    create_sqlite_engine,
)
from backend.app.infrastructure.persistence.models import Downloader, OperationJournal
from backend.app.infrastructure.safe_filesystem import SafeFilesystemGateway
from tests.application.test_analysis import _FakeSiteProvider, _v1_torrent
from tests.application.test_task_adding import _FakeDownloaderProvider, _FakeTransmission


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("valid_metainfo", "changed_on_plan", "start_response_lost"),
    ((True, False, False), (False, False, False), (True, True, False), (True, False, True)),
)
async def test_rousi_dual_secret_candidate_cannot_link_without_verified_approval(
    tmp_path: Path, valid_metainfo: bool, changed_on_plan: bool, start_response_lost: bool
) -> None:
    data_root = tmp_path / "data"
    source = data_root / "source"
    target = data_root / "target"
    source.mkdir(parents=True)
    target.mkdir()
    contents = b"0123456789abcdef"
    movie = source / "Movie.2026.mkv"
    movie.write_bytes(contents)
    original = movie.stat(follow_symlinks=False)
    unit = identify_task_units((SourceTaskFile(movie.name, len(contents)),))[0]
    torrent = _v1_torrent(movie.name.encode(), contents, piece_length=16384)
    changed_torrent = _v1_torrent(movie.name.encode(), b"changed-synthetic", piece_length=16384)
    api_key = "synthetic-rousi-api-key"
    cookie = "synthetic-rousi-download-cookie"
    calls: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request.url.path)
        assert request.url.host == "rousi.pro"
        assert request.headers.get("authorization") is None
        if request.url.path == "/api/v1/torrents":
            assert request.headers.get("api-token") == api_key
            assert request.headers.get("cookie") is None
            return httpx2.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "torrents": [
                            {
                                "id": 123,
                                "title": "Movie.2026",
                                "size": len(contents),
                                "seeders": 1,
                                "leechers": 0,
                            }
                        ],
                        "page": 1,
                        "page_size": 100,
                        "total": 1,
                    },
                },
            )
        assert request.url.path == "/api/v1/torrents/123/download"
        assert request.headers.get("cookie") == cookie
        assert request.headers.get("api-token") is None
        return httpx2.Response(
            200,
            content=(
                b"d3:foo3:bare"
                if not valid_metainfo
                else changed_torrent
                if changed_on_plan and calls.count("/api/v1/torrents/123/download") > 1
                else torrent
            ),
            headers={"content-type": "application/x-bittorrent"},
        )

    adapter = SiteAdapterFactory(transport=httpx2.MockTransport(handler)).create(
        kind=SiteKind.ROUSI_PRO,
        base_url=site_profile(SiteKind.ROUSI_PRO).base_url,
        credential_kind=site_profile(SiteKind.ROUSI_PRO).credential_kind,
        credential=api_key,
        download_cookie=cookie,
    )
    sites = _FakeSiteProvider((EnabledSiteAdapter("cfg-rousi", 1, "rousi_pro", adapter),))
    engine = create_sqlite_engine(tmp_path / "rousi-pre-execution.db")
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
        preflight = await service.analyze(created.task.id, source_root="source")
        units = service.list_units(created.task.id)
        candidates = service.list_candidates(created.task.id)
        assert len(units) == len(candidates) == 1
        selected = candidates[0]
        assert selected.site_id == "rousi_pro"
        assert selected.selected_for_verification
        assert calls == ["/api/v1/torrents", "/api/v1/torrents/123/download"]
        assert preflight.snapshot_digest

        # Search metadata alone cannot authorize a link; even the valid
        # metainfo needs an explicit review of this exact candidate.
        with pytest.raises(ApplicationError) as missing_review:
            service.refresh_execution_gate(units[0].id)
        assert missing_review.value.code == "REVIEW_NOT_FOUND"
        with pytest.raises(ApplicationError) as foreign_review:
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
        assert foreign_review.value.code == "REVIEW_INPUT_INVALID"

        with factory() as session:
            assert list(session.scalars(select(OperationJournal))) == []
        assert list(target.iterdir()) == []

        if not valid_metainfo:
            assert selected.verification_level is None
            assert selected.error_code == "SITE_INVALID_RESPONSE"
            assert not selected.metainfo_digest
            review = service.submit_review(
                units[0].id,
                expected_version=0,
                approved_candidate_id=selected.id,
                rejected_candidate_ids=(),
                manual_mappings=(),
                note=None,
                actor_kind="ADMIN",
                actor_id="synthetic-admin",
            )
            assert review.requires_reverification
            gate = service.refresh_execution_gate(units[0].id)
            assert not gate.eligible
            assert "REVERIFICATION_REQUIRED" in gate.blocked_reasons
            with pytest.raises(ApplicationError) as blocked_plan:
                await service.create_execution_plan(
                    units[0].id, target_root="target", target_downloader_id="synthetic-target"
                )
            assert blocked_plan.value.code == "EXECUTION_PLAN_GATE_NOT_READY"
            with factory() as session:
                assert list(session.scalars(select(OperationJournal))) == []
            assert list(target.iterdir()) == []
        else:
            assert selected.verification_level == VerificationLevel.FULL_VERIFIED.value
            assert selected.metainfo_digest
            review = service.submit_review(
                units[0].id,
                expected_version=0,
                approved_candidate_id=selected.id,
                rejected_candidate_ids=(),
                manual_mappings=(),
                note=None,
                actor_kind="ADMIN",
                actor_id="synthetic-admin",
            )
            gate = service.refresh_execution_gate(units[0].id)
            assert gate.current and gate.eligible and not gate.client_check_required
            assert gate.candidate_id == selected.id and gate.review_revision_id == review.id
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
            if changed_on_plan:
                with pytest.raises(ApplicationError) as changed_torrent_error:
                    await service.create_execution_plan(
                        units[0].id,
                        target_root="target",
                        target_downloader_id="synthetic-target",
                    )
                assert changed_torrent_error.value.code == "EXECUTION_PLAN_TORRENT_CHANGED"
                assert calls == [
                    "/api/v1/torrents",
                    "/api/v1/torrents/123/download",
                    "/api/v1/torrents/123/download",
                ]
                with factory() as session:
                    assert list(session.scalars(select(OperationJournal))) == []
                assert list(target.iterdir()) == []
                assert movie.read_bytes() == contents
                return
            plan = await service.create_execution_plan(
                units[0].id, target_root="target", target_downloader_id="synthetic-target"
            )
            assert plan.ready and plan.current and plan.hardlink_count == 1
            assert plan.client_fetch_count == 0
            assert list(target.iterdir()) == []
            linker = TaskLinkingCoordinator(
                factory,
                service,
                FilesystemOperationService(factory, SafeFilesystemGateway(data_root)),
                data_root=data_root,
            )
            linked = linker.execute(units[0].id, execution_plan_id=plan.id)
            replayed = linker.execute(units[0].id, execution_plan_id=plan.id)
            assert linked.linked_file_count == 1 and not linked.replayed
            assert replayed.replayed
            assert replayed.hardlink_journal_ids == linked.hardlink_journal_ids
            assert service.get_task(created.task.id).status == TaskStatus.ADDING.value
            assert (target / movie.name).stat(follow_symlinks=False).st_ino == original.st_ino
            with factory() as session:
                journals = list(session.scalars(select(OperationJournal)))
            assert len(journals) == 1
            assert journals[0].operation_type == CREATE_HARDLINK_OPERATION
            assert journals[0].status == "APPLIED"

            # Exercise the real post-link coordinators without a Docker daemon,
            # client socket, tracker or production media. Transmission must
            # verify even when the site's torrent was FULL_VERIFIED.
            capabilities = {
                "supports_force_recheck": True,
                "supports_verify_progress": True,
            }
            mappings = (PathMappingRule("/downloads", str(data_root)),)
            digest = downloader_execution_binding_digest(
                downloader_id="synthetic-target",
                version=1,
                kind=DownloaderKind.TRANSMISSION,
                enabled=True,
                connection_status=ProbeStatus.OK,
                path_mapping_status=ProbeStatus.OK,
                path_mappings=mappings,
                capabilities=capabilities,
            )
            transmission = _FakeTransmission()
            downloader = _FakeDownloaderProvider(
                TransmissionWriteBinding(
                    downloader_id="synthetic-target",
                    downloader_version=1,
                    binding_digest=digest,
                    path_mappings=mappings,
                    capabilities=capabilities,
                    adapter=transmission,
                    data_root=data_root,
                )
            )
            adding = TaskAddingCoordinator(
                factory,
                sites,
                downloader,
                QbittorrentAddOperationService(factory),
                TransmissionAddOperationService(factory),
                data_root=data_root,
            )
            verifier = TaskClientVerificationCoordinator(
                factory,
                downloader,
                QbittorrentRecheckOperationService(factory),
                TransmissionVerifyOperationService(factory),
                data_root=data_root,
            )
            seeder = TaskSeedingCoordinator(
                factory,
                downloader,
                QbittorrentStartOperationService(factory),
                TransmissionStartOperationService(factory),
                data_root=data_root,
            )
            added = await adding.execute(units[0].id, execution_plan_id=plan.id)
            assert added.status is TaskStatus.CLIENT_VERIFYING
            assert not added.skip_checking
            assert transmission.add_calls == 1
            checking = await verifier.execute(units[0].id, execution_plan_id=plan.id)
            assert checking.status is TaskStatus.CLIENT_VERIFYING
            assert checking.verification_outcome == "CHECKING"
            assert transmission.verify_calls == 1
            transmission.states[added.torrent_hash] = replace(
                transmission.states[added.torrent_hash],
                status=0,
                percent_done=1.0,
                recheck_progress=1.0,
            )
            verified = await verifier.execute(units[0].id, execution_plan_id=plan.id)
            assert verified.status is TaskStatus.SEEDING
            assert verified.verification_outcome == "VERIFIED"
            assert transmission.verify_calls == 1
            if start_response_lost:
                transmission.raise_after_start_apply_once = True
                with pytest.raises(ApplicationError) as lost:
                    await seeder.execute(units[0].id, execution_plan_id=plan.id)
                assert lost.value.code == "DOWNLOADER_UNAVAILABLE"
                assert transmission.start_calls == 1
                with factory() as session:
                    pending_start = list(
                        session.scalars(
                            select(OperationJournal).order_by(OperationJournal.created_at)
                        )
                    )[-1]
                assert pending_start.operation_type == TRANSMISSION_START_OPERATION
                assert pending_start.status == "INTENT_RECORDED"
                assert service.get_task(created.task.id).status == TaskStatus.SEEDING.value
            started = await seeder.execute(units[0].id, execution_plan_id=plan.id)
            assert started.status is TaskStatus.DONE
            assert started.recovered_after_unknown_result is start_response_lost
            assert transmission.start_calls == 1
            assert (await seeder.execute(units[0].id, execution_plan_id=plan.id)).replayed
            assert (await adding.execute(units[0].id, execution_plan_id=plan.id)).replayed
            assert (await verifier.execute(units[0].id, execution_plan_id=plan.id)).replayed
            assert (
                transmission.add_calls == transmission.verify_calls == transmission.start_calls == 1
            )
            with factory() as session:
                completed_journals = list(
                    session.scalars(select(OperationJournal).order_by(OperationJournal.created_at))
                )
            assert [item.operation_type for item in completed_journals] == [
                CREATE_HARDLINK_OPERATION,
                TRANSMISSION_ADD_OPERATION,
                TRANSMISSION_VERIFY_OPERATION,
                TRANSMISSION_START_OPERATION,
            ]
            assert all(item.status == "APPLIED" for item in completed_journals)
            # Completed-task release is explicit: remove the fake client task
            # with keep-data semantics before rolling back journal-owned links.
            # An unowned sibling must survive, as must the original media.
            unowned = target / "user-owned.txt"
            unowned.write_text("untouched", encoding="utf-8")
            cancellation = TaskCancellationCoordinator(
                factory,
                downloader,
                QbittorrentRemoveOperationService(factory),
                FilesystemOperationService(factory, SafeFilesystemGateway(data_root)),
                TransmissionRemoveOperationService(factory),
            )
            released = await cancellation.release_completed(created.task.id)
            repeated_release = await cancellation.release_completed(created.task.id)
            assert released.status is TaskStatus.DONE
            assert repeated_release.replayed
            assert repeated_release.remove_journal_id == released.remove_journal_id
            assert transmission.remove_calls == 1
            assert added.torrent_hash not in transmission.states
            assert not (target / movie.name).exists()
            assert unowned.read_text(encoding="utf-8") == "untouched"
            with factory() as session:
                released_journals = list(session.scalars(select(OperationJournal)))
            owned_links = [
                item
                for item in released_journals
                if item.operation_type == CREATE_HARDLINK_OPERATION
            ]
            removals = [
                item for item in released_journals if item.operation_type == "TRANSMISSION_REMOVE"
            ]
            assert len(owned_links) == len(removals) == 1
            assert owned_links[0].status == "ROLLED_BACK"
            assert removals[0].status == "APPLIED"
            assert removals[0].intent["delete_local_data"] is False

        after = movie.stat(follow_symlinks=False)
        assert (original.st_dev, original.st_ino, original.st_size, original.st_mtime_ns) == (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        )
        assert movie.read_bytes() == contents
        assert calls == [
            "/api/v1/torrents",
            "/api/v1/torrents/123/download",
            *(["/api/v1/torrents/123/download"] * 2 if valid_metainfo else []),
        ]
    finally:
        engine.dispose()
