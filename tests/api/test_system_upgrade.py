from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.api.dependencies import CSRF_COOKIE
from backend.app.application.errors import ApplicationError
from backend.app.application.system_upgrades import (
    SystemUpgradeActionResult,
    SystemUpgradeStatus,
)
from backend.app.config import AppSettings
from backend.app.infrastructure.updater_protocol import UPDATER_PROTOCOL_VERSION, UpdaterStatus
from backend.app.main import create_app

_PASSWORD = "synthetic correct horse battery staple"
_DIGEST = "sha256:" + "8" * 64
_IMAGE = "ghcr.io/yyxiaoma/packbreaker@" + _DIGEST


class FakeUpgradeService:
    def __init__(self) -> None:
        self.execute_calls: list[dict[str, object]] = []
        self.helper = UpdaterStatus(
            protocol_version=UPDATER_PROTOCOL_VERSION,
            helper_version="0.1.2",
            phase="idle",
            message="ready",
        )

    async def status(self) -> SystemUpgradeStatus:
        return SystemUpgradeStatus(
            current_version="0.1.2",
            latest_version="0.1.3",
            update_available=True,
            target_tag="v0.1.3",
            target_image_digest=_DIGEST,
            immutable_image=_IMAGE,
            platform="linux/amd64",
            release_error_code=None,
            helper_available=True,
            helper_status=self.helper,
            can_upgrade=True,
            blocked_reasons=(),
        )

    async def execute(self, **kwargs: Any) -> SystemUpgradeActionResult:
        idempotency_key = kwargs.get("idempotency_key")
        if idempotency_key is None:
            raise ApplicationError(
                code="IDEMPOTENCY_KEY_REQUIRED",
                status=428,
                title="缺少幂等键",
                detail="synthetic",
            )
        self.execute_calls.append(dict(kwargs))
        accepted = UpdaterStatus(
            protocol_version=UPDATER_PROTOCOL_VERSION,
            helper_version="0.1.2",
            phase="accepted",
            message="accepted",
            request_id=str(idempotency_key),
            current_version="0.1.2",
            target_version="0.1.3",
            target_image=_IMAGE,
            backup_database_file="packbreaker-20260915T010101Z-1234abcd.db",
        )
        return SystemUpgradeActionResult(
            request_id=str(idempotency_key),
            current_version="0.1.2",
            target_version="0.1.3",
            target_image=_IMAGE,
            backup_database_file="packbreaker-20260915T010101Z-1234abcd.db",
            helper_status=accepted,
            idempotency_replayed=False,
        )


def _client(tmp_path: Path) -> tuple[TestClient, FastAPI, FakeUpgradeService]:
    config = (tmp_path / "config").resolve()
    data = (tmp_path / "data").resolve()
    config.mkdir(mode=0o700)
    data.mkdir()
    app = create_app(
        settings=AppSettings(
            config_dir=config,
            data_dir=data,
            backup_driver_interval_seconds=3600,
        )
    )
    client = TestClient(app, base_url="https://testserver")
    client.__enter__()
    assert client.post("/api/v1/auth/setup", json={"password": _PASSWORD}).status_code == 201
    assert client.post("/api/v1/auth/login", json={"password": _PASSWORD}).status_code == 200
    service = FakeUpgradeService()
    app.state.system_upgrade_service = service
    return client, app, service


def _csrf(client: TestClient) -> dict[str, str]:
    token = client.cookies.get(CSRF_COOKIE)
    assert token is not None
    return {"X-CSRF-Token": token}


def test_upgrade_status_is_no_store_and_exposes_immutable_target(tmp_path: Path) -> None:
    client, _app, _service = _client(tmp_path)
    try:
        response = client.get("/api/v1/system/upgrade")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        payload = response.json()
        assert payload["current_version"] == "0.1.2"
        assert payload["latest_version"] == "0.1.3"
        assert payload["target_image_digest"] == _DIGEST
        assert payload["immutable_image"] == _IMAGE
        assert payload["helper_status"]["phase"] == "idle"
        assert payload["can_upgrade"] is True
    finally:
        client.__exit__(None, None, None)


def test_upgrade_action_requires_csrf_and_idempotency_key_then_returns_202(tmp_path: Path) -> None:
    client, _app, service = _client(tmp_path)
    payload = {
        "action": "upgrade",
        "target_version": "0.1.3",
        "target_image_digest": _DIGEST,
    }
    try:
        no_csrf = client.post("/api/v1/system/upgrade/actions", json=payload)
        assert no_csrf.status_code == 403

        no_idempotency = client.post(
            "/api/v1/system/upgrade/actions",
            headers=_csrf(client),
            json=payload,
        )
        assert no_idempotency.status_code == 428
        assert no_idempotency.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"

        headers = {**_csrf(client), "Idempotency-Key": "upgrade-request-001"}
        accepted = client.post(
            "/api/v1/system/upgrade/actions",
            headers=headers,
            json=payload,
        )
        assert accepted.status_code == 202
        result = accepted.json()
        assert result["target_version"] == "0.1.3"
        assert result["target_image"] == _IMAGE
        assert result["helper_status"]["phase"] == "accepted"
        assert result["idempotency_replayed"] is False
        assert len(service.execute_calls) == 1
        assert service.execute_calls[0]["idempotency_key"] == "upgrade-request-001"
    finally:
        client.__exit__(None, None, None)
