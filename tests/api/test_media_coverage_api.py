from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from backend.app.api.dependencies import (
    AccessPrincipal,
    require_admin_csrf_principal,
    require_admin_principal,
)
from backend.app.config import AppSettings
from backend.app.infrastructure.persistence.models import (
    MediaCoverageMovie,
    MediaCoverageRun,
    utc_now,
)
from backend.app.main import create_app


def test_media_coverage_api_reads_snapshot_and_rejects_bad_scope(tmp_path: Path) -> None:
    settings = AppSettings(
        config_dir=(tmp_path / "config").resolve(),
        data_dir=(tmp_path / "data").resolve(),
    )
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    app = create_app(settings=settings)
    principal = AccessPrincipal(kind="admin_session", subject_id="synthetic")
    app.dependency_overrides[require_admin_principal] = lambda: principal
    app.dependency_overrides[require_admin_csrf_principal] = lambda: principal

    with TestClient(app, base_url="https://testserver") as client:
        factory = app.state.runtime.session_factory
        with factory() as session:
            session.add(
                MediaCoverageRun(
                    id="run-a",
                    scope_fingerprint="synthetic",
                    scope_label="library",
                    status="COMPLETED",
                    phase="COMPLETED",
                    started_at=utc_now(),
                    finished_at=utc_now(),
                    movie_count=2,
                    site_count=1,
                    watched_count=1,
                    identity_resolved_count=1,
                    degraded_sites=[],
                )
            )
            session.commit()
            session.add(
                MediaCoverageMovie(
                    run_id="run-a",
                    movie_key="key-zero",
                    display_title="Beta.Movie.2021.720p",
                    year=2021,
                    resolution="720p",
                    external_ids=[],
                    relative_path_hint="Beta.Movie.2021.720p.mkv",
                    coverage_count=0,
                    site_keys=[],
                    bucket="ZERO",
                    match_level="TITLE_YEAR",
                    confidence="MEDIUM",
                    suggestion="ADD_SEED",
                    evidence={},
                )
            )
            session.commit()

        summary = client.get("/api/v1/monitor/coverage/summary")
        assert summary.status_code == 200
        assert summary.json()["total_movies"] == 1
        assert summary.json()["buckets"]["ZERO"] == 1

        movies = client.get("/api/v1/monitor/coverage/movies", params={"bucket": "ZERO"})
        assert movies.status_code == 200
        assert movies.json()["total"] == 1

        exported = client.get("/api/v1/monitor/coverage/export", params={"bucket": "ZERO"})
        assert exported.status_code == 200
        assert "Beta.Movie.2021.720p.mkv" in exported.text

        runs = client.get("/api/v1/monitor/coverage/runs")
        assert runs.status_code == 200
        assert runs.json()[0]["id"] == "run-a"

        invalid = client.post("/api/v1/monitor/coverage/runs", json={"selection_token": "bad"})
        assert invalid.status_code == 422
        assert invalid.json()["code"] == "MEDIA_COVERAGE_INVALID_SCOPE"
