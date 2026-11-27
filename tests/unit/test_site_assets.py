from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import cast

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.secrets import SecretStore
from backend.app.application.site_assets import SiteAssetsService
from backend.app.application.sites import SiteService
from backend.app.domain.site_adapter import SiteUserProfile
from backend.app.domain.site_assets import (
    aggregate_ratio,
    aggregate_site_assets,
    site_asset_stat_date,
)
from backend.app.infrastructure.persistence.base import Base
from backend.app.infrastructure.persistence.models import Site, SiteStatisticsHistory


def _factory() -> sessionmaker[Session]:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def _add_site(session: Session, site_id: str, name: str) -> None:
    session.add(
        Site(
            id=site_id,
            name=name,
            type="MTEAM",
            base_url="https://example.invalid",
            credential_kind="COOKIE",
        )
    )
    session.commit()


def _add_history(
    session: Session,
    site_id: str,
    stat_date: date,
    *,
    ratio: float,
    bonus: float,
    bonus_per_hour: float,
    seeding_count: int,
    uploaded: int,
    downloaded: int,
) -> None:
    session.add(
        SiteStatisticsHistory(
            site_id=site_id,
            stat_date=stat_date,
            ratio=ratio,
            bonus=bonus,
            seeding_points=bonus * 2,
            bonus_per_hour=bonus_per_hour,
            seeding_count=seeding_count,
            uploaded_bytes=uploaded,
            downloaded_bytes=downloaded,
            fetched_at=datetime.now(UTC),
        )
    )
    session.commit()


def test_stat_date_uses_application_timezone() -> None:
    observed = datetime(2026, 1, 1, 16, 30, tzinfo=UTC)
    assert site_asset_stat_date(observed, "Asia/Shanghai") == date(2026, 1, 2)


def test_aggregate_ratio_and_missing_sites_are_not_zero() -> None:
    assert aggregate_ratio(300, 100) == 3.0
    assert aggregate_ratio(100, 0) is None
    assert aggregate_ratio(None, 100) is None

    rows = {
        date(2026, 1, 1): [
            {
                "ratio": 2.0,
                "bonus": 10.0,
                "seeding_count": 5,
                "uploaded_bytes": 200,
                "downloaded_bytes": 100,
            },
            {
                "ratio": 4.0,
                "bonus": 20.0,
                "seeding_count": 7,
                "uploaded_bytes": 400,
                "downloaded_bytes": 100,
            },
        ],
        date(2026, 1, 2): [
            {
                "ratio": 3.0,
                "bonus": 30.0,
                "seeding_count": 9,
                "uploaded_bytes": 600,
                "downloaded_bytes": 200,
            },
        ],
    }
    result = aggregate_site_assets(rows)

    assert result[0].ratio == 3.0
    assert result[0].bonus == 30.0
    assert result[0].site_count == 2
    assert result[1].bonus == 30.0
    assert result[1].site_count == 1


def test_site_assets_read_service_aggregates_window() -> None:
    factory = _factory()
    today = site_asset_stat_date(datetime.now(UTC), "Asia/Shanghai")
    yesterday = today - timedelta(days=1)
    with factory() as session:
        _add_site(session, "site-a", "站点A")
        _add_site(session, "site-b", "站点B")
        _add_history(
            session,
            "site-a",
            yesterday,
            ratio=2.0,
            bonus=100,
            bonus_per_hour=10,
            seeding_count=5,
            uploaded=200,
            downloaded=100,
        )
        _add_history(
            session,
            "site-b",
            yesterday,
            ratio=4.0,
            bonus=200,
            bonus_per_hour=20,
            seeding_count=7,
            uploaded=400,
            downloaded=100,
        )
        _add_history(
            session,
            "site-a",
            today,
            ratio=3.0,
            bonus=180,
            bonus_per_hour=12,
            seeding_count=9,
            uploaded=600,
            downloaded=200,
        )

    service = SiteAssetsService(factory, timezone="Asia/Shanghai")
    summary = service.summary(7)

    assert summary.bonus_total == 180
    assert summary.bonus_per_hour_total == 12
    assert summary.seeding_count_total == 9
    assert summary.site_count == 1
    assert summary.incomplete_sites == ["站点B"]
    assert summary.changes["bonus"] == 80
    assert [item.site_name for item in summary.ratio_distribution] == ["站点A"]

    trend = service.trend(7)
    assert [item.site_count for item in trend] == [2, 1]
    assert trend[0].ratio == 3.0
    assert trend[1].ratio == 3.0

    listing = service.list_sites(7, query="站点A")
    assert listing.total == 1
    assert listing.items[0].ratio_change == 1.0

    csv_text = service.export_csv(7)
    assert csv_text.startswith("\ufeff")
    assert "站点A" in csv_text
    assert "C:\\" not in csv_text


def test_successful_sync_writes_latest_daily_snapshot_and_prunes() -> None:
    factory = _factory()
    with factory() as session:
        _add_site(session, "site-a", "站点A")
        session.add(
            SiteStatisticsHistory(
                site_id="site-a",
                stat_date=date(2025, 1, 1),
                bonus=1.0,
                fetched_at=datetime(2025, 1, 1, tzinfo=UTC),
            )
        )
        session.commit()

    service = SiteService(
        factory,
        cast(SecretStore, object()),
        statistics_timezone="Asia/Shanghai",
        statistics_history_days=30,
    )
    profile = SiteUserProfile(
        site_id="site-a",
        ratio=2.0,
        bonus=100,
        seeding_count=5,
        fetched_at=datetime(2026, 1, 2, 8, tzinfo=UTC),
    )
    with factory() as session:
        service._record_statistics_history(
            session, "site-a", profile, datetime(2026, 1, 2, 12, tzinfo=UTC)
        )
        session.commit()

    with factory() as session:
        rows = list(session.scalars(select(SiteStatisticsHistory)))
        assert len(rows) == 1
        assert rows[0].stat_date == date(2026, 1, 2)
        assert rows[0].bonus == 100

    updated = SiteUserProfile(
        site_id="site-a",
        ratio=2.5,
        bonus=140,
        seeding_count=6,
        fetched_at=datetime(2026, 1, 2, 9, tzinfo=UTC),
    )
    with factory() as session:
        service._record_statistics_history(
            session, "site-a", updated, datetime(2026, 1, 2, 15, tzinfo=UTC)
        )
        session.commit()

    with factory() as session:
        rows = list(session.scalars(select(SiteStatisticsHistory)))
        assert len(rows) == 1
        assert rows[0].bonus == 140
        assert rows[0].ratio == 2.5
