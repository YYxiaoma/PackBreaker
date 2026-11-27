from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from backend.app.api.dependencies import AccessPrincipal, require_admin_principal
from backend.app.config import AppSettings
from backend.app.domain.site_assets import site_asset_stat_date
from backend.app.infrastructure.persistence.models import Site, SiteStatisticsHistory
from backend.app.main import create_app


def test_site_asset_endpoints_are_read_only_and_cover_csv(tmp_path: Path) -> None:
    settings = AppSettings(
        config_dir=(tmp_path / "config").resolve(),
        data_dir=(tmp_path / "data").resolve(),
    )
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    app = create_app(settings=settings)
    app.dependency_overrides[require_admin_principal] = lambda: AccessPrincipal(
        kind="admin_session", subject_id="synthetic"
    )

    with TestClient(app, base_url="https://testserver") as client:
        today = site_asset_stat_date(datetime.now(UTC), settings.timezone)
        yesterday = today - timedelta(days=1)
        factory = app.state.runtime.session_factory
        with factory() as session:
            session.add(
                Site(
                    id="site-a",
                    name="站点A",
                    type="MTEAM",
                    base_url="https://example.invalid",
                    credential_kind="COOKIE",
                )
            )
            for stat_date, bonus in ((yesterday, 100.0), (today, 160.0)):
                session.add(
                    SiteStatisticsHistory(
                        site_id="site-a",
                        stat_date=stat_date,
                        ratio=2.0,
                        bonus=bonus,
                        seeding_points=bonus * 2,
                        bonus_per_hour=12.0,
                        seeding_count=8,
                        uploaded_bytes=800,
                        downloaded_bytes=400,
                        fetched_at=datetime.now(UTC),
                    )
                )
            session.commit()

        summary = client.get("/api/v1/monitor/site-assets/summary", params={"days": 7})
        assert summary.status_code == 200
        assert summary.json()["bonus_total"] == 160.0
        assert summary.json()["changes"]["bonus"] == 60.0

        trend = client.get("/api/v1/monitor/site-assets/trend", params={"days": 7})
        assert trend.status_code == 200
        assert [item["stat_date"] for item in trend.json()] == [
            yesterday.isoformat(),
            today.isoformat(),
        ]

        sites = client.get("/api/v1/monitor/site-assets/sites", params={"days": 7})
        assert sites.status_code == 200
        assert sites.json()["total"] == 1

        exported = client.get("/api/v1/monitor/site-assets/export", params={"days": 7})
        assert exported.status_code == 200
        assert "attachment;" in exported.headers["content-disposition"]
        assert "站点A" in exported.text
