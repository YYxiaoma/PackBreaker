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

        materialized_response = client.post(
            f"/api/v1/history-scans/{scan_id}/actions",
            headers={**_csrf(client), "If-Match": '"3"'},
            json={"action": "materialize", "limit": 10},
        )
        assert materialized_response.status_code == 200
        assert materialized_response.headers["ETag"] == '"3"'
        materialized = materialized_response.json()
        assert materialized["processed_count"] == 1
        assert materialized["task_created_count"] == 1
        assert materialized["task_reused_count"] == 0
        assert materialized["skipped_count"] == 0
        assert materialized["remaining_count"] == 0
        assert materialized["items"][0]["status"] == "MATERIALIZED"
        assert materialized["items"][0]["task_id"] is not None
        assert materialized["items"][0]["source_root"] == "movies"

        task_id = materialized["items"][0]["task_id"]
        assert task_id is not None
        replacement = tmp_path / "replacement.mkv"
        replacement.write_bytes(b"other")
        replacement.replace(tmp_path / "data" / "movies" / "movie.mkv")
        stale_analysis = client.post(
            f"/api/v1/tasks/{task_id}/actions",
            headers=_csrf(client),
            json={"action": "analyze", "source_root": "movies"},
        )
        assert stale_analysis.status_code == 409
        assert stale_analysis.json()["code"] == "HISTORY_TASK_SOURCE_CHANGED"
        task = client.get(f"/api/v1/tasks/{task_id}")
        assert task.status_code == 200
        assert task.json()["status"] == "PENDING"

        replay_response = client.post(
            f"/api/v1/history-scans/{scan_id}/actions",
            headers={**_csrf(client), "If-Match": '"3"'},
            json={"action": "materialize", "limit": 10},
        )
        assert replay_response.status_code == 200
        assert replay_response.json()["processed_count"] == 0
        assert replay_response.json()["remaining_count"] == 0
        task_results = client.get(f"/api/v1/history-scans/{scan_id}/tasks")
        assert task_results.status_code == 200
        task_result = task_results.json()["items"][0]
        assert task_result["relative_path"] == "movie.mkv"
        assert task_result["task_id"] == task_id
        assert task_result["task_status"] == "PENDING"
        assert task_result["analysis_eligible"] is True
        assert task_result["has_preflight"] is False

    finally:
        client.__exit__(None, None, None)


def test_history_scan_batch_analyze_derives_source_root_and_reports_per_task_failure(
    tmp_path: Path,
) -> None:
    client = _authenticated_client(tmp_path)
    try:
        created = client.post(
            "/api/v1/history-scans",
            headers=_csrf(client),
            json={
                "root_path": "/data/movies",
                "media_kind": "MOVIE",
                "extensions": [".mkv"],
                "exclude_patterns": [],
            },
        ).json()
        scan_id = created["id"]
        started = client.post(
            f"/api/v1/history-scans/{scan_id}/actions",
            headers={**_csrf(client), "If-Match": f'"{created["version"]}"'},
            json={"action": "start"},
        ).json()
        scanned = client.post(
            f"/api/v1/history-scans/{scan_id}/actions",
            headers={**_csrf(client), "If-Match": f'"{started["version"]}"'},
            json={"action": "scan", "limit": 10},
        ).json()
        materialized = client.post(
            f"/api/v1/history-scans/{scan_id}/actions",
            headers={**_csrf(client), "If-Match": f'"{scanned["scan"]["version"]}"'},
            json={"action": "materialize", "limit": 10},
        ).json()
        task_id = materialized["items"][0]["task_id"]
        assert task_id is not None

        without_csrf = client.post(
            f"/api/v1/history-scans/{scan_id}/tasks/actions",
            json={"action": "analyze", "task_ids": [task_id]},
        )
        assert without_csrf.status_code == 403

        foreign = client.post(
            f"/api/v1/history-scans/{scan_id}/tasks/actions",
            headers=_csrf(client),
            json={"action": "analyze", "task_ids": ["not-from-this-scan"]},
        )
        assert foreign.status_code == 409
        assert foreign.json()["code"] == "HISTORY_TASK_SELECTION_INVALID"

        analyzed = client.post(
            f"/api/v1/history-scans/{scan_id}/tasks/actions",
            headers=_csrf(client),
            json={"action": "analyze", "task_ids": [task_id]},
        )
        assert analyzed.status_code == 200
        result = analyzed.json()
        assert result["attempted_count"] == 1
        assert result["succeeded_count"] == 0
        assert result["failed_count"] == 1
        assert result["skipped_count"] == 0
        assert result["items"] == [
            {
                "task_id": task_id,
                "source_root": "movies",
                "attempted": True,
                "succeeded": False,
                "task_status": "RETRY",
                "error_code": "ANALYSIS_NO_ENABLED_SITES",
            }
        ]

        refreshed = client.get(f"/api/v1/history-scans/{scan_id}/tasks").json()["items"]
        assert refreshed[0]["task_status"] == "RETRY"
        assert refreshed[0]["analysis_eligible"] is True
        assert refreshed[0]["has_preflight"] is False

        retry_only = client.get(
            f"/api/v1/history-scans/{scan_id}/tasks",
            params={
                "task_status": "RETRY",
                "materialization_status": "MATERIALIZED",
                "query": "movie",
            },
        )
        assert retry_only.status_code == 200
        assert [item["task_id"] for item in retry_only.json()["items"]] == [task_id]
        pending_only = client.get(
            f"/api/v1/history-scans/{scan_id}/tasks",
            params={"task_status": "PENDING"},
        )
        assert pending_only.status_code == 200
        assert pending_only.json()["items"] == []
        missing_query = client.get(
            f"/api/v1/history-scans/{scan_id}/tasks",
            params={"query": "not-present"},
        )
        assert missing_query.status_code == 200
        assert missing_query.json()["items"] == []
    finally:
        client.__exit__(None, None, None)


def test_history_scan_api_exposes_episode_grouping_and_variant_counts(tmp_path: Path) -> None:
    client = _authenticated_client(tmp_path)
    try:
        season = tmp_path / "data" / "shows" / "Example Show" / "Season 01"
        season.mkdir(parents=True)
        (season / "01.1080p.WEB-DL.mkv").write_bytes(b"one")
        (season / "01.2160p.BluRay.mkv").write_bytes(b"two-longer")

        created = client.post(
            "/api/v1/history-scans",
            headers=_csrf(client),
            json={
                "root_path": "/data/shows",
                "media_kind": "EPISODE",
                "extensions": [".mkv"],
                "exclude_patterns": [],
            },
        ).json()
        scan_id = created["id"]
        started = client.post(
            f"/api/v1/history-scans/{scan_id}/actions",
            headers={**_csrf(client), "If-Match": f'"{created["version"]}"'},
            json={"action": "start"},
        ).json()
        scanned = client.post(
            f"/api/v1/history-scans/{scan_id}/actions",
            headers={**_csrf(client), "If-Match": f'"{started["version"]}"'},
            json={"action": "scan", "limit": 10},
        ).json()
        materialized = client.post(
            f"/api/v1/history-scans/{scan_id}/actions",
            headers={**_csrf(client), "If-Match": f'"{scanned["scan"]["version"]}"'},
            json={"action": "materialize", "limit": 10},
        )
        assert materialized.status_code == 200
        assert materialized.json()["task_created_count"] == 2

        items = client.get(f"/api/v1/history-scans/{scan_id}/tasks").json()["items"]
        assert len(items) == 2
        assert {item["episode_kind"] for item in items} == {"SEASON_EPISODE"}
        assert {item["episode_label"] for item in items} == {"S01E01"}
        assert {item["episode_season"] for item in items} == {1}
        assert {item["episode_start"] for item in items} == {1}
        assert {item["variant_count"] for item in items} == {2}
        assert len({item["episode_group_key"] for item in items}) == 1
        assert len({item["episode_variant_key"] for item in items}) == 2
    finally:
        client.__exit__(None, None, None)


def test_history_scan_cancel_preserves_cursor_and_allows_explicit_new_generation(
    tmp_path: Path,
) -> None:
    client = _authenticated_client(tmp_path)
    try:
        (tmp_path / "data" / "movies" / "second.mkv").write_bytes(b"second")
        created = client.post(
            "/api/v1/history-scans",
            headers=_csrf(client),
            json={
                "root_path": "/data/movies",
                "media_kind": "MOVIE",
                "extensions": [".mkv"],
                "exclude_patterns": [],
            },
        ).json()
        scan_id = created["id"]
        started = client.post(
            f"/api/v1/history-scans/{scan_id}/actions",
            headers={**_csrf(client), "If-Match": f'"{created["version"]}"'},
            json={"action": "start"},
        ).json()
        first = client.post(
            f"/api/v1/history-scans/{scan_id}/actions",
            headers={**_csrf(client), "If-Match": f'"{started["version"]}"'},
            json={"action": "scan", "limit": 1},
        ).json()
        assert first["scan"]["status"] == "SCANNING"
        assert first["scan"]["cursor"] == "movie.mkv"

        cancelled_response = client.post(
            f"/api/v1/history-scans/{scan_id}/actions",
            headers={**_csrf(client), "If-Match": f'"{first["scan"]["version"]}"'},
            json={"action": "cancel"},
        )
        assert cancelled_response.status_code == 200
        cancelled = cancelled_response.json()
        assert cancelled["status"] == "CANCELLED"
        assert cancelled["cursor"] == "movie.mkv"
        assert cancelled["discovered_count"] == 1

        blocked = client.post(
            f"/api/v1/history-scans/{scan_id}/actions",
            headers={**_csrf(client), "If-Match": f'"{cancelled["version"]}"'},
            json={"action": "scan", "limit": 1},
        )
        assert blocked.status_code == 409
        assert blocked.json()["code"] == "HISTORY_SCAN_STATE_INVALID"

        restarted_response = client.post(
            f"/api/v1/history-scans/{scan_id}/actions",
            headers={**_csrf(client), "If-Match": f'"{cancelled["version"]}"'},
            json={"action": "start"},
        )
        assert restarted_response.status_code == 200
        restarted = restarted_response.json()
        assert restarted["status"] == "SCANNING"
        assert restarted["generation"] == cancelled["generation"] + 1
        assert restarted["cursor"] is None
    finally:
        client.__exit__(None, None, None)
