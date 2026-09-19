"""Loopback-only CI fixture for browser-to-real-FastAPI approval checks.

Never import from a production entrypoint: all files, credentials and site
responses are synthetic and the downloaders point at nonexistent .invalid hosts.
"""

import os
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

from backend.app.application.tasks import TaskAnalysisService
from backend.app.config import AppSettings
from backend.app.domain.task_units import SourceTaskFile, identify_task_units
from backend.app.infrastructure.persistence.models import Downloader
from backend.app.main import create_app
from tests.api.test_tasks import (
    _create_ready_qb_target,
    _create_ready_transmission_target,
    _create_task,
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
        (media_dir / media_name).write_bytes(content)
        unit = identify_task_units((SourceTaskFile(media_name, len(content)),))[0]
        _create_task(application, unit.normalized_unit_key)
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
            session.commit()
        yield


app.router.lifespan_context = isolated_fixture_lifespan
