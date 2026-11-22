"""监控中心 · 做种健康巡查接口的基础契约测试。"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.api.dependencies import CSRF_COOKIE
from backend.app.config import AppSettings
from backend.app.main import create_app

_PASSWORD = "synthetic correct horse battery staple"


def _client(tmp_path: Path) -> TestClient:
    settings = AppSettings(
        config_dir=(tmp_path / "config").resolve(),
        data_dir=(tmp_path / "data").resolve(),
        seeding_health_enabled=False,
    )
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    app: FastAPI = create_app(settings=settings)
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


def test_summary_requires_authentication(tmp_path: Path) -> None:
    client = _client(tmp_path)
    try:
        client.cookies.clear()
        assert client.get("/api/v1/monitor/seeding-health/summary").status_code == 401
    finally:
        client.__exit__(None, None, None)


def test_empty_state_shape(tmp_path: Path) -> None:
    client = _client(tmp_path)
    try:
        summary = client.get("/api/v1/monitor/seeding-health/summary")
        assert summary.status_code == 200
        payload = summary.json()
        assert payload["watched"] == 0
        assert payload["abnormal"] == 0
        assert payload["last_run"] is None
        assert payload["stale"] is True
        assert payload["by_kind"] == {}

        issues = client.get("/api/v1/monitor/seeding-health/issues")
        assert issues.status_code == 200
        assert issues.json() == {"items": [], "total": 0, "page": 1, "page_size": 20}
    finally:
        client.__exit__(None, None, None)


def test_pagination_validation(tmp_path: Path) -> None:
    client = _client(tmp_path)
    try:
        response = client.get("/api/v1/monitor/seeding-health/issues", params={"page_size": 500})
        assert response.status_code == 422
        assert response.json()["code"] == "SEEDING_HEALTH_PAGE_INVALID"
    finally:
        client.__exit__(None, None, None)


def test_unknown_issue_returns_404(tmp_path: Path) -> None:
    client = _client(tmp_path)
    try:
        response = client.get("/api/v1/monitor/seeding-health/issues/does-not-exist")
        assert response.status_code == 404
        assert response.json()["code"] == "SEEDING_HEALTH_NOT_FOUND"
    finally:
        client.__exit__(None, None, None)


def test_manual_run_without_downloaders_finishes(tmp_path: Path) -> None:
    client = _client(tmp_path)
    try:
        response = client.post("/api/v1/monitor/seeding-health/runs", headers=_csrf(client))
        assert response.status_code == 202
        body = response.json()
        assert body["accepted"] is True
        assert body["run_id"]

        runs = client.get("/api/v1/monitor/seeding-health/runs")
        assert runs.status_code == 200
        items = runs.json()
        assert len(items) == 1
        assert items[0]["trigger"] == "MANUAL"
        assert items[0]["status"] == "OK"
    finally:
        client.__exit__(None, None, None)


def test_policy_roundtrip(tmp_path: Path) -> None:
    client = _client(tmp_path)
    try:
        saved = client.put(
            "/api/v1/monitor/seeding-health/policies/hdtime",
            json={"min_seeding_hours": 72},
            headers=_csrf(client),
        )
        assert saved.status_code == 200
        assert saved.json()["min_seeding_hours"] == 72
        assert saved.json()["source"] == "MANUAL"

        listed = client.get("/api/v1/monitor/seeding-health/policies")
        assert listed.status_code == 200
        assert listed.json()[0]["site_key"] == "hdtime"

        invalid = client.put(
            "/api/v1/monitor/seeding-health/policies/hdtime",
            json={"min_seeding_hours": 9000},
            headers=_csrf(client),
        )
        assert invalid.status_code == 422
    finally:
        client.__exit__(None, None, None)


def test_runtime_settings_roundtrip(tmp_path: Path) -> None:
    client = _client(tmp_path)
    try:
        current = client.get("/api/v1/monitor/seeding-health/settings")
        assert current.status_code == 200
        assert current.json()["site_probe_enabled"] is False

        saved = client.put(
            "/api/v1/monitor/seeding-health/settings",
            json={"site_probe_enabled": True},
            headers=_csrf(client),
        )
        assert saved.status_code == 200
        assert saved.json()["site_probe_enabled"] is True

        reloaded = client.get("/api/v1/monitor/seeding-health/settings")
        assert reloaded.json()["site_probe_enabled"] is True
    finally:
        client.__exit__(None, None, None)


def test_repair_preview_unknown_issue(tmp_path: Path) -> None:
    client = _client(tmp_path)
    try:
        response = client.post(
            "/api/v1/monitor/seeding-health/issues/missing/repair-preview",
            json={"action": "RETRIEVE_TORRENT"},
            headers=_csrf(client),
        )
        assert response.status_code == 404
    finally:
        client.__exit__(None, None, None)
