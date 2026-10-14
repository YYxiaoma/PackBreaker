"""Seven production-adapter profiles can enter normal, unmarked unpack executions.

All tests use a synthetic SQLite database, temporary files and no external
site/download-client RPC. This verifies registration and admission, not real E2E.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.app.domain.site_config import SiteKind
from backend.app.infrastructure.persistence.models import UnpackExecution
from tests.api.test_unpack_v2 import (
    _authenticated_client,
    _create_ready_downloader,
    _csrf,
)

_NEW_KINDS = (
    SiteKind.PTERCLUB,
    SiteKind.AUDIENCES,
    SiteKind.SPRING_SUNDAY,
    SiteKind.HDDOLBY,
    SiteKind.U2,
    SiteKind.TANGPT,
    SiteKind.CARPT,
)


@pytest.mark.parametrize("kind", _NEW_KINDS)
def test_seven_supported_sites_start_normal_manual_unpack_without_acceptance_marker(
    tmp_path: Path, kind: SiteKind
) -> None:
    client, app = _authenticated_client(tmp_path)
    try:
        downloader_id = _create_ready_downloader(app)
        created = client.post(
            "/api/v1/sites",
            headers=_csrf(client),
            json={
                "name": f"synthetic-{kind.value}",
                "type": kind.value,
                "credential": {
                    "kind": "COOKIE",
                    "value": "synthetic-test-session-not-a-real-cookie",
                },
            },
        )
        assert created.status_code == 201, created.text
        site_id = created.json()["id"]

        # Explicit site enable is required; importing a Cookie alone never
        # authorizes a task to run.
        source = app.state.settings.data_dir / f"source-{kind.value}"
        source.mkdir()
        (source / "Synthetic.2026.mkv").write_bytes(b"synthetic test data only")
        output = app.state.settings.data_dir / f"output-{kind.value}"
        definition = client.post(
            "/api/v1/unpack/definitions",
            headers=_csrf(client),
            json={
                "name": f"Synthetic {kind.value} unpack",
                "trigger_kind": "MANUAL",
                "source_kind": "DIRECTORY",
                "execution_scope_kind": "ALL_MATCHING_MEDIA",
                "source_config": {"directory_path": str(source)},
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
                "auto_match_threshold_bps": 10000,
            },
        )
        assert definition.status_code == 201, definition.text
        definition_id = definition.json()["id"]
        action_url = f"/api/v1/unpack/definitions/{definition_id}/actions"

        blocked = client.post(action_url, headers=_csrf(client), json={"action": "run"})
        assert blocked.status_code == 409
        assert blocked.json()["code"] == "UNPACK_SITE_NOT_READY"

        enabled = client.post(
            f"/api/v1/sites/{site_id}/actions",
            headers={**_csrf(client), "If-Match": '"1"'},
            json={"action": "enable"},
        )
        assert enabled.status_code == 200, enabled.text
        assert enabled.json()["enabled"] is True

        result = client.post(action_url, headers=_csrf(client), json={"action": "run"})
        assert result.status_code == 200, result.text
        execution_id = result.json()["execution_id"]
        assert execution_id
        with app.state.runtime.session_factory() as session:
            execution = session.get(UnpackExecution, execution_id)
            assert execution is not None
            assert execution.config_snapshot["site_ids"] == [site_id]
            assert "acceptance_mode" not in execution.config_snapshot
    finally:
        client.__exit__(None, None, None)
