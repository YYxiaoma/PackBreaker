"""One synthetic task through real analysis/approval and native ARM64 clients.

The site and approving actor are isolated test doubles. All task/plan/journal
rows are created by the production application services, never hand-inserted.
Only the explicit native ARM64 CI sandbox permits the real-client variant.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.app.application.downloader_operations import (
    QbittorrentAddOperationService,
    QbittorrentRecheckOperationService,
    QbittorrentStartOperationService,
)
from backend.app.application.downloaders import QbittorrentWriteBinding, TransmissionWriteBinding
from backend.app.application.errors import ApplicationError
from backend.app.application.filesystem_operations import (
    CREATE_HARDLINK_OPERATION,
    FilesystemOperationService,
)
from backend.app.application.sites import EnabledSiteAdapter
from backend.app.application.task_adding import TaskAddingCoordinator
from backend.app.application.task_client_verification import TaskClientVerificationCoordinator
from backend.app.application.task_linking import TaskLinkingCoordinator
from backend.app.application.task_seeding import TaskSeedingCoordinator
from backend.app.application.tasks import TaskAnalysisService
from backend.app.application.transmission_operations import (
    TRANSMISSION_ADD_OPERATION,
    TRANSMISSION_START_OPERATION,
    TRANSMISSION_VERIFY_OPERATION,
    TransmissionAddOperationService,
    TransmissionStartOperationService,
    TransmissionVerifyOperationService,
)
from backend.app.domain.downloader import (
    DownloaderCredential,
    PathMappingRule,
    ProbeStatus,
    downloader_execution_binding_digest,
)
from backend.app.domain.site_adapter import TorrentDetails
from backend.app.domain.site_search import (
    CandidateMeta,
    SearchPage,
    SearchQuery,
    normalize_candidate_meta,
)
from backend.app.domain.task_state import TaskStatus
from backend.app.domain.task_units import SourceTaskFile, identify_task_units
from backend.app.domain.verification import DownloaderKind, VerificationLevel
from backend.app.infrastructure.adapters.downloaders import (
    QbittorrentAdapter,
    QbittorrentWriteAdapter,
    TransmissionAdapter,
    TransmissionWriteAdapter,
)
from backend.app.infrastructure.persistence.base import Base
from backend.app.infrastructure.persistence.database import (
    create_session_factory,
    create_sqlite_engine,
)
from backend.app.infrastructure.persistence.models import (
    Downloader,
    OperationJournal,
    SecretRecord,
    UnpackTask,
)
from backend.app.infrastructure.safe_filesystem import SafeFilesystemGateway
from backend.app.infrastructure.torrent_parser import parse_torrent
from tests.application.test_analysis import _FakeAdapter, _FakeSiteProvider
from tests.application.test_task_adding import (
    _FakeDownloaderProvider,
    _FakeQbittorrent,
    _v1_torrent,
)


class _SizedSyntheticSite(_FakeAdapter):
    def __init__(self, torrent: bytes, length: int) -> None:
        super().__init__("synthetic", torrent)
        self.length = length

    def _candidate(self) -> CandidateMeta:
        return normalize_candidate_meta(
            site_id=self.site_id,
            torrent_id="42",
            display_name="Movie.2026",
            total_size=self.length,
        )

    async def search(self, query: SearchQuery) -> SearchPage:
        self.search_calls.append(query)
        return SearchPage(self.site_id, query.page, (self._candidate(),), False, 1)

    async def fetch_details(self, torrent_id: str) -> TorrentDetails:
        return TorrentDetails(self._candidate())


async def _run_full_lifecycle(
    sandbox: Path,
    client: QbittorrentWriteAdapter | TransmissionWriteAdapter,
    *,
    remote_root: str,
    kind: DownloaderKind = DownloaderKind.QBITTORRENT,
) -> None:
    data_root = sandbox / "data"
    source_root = data_root / "source"
    target_root = data_root / "target"
    source_root.mkdir(parents=True)
    target_root.mkdir()
    # Deliberately differs from the prior journal-backed test in the *same*
    # isolated client: a torrent hash identifies content, not its save path.
    # Reusing that fixture's hash would exercise the existing-torrent guard
    # instead of this new task's ADD/START journal.
    content = b"fullchainarm64v1"
    if kind is DownloaderKind.TRANSMISSION:
        # A tiny synthetic file verifies before Transmission can expose its
        # mandatory CHECKING evidence. This isolated large fixture is never
        # taken from or written to a user's media directory.
        content = b"fullchainarm64tr" * (8 * 1024 * 1024)
    source = source_root / "Movie.2026.mkv"
    source.write_bytes(content)
    before = source.stat(follow_symlinks=False)
    unit = identify_task_units((SourceTaskFile(source.name, len(content)),))[0]
    torrent = _v1_torrent(source.name.encode(), content, piece_length=16384)
    meta = parse_torrent(torrent)
    assert meta.v1_info_hash is not None
    assert not await client.get_torrents((meta.v1_info_hash,)), (
        "single-task CI fixture must use a distinct torrent hash"
    )
    site_adapter = _SizedSyntheticSite(torrent, len(content))
    sites = _FakeSiteProvider((EnabledSiteAdapter("synthetic-site", 1, "synthetic", site_adapter),))

    engine = create_sqlite_engine(sandbox / "full-lifecycle.db")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    service = TaskAnalysisService(factory, sites, data_root=data_root)
    is_tr = kind is DownloaderKind.TRANSMISSION
    downloader_id = "synthetic-full-lifecycle-tr" if is_tr else "synthetic-full-lifecycle-qb"
    mappings = (PathMappingRule(remote_root, str(data_root)),)
    capabilities = {
        "client": "Transmission" if is_tr else "qBittorrent",
        "version": "4.1.3" if is_tr else "v5.2.3",
        "api_version": "6.0.0" if is_tr else "2.15.1",
        "supports_skip_checking": not is_tr,
        "supports_force_recheck": True,
        "supports_verify_progress": True,
    }
    binding_digest = downloader_execution_binding_digest(
        downloader_id=downloader_id,
        version=1,
        kind=kind,
        enabled=True,
        connection_status=ProbeStatus.OK,
        path_mapping_status=ProbeStatus.OK,
        path_mappings=mappings,
        capabilities=capabilities,
    )
    # The database's secret record is an inert sentinel satisfying the
    # configuration precondition. The *real* client credential remains solely
    # in the CI process environment and is never written to SQLite.
    with factory() as session:
        session.add(
            SecretRecord(
                id="synthetic-credential-id",
                kind="DOWNLOADER_CREDENTIAL",
                ciphertext="not-a-real-credential",
                key_version=1,
            )
        )
        session.flush()
        session.add(
            Downloader(
                id=downloader_id,
                name="Isolated Transmission" if is_tr else "Isolated qBittorrent",
                type=kind.value,
                base_url=(
                    "http://127.0.0.1:9091/transmission/rpc" if is_tr else "http://127.0.0.1:8080"
                ),
                secret_id="synthetic-credential-id",
                path_mappings=[{"remote_prefix": remote_root, "container_prefix": str(data_root)}],
                capabilities=capabilities,
                connection_status=ProbeStatus.OK.value,
                path_mapping_status=ProbeStatus.OK.value,
                enabled=True,
                version=1,
            )
        )
        session.commit()

    try:
        created = service.create_task(
            task_type="PACKAGE_UNPACK",
            source_downloader_id="synthetic-source",
            source_hash="synthetic-full-lifecycle-source",
            normalized_unit_key=unit.normalized_unit_key,
        )
        assert created.created and created.task.status == TaskStatus.PENDING.value
        snapshot = await service.analyze(created.task.id, source_root="source")
        assert snapshot.snapshot_digest and service.latest_preflight(created.task.id).current
        units = service.list_units(created.task.id)
        candidates = service.list_candidates(created.task.id)
        assert len(units) == 1 and len(candidates) == 1
        candidate = candidates[0]
        assert candidate.verification_level == VerificationLevel.FULL_VERIFIED.value
        assert not candidate.rejected

        with pytest.raises(ApplicationError) as blocked:
            service.refresh_execution_gate(units[0].id)
        assert blocked.value.code == "REVIEW_NOT_FOUND"
        with pytest.raises(ApplicationError) as foreign:
            service.submit_review(
                units[0].id,
                expected_version=0,
                approved_candidate_id=str(uuid4()),
                rejected_candidate_ids=(),
                manual_mappings=(),
                note=None,
                actor_kind="ADMIN",
                actor_id="ci-synthetic-admin",
            )
        assert foreign.value.code == "REVIEW_INPUT_INVALID"
        review = service.submit_review(
            units[0].id,
            expected_version=0,
            approved_candidate_id=candidate.id,
            rejected_candidate_ids=(),
            manual_mappings=(),
            note=None,
            actor_kind="ADMIN",
            actor_id="ci-synthetic-admin",
        )
        assert review.approved_candidate_id == candidate.id and not review.requires_reverification
        gate = service.refresh_execution_gate(units[0].id)
        assert gate.current and gate.eligible and gate.review_revision_id == review.id
        assert not gate.client_check_required
        plan = await service.create_execution_plan(
            units[0].id,
            target_root="target",
            target_downloader_id=downloader_id,
        )
        assert plan.current and plan.ready and not plan.blocked_reasons
        assert plan.hardlink_count == 1 and plan.client_fetch_count == 0
        assert plan.target_remote_save_path == f"{remote_root}/target"
        assert service.get_execution_plan(units[0].id).plan_digest == plan.plan_digest
        assert not list(target_root.iterdir())
        with factory() as session:
            assert list(session.scalars(select(OperationJournal))) == []

        linker = TaskLinkingCoordinator(
            factory,
            service,
            FilesystemOperationService(factory, SafeFilesystemGateway(data_root)),
            data_root=data_root,
        )
        linked = linker.execute(units[0].id, execution_plan_id=plan.id)
        replay_link = linker.execute(units[0].id, execution_plan_id=plan.id)
        assert linked.linked_file_count == 1 and not linked.replayed and replay_link.replayed
        assert linked.hardlink_journal_ids == replay_link.hardlink_journal_ids
        target_file = target_root / source.name
        assert target_file.stat(follow_symlinks=False).st_ino == before.st_ino
        binding: QbittorrentWriteBinding | TransmissionWriteBinding
        if is_tr:
            binding = TransmissionWriteBinding(
                downloader_id=downloader_id,
                downloader_version=1,
                binding_digest=binding_digest,
                path_mappings=mappings,
                capabilities=capabilities,
                data_root=data_root,
                adapter=cast(TransmissionWriteAdapter, client),
            )
        else:
            binding = QbittorrentWriteBinding(
                downloader_id=downloader_id,
                downloader_version=1,
                binding_digest=binding_digest,
                path_mappings=mappings,
                capabilities=capabilities,
                data_root=data_root,
                adapter=cast(QbittorrentWriteAdapter, client),
            )
        provider = _FakeDownloaderProvider(binding)
        adding = TaskAddingCoordinator(
            factory,
            sites,
            provider,
            QbittorrentAddOperationService(factory),
            TransmissionAddOperationService(factory),
            data_root=data_root,
        )
        first = await adding.execute(units[0].id, execution_plan_id=plan.id)
        assert first.status is (TaskStatus.CLIENT_VERIFYING if is_tr else TaskStatus.SEEDING)
        assert first.skip_checking is (not is_tr)
        replay_add = await adding.execute(units[0].id, execution_plan_id=plan.id)
        assert replay_add.replayed and replay_add.qbit_journal_id == first.qbit_journal_id
        verification_journal_id: str | None = None
        if is_tr:
            verifier = TaskClientVerificationCoordinator(
                factory,
                provider,
                QbittorrentRecheckOperationService(factory),
                TransmissionVerifyOperationService(factory),
                data_root=data_root,
            )
            first_check = await verifier.execute(units[0].id, execution_plan_id=plan.id)
            assert first_check.recheck_journal_id
            if first_check.status is TaskStatus.CLIENT_VERIFYING:
                for _ in range(60):
                    observed = await client.get_torrents((first.torrent_hash,))
                    assert len(observed) == 1
                    if observed[0].verification_complete:
                        break
                    await asyncio.sleep(1)
                else:
                    raise AssertionError("isolated Transmission did not finish mandatory verify")
                verified = await verifier.execute(units[0].id, execution_plan_id=plan.id)
            else:
                verified = first_check
            assert verified.status is TaskStatus.SEEDING
            assert verified.verification_outcome == "VERIFIED"
            assert verified.recheck_journal_id == first_check.recheck_journal_id
            verification_journal_id = verified.recheck_journal_id
        seeder = TaskSeedingCoordinator(
            factory,
            provider,
            QbittorrentStartOperationService(factory),
            TransmissionStartOperationService(factory),
            data_root=data_root,
        )
        try:
            done = await seeder.execute(units[0].id, execution_plan_id=plan.id)
        except ApplicationError as exc:
            if exc.code != "DOWNLOADER_START_NOT_CONFIRMED":
                raise
            for _ in range(45):
                states = await client.get_torrents((first.torrent_hash,))
                if len(states) == 1 and states[0].seeding:
                    break
                await asyncio.sleep(1)
            else:
                raise AssertionError(
                    "isolated real downloader did not enter verified seeding"
                ) from exc
            done = await seeder.execute(units[0].id, execution_plan_id=plan.id)
            assert done.recovered_after_unknown_result
        assert done.status is TaskStatus.DONE
        replay_done = await seeder.execute(units[0].id, execution_plan_id=plan.id)
        assert replay_done.replayed and replay_done.start_journal_id == done.start_journal_id
        with factory() as session:
            task = session.get(UnpackTask, created.task.id)
            assert task is not None and task.status == TaskStatus.DONE.value
            journals = list(
                session.scalars(select(OperationJournal).order_by(OperationJournal.created_at))
            )
            expected_operations = [CREATE_HARDLINK_OPERATION]
            if is_tr:
                expected_operations.extend(
                    [
                        TRANSMISSION_ADD_OPERATION,
                        TRANSMISSION_VERIFY_OPERATION,
                        TRANSMISSION_START_OPERATION,
                    ]
                )
                assert journals[2].id == verification_journal_id
            else:
                expected_operations.extend(["QBITTORRENT_ADD", "QBITTORRENT_START"])
            assert [item.operation_type for item in journals] == expected_operations
            assert all(item.status == "APPLIED" for item in journals)
        after = source.stat(follow_symlinks=False)
        assert (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) == (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        )
        assert target_file.stat(follow_symlinks=False).st_ino == before.st_ino
        assert target_file.read_bytes() == content
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_synthetic_single_task_lifecycle_with_fake_client(tmp_path: Path) -> None:
    await _run_full_lifecycle(tmp_path, _FakeQbittorrent(), remote_root="/downloads")


@pytest.mark.skipif(
    not os.environ.get("PACKBREAKER_CI_REAL_QB_SANDBOX"),
    reason="requires isolated native ARM64 qBittorrent Docker namespace",
)
@pytest.mark.asyncio
async def test_native_arm64_full_approved_qb_real_task_lifecycle() -> None:
    root = os.environ.get("PACKBREAKER_CI_REAL_QB_SANDBOX", "")
    sandbox = Path(root)
    assert (
        root
        and sandbox.name.startswith(".ci-arm64-real-downloaders.")
        and sandbox.parent == Path.cwd()
        and not sandbox.is_symlink()
        and (sandbox / "data").is_dir()
    )
    password = os.environ.get("PACKBREAKER_CI_REAL_QB_PASSWORD", "")
    if not password:
        raise ValueError("isolated qBittorrent temporary password is required")
    client = QbittorrentAdapter(
        "http://127.0.0.1:8080", DownloaderCredential(username="admin", password=password)
    )
    connection = await client.test_connection()
    assert connection.capabilities.version.lstrip("v").startswith("5.2.3")
    assert connection.capabilities.supports_skip_checking
    # A separate bind-mounted directory avoids collisions with the existing
    # journal-backed test in the same temporary qBittorrent container.
    dedicated = sandbox / "data" / "full-lifecycle"
    dedicated.mkdir(mode=0o700)
    await _run_full_lifecycle(dedicated, client, remote_root="/downloads/full-lifecycle/data")


@pytest.mark.skipif(
    not os.environ.get("PACKBREAKER_CI_REAL_TR_SANDBOX"),
    reason="requires isolated native ARM64 Transmission Docker namespace",
)
@pytest.mark.asyncio
async def test_native_arm64_full_approved_tr_real_task_lifecycle() -> None:
    raw = os.environ.get("PACKBREAKER_CI_REAL_TR_SANDBOX", "")
    sandbox = Path(raw)
    assert (
        raw
        and sandbox.name == "tr"
        and sandbox.parent.name.startswith(".ci-arm64-real-downloaders.")
        and sandbox.parent.parent == Path.cwd()
        and not sandbox.is_symlink()
        and not sandbox.parent.is_symlink()
        and (sandbox / "data").is_dir()
    )
    password = os.environ.get("PACKBREAKER_CI_REAL_TR_PASSWORD", "")
    if not password:
        raise ValueError("isolated Transmission temporary password is required")
    client = TransmissionAdapter(
        "http://127.0.0.1:9091/transmission/rpc",
        DownloaderCredential(username="packbreaker", password=password),
    )
    connection = await client.test_connection()
    assert connection.capabilities.version.startswith("4.1.3")
    assert not connection.capabilities.supports_skip_checking
    dedicated = sandbox / "data" / "full-lifecycle"
    dedicated.mkdir(mode=0o700)
    await _run_full_lifecycle(
        dedicated,
        client,
        remote_root="/downloads/full-lifecycle/data",
        kind=DownloaderKind.TRANSMISSION,
    )
