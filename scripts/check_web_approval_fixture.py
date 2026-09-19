"""Loopback-only CI fixture for browser-to-real-FastAPI approval checks.

Never import from a production entrypoint: all files, credentials and site
responses are synthetic and the downloaders point at nonexistent .invalid hosts.
"""

import os
import tempfile
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from backend.app.application.task_definitions import (
    TaskDefinitionCreate,
    TaskOutputPolicyCreate,
    TaskSourceCreate,
)
from backend.app.application.tasks import TaskAnalysisService
from backend.app.config import AppSettings
from backend.app.domain.task_definition import TaskDefinitionKind, TaskSourceKind
from backend.app.infrastructure.persistence.models import Downloader, Site
from backend.app.main import create_app
from tests.api.test_tasks import (
    _create_ready_qb_target,
    _create_ready_transmission_target,
    _FakeAdapter,
    _FakeSiteProvider,
    _v1_torrent,
)

if os.getenv("PACKBREAKER_CI_WEB_APPROVAL") != "1":
    raise RuntimeError("the browser approval fixture is CI-only")
root = Path(os.environ["PACKBREAKER_CI_WEB_APPROVAL_ROOT"])
if (
    not root.is_absolute()
    or root.parent != Path(tempfile.gettempdir())
    or not root.name.startswith(".ci-web-approval.")
    or root.is_symlink()
    or not root.is_dir()
):
    raise RuntimeError("browser approval fixture requires a new isolated CI directory")

settings = AppSettings(
    host="127.0.0.1",
    port=18081,
    config_dir=root / "config",
    data_dir=root / "data",
    task_driver_interval_seconds=3600,
    task_definition_driver_interval_seconds=3600,
    notification_driver_interval_seconds=3600,
    backup_driver_interval_seconds=3600,
)
settings.data_dir.mkdir(mode=0o700)
app = create_app(settings=settings)
original_lifespan = app.router.lifespan_context


@asynccontextmanager
async def isolated_fixture_lifespan(application):  # type: ignore[no-untyped-def]
    async with original_lifespan(application):
        media_dir = settings.data_dir / "web-review-source"
        media_dir.mkdir(mode=0o700)
        target_dir = settings.data_dir / "web-review-target"
        target_dir.mkdir(mode=0o700)
        content = b"synthetic-web-review-v1"
        media_name = "Movie.2026.mkv"
        media_file = media_dir / media_name
        media_file.write_bytes(content)
        snapshot = media_file.stat(follow_symlinks=False)
        application.state.task_analysis_service = TaskAnalysisService(
            application.state.runtime.session_factory,
            _FakeSiteProvider(
                _FakeAdapter(_v1_torrent(media_name.encode(), content, piece_length=16))
            ),
            data_root=settings.data_dir,
        )
        _create_ready_qb_target(application, settings)
        transmission_id = _create_ready_transmission_target(application, settings)
        # Only a synthetic credential in an isolated SecretStore. There is no
        # live client or PT endpoint; the browser must never start execution.
        secret_id = application.state.secret_store.put(
            kind="DOWNLOADER_CREDENTIAL",
            value=b'{"username":"ci","password":"synthetic-test-only","api_key":null}',
        )
        with application.state.runtime.session_factory() as session:
            target = session.get(Downloader, transmission_id)
            assert target is not None and target.base_url.endswith(".invalid:9091/transmission/rpc")
            target.secret_id = secret_id
            # The persisted site identifies the in-process fake used only for
            # analysis. Its .invalid endpoint is never contacted.
            now = datetime.now(UTC)
            session.add(
                Site(
                    id="cfg-fake",
                    name="隔离合成测试站",
                    type="MTEAM",
                    base_url="https://site.invalid",
                    credential_kind="API_KEY",
                    secret_id=None,
                    capabilities={},
                    connection_status="OK",
                    enabled=True,
                    version=1,
                    last_test_at=now,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()
        definition = application.state.task_definition_service.create_definition(
            TaskDefinitionCreate(
                name="隔离浏览器审批任务",
                kind=TaskDefinitionKind.MANUAL,
                site_id="cfg-fake",
                source=TaskSourceCreate(
                    kind=TaskSourceKind.DIRECTORY,
                    directory_path="web-review-source",
                    config={
                        "target_downloader_id": transmission_id,
                        "selected_files": [
                            {
                                "relative_path": media_name,
                                "size_bytes": snapshot.st_size,
                                "device": snapshot.st_dev,
                                "inode": snapshot.st_ino,
                                "mtime_ns": snapshot.st_mtime_ns,
                            }
                        ],
                    },
                ),
                output_policy=TaskOutputPolicyCreate(output_directory="web-review-target"),
            )
        )
        execution = await application.state.task_definition_execution_service.materialize_manual(
            definition.id, trace_id=str(uuid4())
        )
        assert len(execution.items) == 1
        assert execution.items[0].unpack_task_id is not None
        yield


app.router.lifespan_context = isolated_fixture_lifespan
