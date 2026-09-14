from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from backend.app.api.dependencies import CSRF_COOKIE
from backend.app.config import AppSettings
from backend.app.main import create_app

_PASSWORD = "synthetic correct horse battery staple"


def _authenticated_client(tmp_path: Path) -> TestClient:
    settings = AppSettings(
        config_dir=(tmp_path / "config").resolve(),
        data_dir=(tmp_path / "data").resolve(),
    )
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    (settings.data_dir / "movies").mkdir()
    (settings.data_dir / "movies" / "movie.mkv").write_bytes(b"movie")
    app = create_app(settings=settings)
    client = TestClient(app, base_url="https://testserver")
    client.__enter__()
    assert client.post("/api/v1/auth/setup", json={"password": _PASSWORD}).status_code == 201
    assert client.post("/api/v1/auth/login", json={"password": _PASSWORD}).status_code == 200
    return client


def _csrf(client: TestClient) -> dict[str, str]:
    token = client.cookies.get(CSRF_COOKIE)
    assert token is not None
    return {"X-CSRF-Token": token}


def test_history_scan_api_requires_csrf_version_and_persists_incremental_result(
    tmp_path: Path,
) -> None:
    client = _authenticated_client(tmp_path)
    try:
        payload = {
            "root_path": "/data/movies",
            "media_kind": "MOVIE",
            "extensions": [".mkv"],
            "exclude_patterns": ["sample"],
        }
        assert client.post("/api/v1/history-scans", json=payload).status_code == 403
        created_response = client.post(
            "/api/v1/history-scans",
            headers=_csrf(client),
            json=payload,
        )
        assert created_response.status_code == 201
        assert created_response.headers["ETag"] == '"1"'
        created = created_response.json()
        scan_id = created["id"]
        assert created["root_path"] == "/data/movies"
        assert created["status"] == "READY"

        missing_match = client.post(
            f"/api/v1/history-scans/{scan_id}/actions",
            headers=_csrf(client),
            json={"action": "start"},
        )
        assert missing_match.status_code == 428

        started_response = client.post(
            f"/api/v1/history-scans/{scan_id}/actions",
            headers={**_csrf(client), "If-Match": '"1"'},
            json={"action": "start"},
        )
        assert started_response.status_code == 200
        assert started_response.headers["ETag"] == '"2"'

        scanned_response = client.post(
            f"/api/v1/history-scans/{scan_id}/actions",
            headers={**_csrf(client), "If-Match": '"2"'},
            json={"action": "scan", "limit": 10},
        )
        assert scanned_response.status_code == 200
        assert scanned_response.headers["ETag"] == '"3"'
        scanned = scanned_response.json()
        assert scanned["processed_count"] == 1
        assert scanned["has_more"] is False
        assert scanned["scan"]["status"] == "DONE"
        assert scanned["scan"]["new_count"] == 1

        listed = client.get("/api/v1/history-scans")
        assert listed.status_code == 200
        assert listed.json()["items"][0]["id"] == scan_id
    finally:
        client.__exit__(None, None, None)
