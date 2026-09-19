"""Journal-backed task chains against isolated native ARM64 Docker clients.

Run only from the dedicated CI namespace probe. Existing task fixtures synthesize
an authorized, already-linked task; no PT site, production service, or real source
is contacted. This covers ADDING -> DONE and durable idempotent replay,
not the preceding authorization/LINKING stages or Web UI approval.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

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
from backend.app.application.task_adding import TaskAddingCoordinator
from backend.app.application.task_client_verification import TaskClientVerificationCoordinator
from backend.app.application.task_linking import TaskLinkingCoordinator
from backend.app.application.task_seeding import TaskSeedingCoordinator
from backend.app.application.tasks import ExecutionPlanView
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
    ProbeStatus,
    downloader_execution_binding_digest,
)
from backend.app.domain.task_state import TaskStatus
from backend.app.domain.verification import DownloaderKind
from backend.app.infrastructure.adapters.downloaders import QbittorrentAdapter, TransmissionAdapter
from backend.app.infrastructure.persistence.models import OperationJournal, UnpackTask
from backend.app.infrastructure.persistence.task_analysis_repositories import (
    TaskExecutionPlanRepository,
)
from backend.app.infrastructure.safe_filesystem import SafeFilesystemGateway
from backend.app.infrastructure.torrent_parser import parse_torrent
from tests.application.test_task_adding import _AddingFixture, _FakeSiteProvider

pytest_plugins = ("tests.application.test_task_adding",)


@contextmanager
def _test_stage(name: str) -> Iterator[None]:
    try:
        yield
    except Exception as exc:
        error_code = getattr(exc, "code", None)
        safe_code = (
            error_code if isinstance(error_code, str) and error_code.isidentifier() else "unknown"
        )
        # The stage/type/code are sufficient for debugging without exposing
        # container paths, API response bodies or ephemeral credentials.
        print(
            f"::error file=tests/integration/test_arm64_real_task_chain.py::"
            f"isolated task chain stage={name} exception={type(exc).__name__} code={safe_code}",
            flush=True,
        )
        raise


def _link_approved_synthetic_task(fixture: _AddingFixture) -> str:
    """Execute real LINKING from the fixture's already approved plan, without inventing approval."""
    target = fixture.data_root / "target" / fixture.source_file.name
    assert target.is_file() and target.stat().st_ino == fixture.source_file.stat().st_ino
    # Undo only the fixture-created target link, never the synthetic source.
    target.unlink()
    assert not target.exists() and fixture.source_file.is_file()
    with fixture.factory() as session:
        plan = TaskExecutionPlanRepository(session).get(fixture.plan_id)
        task = session.get(UnpackTask, fixture.task_id)
        assert plan is not None and plan.ready and task is not None
        assert task.status == TaskStatus.ADDING.value
        assert task.version == plan.task_version + 2
        task.status = TaskStatus.AWAITING_CONFIRMATION.value
        task.version = plan.task_version
        task.checkpoint = {}
        session.commit()
        binding = fixture.downloader_provider.binding
        view = ExecutionPlanView(
            id=plan.id,
            plan_digest=plan.plan_digest,
            ready=True,
            current=True,
            current_reasons=(),
            target_root=plan.target_root,
            target_device=(fixture.data_root / "target").stat().st_dev,
            target_downloader_id=binding.downloader_id,
            target_downloader_version=binding.downloader_version,
            target_remote_save_path="/downloads/target",
            verification_level=plan.verification_level,
            client_check_required=plan.client_check_required,
            hardlink_count=1,
            client_fetch_count=0,
            create_directory_count=0,
            estimated_download_bytes_upper_bound=0,
            blocked_reasons=(),
            actions=(
                {
                    "torrent_path": fixture.source_file.name,
                    "kind": "HARDLINK",
                    "length": fixture.source_file.stat().st_size,
                    "source_relative_path": fixture.source_file.name,
                },
            ),
            execution_allowed=False,
            side_effects_started=False,
            created_at=plan.created_at,
        )

    class ApprovedPlanProvider:
        def get_execution_plan(self, unit_id: str) -> ExecutionPlanView:
            assert unit_id == fixture.unit_id
            return view

    linker = TaskLinkingCoordinator(
        fixture.factory,
        ApprovedPlanProvider(),
        FilesystemOperationService(fixture.factory, SafeFilesystemGateway(fixture.data_root)),
        data_root=fixture.data_root,
    )
    with _test_stage("approved-plan-real-linking"):
        linked = linker.execute(fixture.unit_id, execution_plan_id=fixture.plan_id)
        repeated = linker.execute(fixture.unit_id, execution_plan_id=fixture.plan_id)
    assert not linked.replayed and repeated.replayed
    assert linked.linked_file_count == 1 and len(linked.hardlink_journal_ids) == 1
    assert repeated.hardlink_journal_ids == linked.hardlink_journal_ids
    assert linked.client_fetch_count == 0
    assert (
        target.stat(follow_symlinks=False).st_ino
        == fixture.source_file.stat(follow_symlinks=False).st_ino
    )
    return linked.hardlink_journal_ids[0]


pytestmark = pytest.mark.skipif(
    not (
        os.environ.get("PACKBREAKER_CI_REAL_QB_SANDBOX")
        or os.environ.get("PACKBREAKER_CI_REAL_TR_SANDBOX")
    ),
    reason="requires isolated ARM64 Docker downloader namespace and synthetic CI-only data volume",
)


@pytest.fixture
def tmp_path() -> Path:
    """Pin the existing fixture to the CI-only data root mounted at /downloads."""
    raw = os.environ.get("PACKBREAKER_CI_REAL_QB_SANDBOX") or os.environ.get(
        "PACKBREAKER_CI_REAL_TR_SANDBOX", ""
    )
    sandbox = Path(raw)
    top_level = (
        sandbox.name.startswith(".ci-arm64-real-downloaders.") and sandbox.parent == Path.cwd()
    )
    nested = (
        sandbox.name == "tr"
        and sandbox.parent.name.startswith(".ci-arm64-real-downloaders.")
        and sandbox.parent.parent == Path.cwd()
        and not sandbox.parent.is_symlink()
    )
    if (
        not raw
        or not (top_level or nested)
        or sandbox.is_symlink()
        or not sandbox.is_dir()
        or not (sandbox / "data").is_dir()
    ):
        raise ValueError("real downloader task CI requires its own existing isolated sandbox")
    return sandbox


@pytest.mark.skipif(
    not os.environ.get("PACKBREAKER_CI_REAL_QB_SANDBOX"), reason="qBittorrent sandbox only"
)
@pytest.mark.asyncio
async def test_authorized_qb_task_journal_replays_without_duplicate_real_add(
    adding_fixture: _AddingFixture,
) -> None:
    password = os.environ.get("PACKBREAKER_CI_REAL_QB_PASSWORD", "")
    if not password:
        raise ValueError("isolated qBittorrent temporary password is required")
    adapter = QbittorrentAdapter(
        "http://127.0.0.1:8080",
        DownloaderCredential(username="admin", password=password),
    )
    connection = await adapter.test_connection()
    assert connection.capabilities.version.lstrip("v").startswith("5.2.3")
    assert connection.capabilities.supports_skip_checking
    binding = adding_fixture.downloader_provider.binding
    assert isinstance(binding, QbittorrentWriteBinding)
    adding_fixture.downloader_provider.binding = replace(binding, adapter=adapter)

    linked_journal_id = _link_approved_synthetic_task(adding_fixture)

    source = adding_fixture.source_file
    target = adding_fixture.data_root / "target" / source.name
    original = source.stat(follow_symlinks=False)
    assert (original.st_dev, original.st_ino) == (
        target.stat(follow_symlinks=False).st_dev,
        target.stat(follow_symlinks=False).st_ino,
    )

    with _test_stage("journal-backed-add"):
        first = await adding_fixture.coordinator.execute(
            adding_fixture.unit_id,
            execution_plan_id=adding_fixture.plan_id,
        )
    assert first.status is TaskStatus.SEEDING and first.skip_checking
    assert not first.replayed
    before_replay = await adapter.get_torrents((first.torrent_hash,))
    assert len(before_replay) == 1 and before_replay[0].verification_complete

    with _test_stage("journal-backed-add-replay"):
        replay = await adding_fixture.coordinator.execute(
            adding_fixture.unit_id,
            execution_plan_id=adding_fixture.plan_id,
        )
    assert replay.replayed and replay.qbit_journal_id == first.qbit_journal_id
    all_torrents = await adapter.get_torrents((first.torrent_hash,))
    assert len(all_torrents) == 1

    with _test_stage("journal-backed-start"):
        try:
            done = await adding_fixture.seeder.execute(
                adding_fixture.unit_id,
                execution_plan_id=adding_fixture.plan_id,
            )
        except ApplicationError as exc:
            if exc.code != "DOWNLOADER_START_NOT_CONFIRMED":
                raise
            # The real client may acknowledge start before switching out of
            # stoppedUP. Do not issue a second start while its result is unknown:
            # first observe the real client, then let the journal reconcile.
            for _ in range(45):
                latest = await adapter.get_torrents((first.torrent_hash,))
                if len(latest) == 1 and latest[0].seeding:
                    break
                await asyncio.sleep(1)
            else:
                raise AssertionError("real qBittorrent never entered verified seeding") from exc
            done = await adding_fixture.seeder.execute(
                adding_fixture.unit_id,
                execution_plan_id=adding_fixture.plan_id,
            )
            assert done.recovered_after_unknown_result
    assert done.status is TaskStatus.DONE
    with _test_stage("journal-backed-start-replay"):
        repeated_done = await adding_fixture.seeder.execute(
            adding_fixture.unit_id,
            execution_plan_id=adding_fixture.plan_id,
        )
    assert repeated_done.replayed and repeated_done.start_journal_id == done.start_journal_id
    with adding_fixture.factory() as session:
        task = session.get(UnpackTask, adding_fixture.task_id)
        assert task is not None and task.status == TaskStatus.DONE.value
        journals = list(
            session.scalars(select(OperationJournal).order_by(OperationJournal.created_at))
        )
        assert [entry.operation_type for entry in journals] == [
            CREATE_HARDLINK_OPERATION,
            "QBITTORRENT_ADD",
            "QBITTORRENT_START",
        ]
        assert journals[0].id == linked_journal_id
        assert all(entry.status == "APPLIED" for entry in journals)

    after = source.stat(follow_symlinks=False)
    assert (original.st_dev, original.st_ino, original.st_size, original.st_mtime_ns) == (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    )
    assert target.stat(follow_symlinks=False).st_ino == original.st_ino
    assert target.read_bytes() == source.read_bytes()


@pytest.mark.skipif(
    not os.environ.get("PACKBREAKER_CI_REAL_TR_SANDBOX"), reason="Transmission sandbox only"
)
@pytest.mark.asyncio
async def test_authorized_transmission_task_journal_verifies_then_seeds_real_client(
    adding_fixture: _AddingFixture,
) -> None:
    """Exercise real RPC and journal without skipping Transmission's mandatory verify."""
    with _test_stage("tr-preflight-connect"):
        password = os.environ.get("PACKBREAKER_CI_REAL_TR_PASSWORD", "")
        if not password:
            raise ValueError("isolated Transmission temporary password is required")
        adapter = TransmissionAdapter(
            "http://127.0.0.1:9091/transmission/rpc",
            DownloaderCredential(username="packbreaker", password=password),
        )
        connection = await adapter.test_connection()
        assert connection.capabilities.version.startswith("4.1.3")
        assert not connection.capabilities.supports_skip_checking

    old_binding = adding_fixture.downloader_provider.binding
    assert isinstance(old_binding, QbittorrentWriteBinding)
    capabilities = {
        "client": "Transmission",
        "version": "4.1.3",
        "api_version": "6.0.0",
        "supports_skip_checking": False,
        "supports_force_recheck": True,
        "supports_verify_progress": True,
    }
    binding_digest = downloader_execution_binding_digest(
        downloader_id=old_binding.downloader_id,
        version=old_binding.downloader_version,
        kind=DownloaderKind.TRANSMISSION,
        enabled=True,
        connection_status=ProbeStatus.OK,
        path_mapping_status=ProbeStatus.OK,
        path_mappings=old_binding.path_mappings,
        capabilities=capabilities,
    )
    adding_fixture.downloader_provider.binding = TransmissionWriteBinding(
        downloader_id=old_binding.downloader_id,
        downloader_version=old_binding.downloader_version,
        binding_digest=binding_digest,
        path_mappings=old_binding.path_mappings,
        capabilities=capabilities,
        adapter=adapter,
        data_root=adding_fixture.data_root,
    )
    with adding_fixture.factory() as session:
        plan = TaskExecutionPlanRepository(session).get(adding_fixture.plan_id)
        task = session.get(UnpackTask, adding_fixture.task_id)
        assert plan is not None and task is not None
        plan_payload = dict(plan.payload)
        plan_payload["target_downloader_binding_digest"] = binding_digest
        plan.payload = plan_payload
        checkpoint = dict(task.checkpoint)
        checkpoint["target_downloader_binding_digest"] = binding_digest
        task.checkpoint = checkpoint
        session.commit()

    adding = TaskAddingCoordinator(
        adding_fixture.factory,
        _FakeSiteProvider(adding_fixture.site_adapter),
        adding_fixture.downloader_provider,
        QbittorrentAddOperationService(adding_fixture.factory),
        TransmissionAddOperationService(adding_fixture.factory),
        data_root=adding_fixture.data_root,
    )
    verifier = TaskClientVerificationCoordinator(
        adding_fixture.factory,
        adding_fixture.downloader_provider,
        QbittorrentRecheckOperationService(adding_fixture.factory),
        TransmissionVerifyOperationService(adding_fixture.factory),
        data_root=adding_fixture.data_root,
    )
    seeder = TaskSeedingCoordinator(
        adding_fixture.factory,
        adding_fixture.downloader_provider,
        QbittorrentStartOperationService(adding_fixture.factory),
        TransmissionStartOperationService(adding_fixture.factory),
        data_root=adding_fixture.data_root,
    )
    linked_journal_id = _link_approved_synthetic_task(adding_fixture)
    source = adding_fixture.source_file
    target = adding_fixture.data_root / "target" / source.name
    original = source.stat(follow_symlinks=False)
    with _test_stage("tr-preflight-file"):
        assert original.st_size >= 64 * 1024 * 1024
        assert original.st_ino == target.stat(follow_symlinks=False).st_ino

    with _test_stage("tr-journal-backed-add"):
        try:
            added = await adding.execute(
                adding_fixture.unit_id, execution_plan_id=adding_fixture.plan_id
            )
        except ApplicationError as exc:
            if exc.code == "DOWNLOADER_STATE_MISMATCH":
                meta = parse_torrent(adding_fixture.torrent_content)
                assert meta.v1_info_hash is not None
                observed = await adapter.get_torrents((meta.v1_info_hash,))
                with adding_fixture.factory() as session:
                    journal = session.scalar(
                        select(OperationJournal).where(
                            OperationJournal.operation_type == TRANSMISSION_ADD_OPERATION
                        )
                    )
                    assert journal is not None
                    expected_path = journal.intent.get("remote_save_path")
                    expected_tag = journal.intent.get("ownership_tag")
                assert isinstance(expected_path, str) and isinstance(expected_tag, str)
                state = observed[0] if len(observed) == 1 else None
                print(
                    "::error file=tests/integration/test_arm64_real_task_chain.py::"
                    f"tr add synthetic mismatch count={len(observed)} "
                    f"path_matches={state.download_dir == expected_path if state else False} "
                    f"label_matches={expected_tag in state.labels if state else False} "
                    f"stopped={state.stopped if state else False} "
                    f"status={state.status if state else 'unknown'} "
                    f"progress={state.percent_done if state else 'unknown'}",
                    flush=True,
                )
            raise
    with _test_stage("tr-add-state"):
        assert added.status is TaskStatus.CLIENT_VERIFYING and added.skip_checking is False
        states = await adapter.get_torrents((added.torrent_hash,))
        assert len(states) == 1 and states[0].stopped
    with _test_stage("tr-journal-backed-add-replay"):
        add_replay = await adding.execute(
            adding_fixture.unit_id, execution_plan_id=adding_fixture.plan_id
        )
    with _test_stage("tr-add-replay-state"):
        assert add_replay.replayed and add_replay.qbit_journal_id == added.qbit_journal_id

    with _test_stage("tr-journal-backed-verify"):
        first_check = await verifier.execute(
            adding_fixture.unit_id, execution_plan_id=adding_fixture.plan_id
        )
        assert first_check.recheck_journal_id
        if first_check.status is TaskStatus.CLIENT_VERIFYING:
            for _ in range(60):
                observed = await adapter.get_torrents((added.torrent_hash,))
                assert len(observed) == 1
                if observed[0].verification_complete:
                    break
                await asyncio.sleep(1)
            else:
                raise AssertionError("Transmission did not complete client verification")
            verified = await verifier.execute(
                adding_fixture.unit_id, execution_plan_id=adding_fixture.plan_id
            )
        else:
            verified = first_check
    with _test_stage("tr-verification-evidence"):
        if verified.status is not TaskStatus.SEEDING:
            print(
                f"::error file=tests/integration/test_arm64_real_task_chain.py::"
                f"tr verified state={verified.status.value} "
                f"outcome={verified.verification_outcome} "
                f"checking_observed={verified.checking_observed}",
                flush=True,
            )
        assert verified.status is TaskStatus.SEEDING
        assert verified.verification_outcome == "VERIFIED"
        assert verified.recheck_journal_id == first_check.recheck_journal_id

    with _test_stage("tr-journal-backed-start"):
        try:
            done = await seeder.execute(
                adding_fixture.unit_id, execution_plan_id=adding_fixture.plan_id
            )
        except ApplicationError as exc:
            if exc.code != "DOWNLOADER_START_NOT_CONFIRMED":
                raise
            for _ in range(45):
                observed = await adapter.get_torrents((added.torrent_hash,))
                if len(observed) == 1 and observed[0].seeding:
                    break
                await asyncio.sleep(1)
            else:
                raise AssertionError("Transmission did not enter verified seeding") from exc
            done = await seeder.execute(
                adding_fixture.unit_id, execution_plan_id=adding_fixture.plan_id
            )
            assert done.recovered_after_unknown_result
    with _test_stage("tr-start-state"):
        assert done.status is TaskStatus.DONE
    with _test_stage("tr-journal-backed-start-replay"):
        repeated = await seeder.execute(
            adding_fixture.unit_id, execution_plan_id=adding_fixture.plan_id
        )
    with _test_stage("tr-completion-and-file-invariants"):
        assert repeated.replayed and repeated.start_journal_id == done.start_journal_id
        with adding_fixture.factory() as session:
            task = session.get(UnpackTask, adding_fixture.task_id)
            assert task is not None and task.status == TaskStatus.DONE.value
            journals = list(
                session.scalars(select(OperationJournal).order_by(OperationJournal.created_at))
            )
            assert [item.operation_type for item in journals] == [
                CREATE_HARDLINK_OPERATION,
                TRANSMISSION_ADD_OPERATION,
                TRANSMISSION_VERIFY_OPERATION,
                TRANSMISSION_START_OPERATION,
            ]
            assert journals[0].id == linked_journal_id
            assert all(item.status == "APPLIED" for item in journals)
        after = source.stat(follow_symlinks=False)
        assert (original.st_dev, original.st_ino, original.st_size, original.st_mtime_ns) == (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        )
        assert target.stat(follow_symlinks=False).st_ino == original.st_ino
        assert target.read_bytes() == source.read_bytes()
