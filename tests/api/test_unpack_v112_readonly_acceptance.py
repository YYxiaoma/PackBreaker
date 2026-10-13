"""Task-scoped CarPT read-only acceptance cannot unlock any production write."""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from fastapi import FastAPI
from sqlalchemy import func, select

from backend.app.application.unpack_acceptance import (
    READ_ONLY_ACCEPTANCE_MARKER,
    UnpackAcceptanceScope,
)
from backend.app.infrastructure.persistence.models import (
    Downloader,
    Site,
    UnpackExecution,
    UnpackExternalOperationJournal,
)
from tests.api.test_unpack_v2 import (
    _authenticated_client,
    _create_ready_downloader,
    _create_ready_site,
    _csrf,
)


def _scope_file(
    app: FastAPI,
    *,
    definition_id: str,
    site_id: str,
    client_id: str,
    media: Path,
    output: Path,
) -> Path:
    path = cast(Path, app.state.settings.config_dir) / "v112-unpack-readonly-acceptance.json"
    value = {
        "schema": "packbreaker-v112-readonly-site-acceptance-v1",
        "definition_id": definition_id,
        "site_id": site_id,
        "site_version": 1,
        "downloader_id": client_id,
        "source_directory": str(media.parent),
        "source_filename": media.name,
        "output_directory": str(output),
        "expires_at_utc": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
    }
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)
    return path


def test_only_exact_carpt_definition_can_run_readonly_and_never_materializes(
    tmp_path: Path,
) -> None:
    client, app = _authenticated_client(tmp_path)
    try:
        site_id = _create_ready_site(app)
        downloader_id = _create_ready_downloader(app)
        secret_id = app.state.secret_store.put(kind="SITE_COOKIE", value=b"synthetic=1")
        with app.state.runtime.session_factory() as session:
            site = session.get(Site, site_id)
            assert site is not None
            site.type = "CARPT"
            site.base_url = "https://carpt.net"
            site.credential_kind = "COOKIE"
            site.enabled = False
            site.secret_id = secret_id
            downloader = session.get(Downloader, downloader_id)
            assert downloader is not None
            downloader.type = "TRANSMISSION"
            session.commit()

        media_dir = app.state.settings.data_dir / "CarPT-synthetic"
        media_dir.mkdir()
        media = media_dir / "Hail.the.Judge.1994.mkv"
        media.write_bytes(b"no download client contact or real media here")
        output = app.state.settings.data_dir / "isolated-CarPT-COPY"
        token = "v1." + base64.urlsafe_b64encode(str(media_dir).encode()).decode().rstrip("=")
        scan = client.post(
            "/api/v1/unpack/source-scans",
            json={"selection_token": token, "file_filter": {"extensions": [".mkv"]}},
            headers=_csrf(client),
        )
        assert scan.status_code == 201, scan.text
        scan_id = scan.json()["id"]
        rows = client.get(f"/api/v1/unpack/source-scans/{scan_id}/items")
        assert rows.status_code == 200
        source_key = rows.json()["items"][0]["source_object_key"]
        selected = client.put(
            f"/api/v1/unpack/source-scans/{scan_id}/selection",
            json={"source_object_keys": [source_key], "selected": True},
            headers=_csrf(client),
        )
        assert selected.status_code == 200
        created = client.post(
            "/api/v1/unpack/definitions",
            headers=_csrf(client),
            json={
                "name": "isolated CarPT read-only acceptance",
                "trigger_kind": "MANUAL",
                "source_kind": "DIRECTORY",
                "execution_scope_kind": "SELECTED_MEDIA",
                "source_config": {"directory_path": str(media_dir)},
                "file_filter": {"extensions": [".mkv"]},
                "site_ids": [site_id],
                "output_config": {
                    "output_directory": str(output),
                    "storage_mode": "COPY",
                    "conflict_policy": "VERIFY_REUSE_OR_STOP",
                    "target_downloader_id": downloader_id,
                },
                "retry_enabled": False,
                "max_retries": 0,
                "auto_match_threshold_bps": 10_000,
                "source_scan_id": scan_id,
            },
        )
        assert created.status_code == 201, created.text
        definition_id = created.json()["id"]
        uri = f"/api/v1/unpack/definitions/{definition_id}/actions"
        unauthorized = client.post(uri, headers=_csrf(client), json={"action": "run"})
        assert unauthorized.status_code == 409
        assert unauthorized.json()["code"] == "UNPACK_SITE_NOT_READY"
        scope_path = _scope_file(
            app,
            definition_id=definition_id,
            site_id=site_id,
            client_id=downloader_id,
            media=media,
            output=output,
        )
        scope = UnpackAcceptanceScope(app.state.settings.config_dir)
        with app.state.runtime.session_factory() as session:
            from backend.app.infrastructure.persistence.models import UnpackDefinition

            definition = session.get(UnpackDefinition, definition_id)
            assert definition is not None and scope.authorize_definition(session, definition)
        accepted = client.post(uri, headers=_csrf(client), json={"action": "run"})
        assert accepted.status_code == 200, accepted.text
        execution_id = accepted.json()["execution_id"]
        assert execution_id
        with app.state.runtime.session_factory() as session:
            execution = session.get(UnpackExecution, execution_id)
            assert execution is not None
            assert execution.config_snapshot["acceptance_mode"] == READ_ONLY_ACCEPTANCE_MARKER
            assert scope.authorize_execution(session, execution)
            assert session.scalar(select(func.count(UnpackExternalOperationJournal.id))) == 0
            assert session.get(Site, site_id).enabled is False
        # Normal site registry remains disabled; only the exact acceptance execution can bind.
        assert not any(s.config_id == site_id for s in app.state.site_service.enabled_adapters())
        acceptance_bindings = app.state.site_service.bindings_for_unpack_execution(execution_id)
        assert len(acceptance_bindings) == 1
        assert acceptance_bindings[0].config_id == site_id

        # Revocation cannot accidentally re-enable filesystem or downloader operations.
        scope_path.unlink()
        assert not any(
            s.config_id == site_id
            for s in app.state.site_service.bindings_for_unpack_execution(execution_id)
        )
        assert (
            app.state.unpack_materialization_service.materialize_next_batch(
                execution_id
            ).processed_count
            == 0
        )
        assert not output.exists()
        assert (
            asyncio.run(
                app.state.unpack_auxiliary_service.advance_next_batch(execution_id)
            ).processed_count
            == 0
        )
        assert (
            asyncio.run(
                app.state.unpack_seeding_service.advance_next_batch(execution_id)
            ).processed_count
            == 0
        )
        with app.state.runtime.session_factory() as session:
            assert session.scalar(select(func.count(UnpackExternalOperationJournal.id))) == 0
        assert media.read_bytes() == b"no download client contact or real media here"
    finally:
        client.__exit__(None, None, None)


def test_acceptance_file_rejects_world_readable_symlink_and_scope_drift(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    scope = UnpackAcceptanceScope(config)
    assert scope._read() is None
    path = scope.path
    payload = {
        "schema": "packbreaker-v112-readonly-site-acceptance-v1",
        "definition_id": "one",
        "site_id": "two",
        "site_version": 1,
        "downloader_id": "three",
        "source_directory": "/data/source",
        "source_filename": "Hail.mkv",
        "output_directory": "/data/isolated",
        "expires_at_utc": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o644)
    assert scope._read() is None
    path.chmod(0o600)
    assert scope._read() is not None
    payload["expires_at_utc"] = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert scope._read() is None
    payload["expires_at_utc"] = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.unlink()
    other = config / "real-file"
    other.write_text(json.dumps(payload), encoding="utf-8")
    other.chmod(0o600)
    path.symlink_to(other)
    assert scope._read() is None
