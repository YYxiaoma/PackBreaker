"""Journal-backed qBittorrent task chain against an isolated ARM64 Docker client.

Run only from the dedicated CI namespace probe. Existing task fixtures synthesize
an authorized, already-linked task; no PT site, production service, or real source
is contacted. This covers ADDING -> SEEDING -> DONE and durable idempotent replay,
not the preceding authorization/LINKING stages or Web UI approval.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import select

from backend.app.application.downloaders import QbittorrentWriteBinding
from backend.app.domain.downloader import DownloaderCredential
from backend.app.domain.task_state import TaskStatus
from backend.app.infrastructure.adapters.downloaders import QbittorrentAdapter
from backend.app.infrastructure.persistence.models import OperationJournal, UnpackTask
from tests.application.test_task_adding import _AddingFixture

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


pytestmark = pytest.mark.skipif(
    not os.environ.get("PACKBREAKER_CI_REAL_QB_SANDBOX"),
    reason="requires isolated ARM64 Docker downloader namespace and synthetic CI-only data volume",
)


@pytest.fixture
def tmp_path() -> Path:
    """Pin the existing fixture to the CI-only data root mounted at /downloads."""
    raw = os.environ.get("PACKBREAKER_CI_REAL_QB_SANDBOX", "")
    sandbox = Path(raw)
    if (
        not raw
        or sandbox.name.startswith(".ci-arm64-real-downloaders.") is False
        or sandbox.parent != Path.cwd()
        or sandbox.is_symlink()
        or not sandbox.is_dir()
        or not (sandbox / "data").is_dir()
    ):
        raise ValueError("real downloader task CI requires its own existing isolated sandbox")
    return sandbox


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
        done = await adding_fixture.seeder.execute(
            adding_fixture.unit_id,
            execution_plan_id=adding_fixture.plan_id,
        )
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
            "QBITTORRENT_ADD",
            "QBITTORRENT_START",
        ]
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
