"""API-level guards for the destructive movie dedup endpoints."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from backend.app.api.dependencies import CSRF_COOKIE
from backend.app.config import AppSettings
from backend.app.main import create_app

_PASSWORD = "synthetic correct horse battery staple"


def _client(tmp_path: Path) -> TestClient:
    config = (tmp_path / "config").resolve()
    data = (tmp_path / "data").resolve()
    config.mkdir(mode=0o700)
    data.mkdir()
    (config / "torrents").mkdir(mode=0o700)
    app = create_app(settings=AppSettings(config_dir=config, data_dir=data))
    client = TestClient(app, base_url="https://testserver")
    client.__enter__()
    assert (
        client.post(
            "/api/v1/auth/setup", json={"username": "admin", "password": _PASSWORD}
        ).status_code
        == 201
    )
    assert (
        client.post(
            "/api/v1/auth/login", json={"username": "admin", "password": _PASSWORD}
        ).status_code
        == 200
    )
    return client


def _csrf(client: TestClient) -> dict[str, str]:
    token = client.cookies.get(CSRF_COOKIE)
    assert token is not None
    return {"X-CSRF-Token": token}


def test_dedup_execute_requires_csrf_and_idempotency_key(tmp_path: Path) -> None:
    client = _client(tmp_path)
    try:
        url = "/api/v1/movie-dedup/jobs/missing/execute"

        # No CSRF token: rejected before any business logic.
        response = client.post(url, json={"pair_ids": ["pair-1"]})
        assert response.status_code == 403
        assert response.json()["code"] == "CSRF_INVALID"

        # With CSRF but no idempotency key: 428.
        response = client.post(url, json={"pair_ids": ["pair-1"]}, headers=_csrf(client))
        assert response.status_code == 428
        assert response.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"

        # Oversized selection is rejected by the schema.
        response = client.post(
            url,
            json={"pair_ids": [f"pair-{index}" for index in range(501)]},
            headers={**_csrf(client), "Idempotency-Key": "test-key"},
        )
        assert response.status_code == 422
    finally:
        client.__exit__(None, None, None)


def test_dedup_reconcile_requires_csrf(tmp_path: Path) -> None:
    client = _client(tmp_path)
    try:
        response = client.post("/api/v1/movie-dedup/jobs/missing/reconcile")
        assert response.status_code == 403
        assert response.json()["code"] == "CSRF_INVALID"
    finally:
        client.__exit__(None, None, None)
