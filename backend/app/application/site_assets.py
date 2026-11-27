"""监控中心 · 站点资产趋势的只读查询用例。"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.domain.site_assets import (
    aggregate_ratio,
    aggregate_site_assets,
    site_asset_stat_date,
)
from backend.app.infrastructure.persistence.models import Site, SiteStatisticsHistory

_TREND_METRICS = ("bonus", "seeding_points", "bonus_per_hour", "seeding_count")


@dataclass(frozen=True, slots=True)
class SiteAssetPointView:
    stat_date: date
    ratio: float | None
    bonus: float | None
    seeding_points: float | None
    bonus_per_hour: float | None
    seeding_count: int | None
    site_count: int


@dataclass(frozen=True, slots=True)
class SiteAssetSiteView:
    site_id: str
    site_name: str
    site_type: str
    ratio: float | None
    bonus: float | None
    seeding_points: float | None
    bonus_per_hour: float | None
    seeding_count: int | None
    ratio_change: float | None
    fetched_at: datetime


@dataclass(frozen=True, slots=True)
class SiteAssetRatioView:
    site_id: str
    site_name: str
    site_type: str
    ratio: float


@dataclass(frozen=True, slots=True)
class SiteAssetSummaryView:
    window_days: int
    generated_at: datetime
    bonus_total: float | None
    seeding_points_total: float | None
    bonus_per_hour_total: float | None
    seeding_count_total: int | None
    site_count: int
    changes: dict[str, float | None]
    incomplete_sites: list[str]
    ratio_distribution: list[SiteAssetRatioView]


@dataclass(frozen=True, slots=True)
class SiteAssetSiteListView:
    items: list[SiteAssetSiteView]
    total: int
    page: int
    page_size: int


class SiteAssetsService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        timezone: str,
    ) -> None:
        self._session_factory = session_factory
        self._timezone = timezone

    def _window(self, days: int) -> tuple[date, date]:
        bounded = min(max(days, 1), 365)
        today = site_asset_stat_date(datetime.now(UTC), self._timezone)
        return today - timedelta(days=bounded - 1), today

    def _load_rows(self, session: Session, start: date, end: date) -> list[dict[str, Any]]:
        result = session.execute(
            select(SiteStatisticsHistory, Site.name, Site.type)
            .join(Site, Site.id == SiteStatisticsHistory.site_id)
            .where(
                SiteStatisticsHistory.stat_date >= start,
                SiteStatisticsHistory.stat_date <= end,
            )
            .order_by(
                SiteStatisticsHistory.stat_date.asc(),
                Site.name.asc(),
            )
        )
        rows: list[dict[str, Any]] = []
        for history, site_name, site_type in result:
            rows.append(
                {
                    "site_id": history.site_id,
                    "site_name": site_name,
                    "site_type": site_type,
                    "stat_date": history.stat_date,
                    "ratio": history.ratio,
                    "bonus": history.bonus,
                    "seeding_points": history.seeding_points,
                    "bonus_per_hour": history.bonus_per_hour,
                    "seeding_count": history.seeding_count,
                    "uploaded_bytes": history.uploaded_bytes,
                    "downloaded_bytes": history.downloaded_bytes,
                    "seeding_size_bytes": history.seeding_size_bytes,
                    "fetched_at": history.fetched_at,
                }
            )
        return rows

    def summary(self, days: int = 30) -> SiteAssetSummaryView:
        start, end = self._window(days)
        with self._session_factory() as session:
            rows = self._load_rows(session, start, end)
        grouped: dict[date, list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(row["stat_date"], []).append(row)
        daily = aggregate_site_assets(grouped)

        latest = daily[-1] if daily else None
        latest_date = latest.stat_date if latest else None
        latest_site_ids = {row["site_id"] for row in rows if row["stat_date"] == latest_date}
        all_site_ids = {row["site_id"] for row in rows}
        names_by_id = {row["site_id"]: row["site_name"] for row in rows}
        incomplete = sorted(
            names_by_id[site_id]
            for site_id in all_site_ids - latest_site_ids
            if site_id in names_by_id
        )

        changes: dict[str, float | None] = {metric: None for metric in (*_TREND_METRICS, "ratio")}
        earliest_date = daily[0].stat_date if len(daily) > 1 else None
        if latest_date is not None and earliest_date is not None:
            latest_by_site = {
                row["site_id"]: row for row in rows if row["stat_date"] == latest_date
            }
            earliest_by_site = {
                row["site_id"]: row for row in rows if row["stat_date"] == earliest_date
            }
            common_ids = set(latest_by_site) & set(earliest_by_site)
            for metric in _TREND_METRICS:
                current_values = [latest_by_site[site_id][metric] for site_id in common_ids]
                previous_values = [earliest_by_site[site_id][metric] for site_id in common_ids]
                if (
                    common_ids
                    and all(value is not None for value in current_values)
                    and all(value is not None for value in previous_values)
                ):
                    changes[metric] = float(sum(current_values) - sum(previous_values))
            current_ratio = (
                aggregate_ratio(
                    sum(latest_by_site[site_id]["uploaded_bytes"] for site_id in common_ids),
                    sum(latest_by_site[site_id]["downloaded_bytes"] for site_id in common_ids),
                )
                if common_ids
                else None
            )
            previous_ratio = (
                aggregate_ratio(
                    sum(earliest_by_site[site_id]["uploaded_bytes"] for site_id in common_ids),
                    sum(earliest_by_site[site_id]["downloaded_bytes"] for site_id in common_ids),
                )
                if common_ids
                else None
            )
            if current_ratio is not None and previous_ratio is not None:
                changes["ratio"] = float(current_ratio - previous_ratio)

        distribution = sorted(
            (
                SiteAssetRatioView(
                    site_id=row["site_id"],
                    site_name=row["site_name"],
                    site_type=row["site_type"],
                    ratio=float(row["ratio"]),
                )
                for row in rows
                if row["stat_date"] == latest_date and row["ratio"] is not None
            ),
            key=lambda item: (-item.ratio, item.site_name),
        )

        return SiteAssetSummaryView(
            window_days=days,
            generated_at=datetime.now(UTC),
            bonus_total=latest.bonus if latest else None,
            seeding_points_total=latest.seeding_points if latest else None,
            bonus_per_hour_total=latest.bonus_per_hour if latest else None,
            seeding_count_total=latest.seeding_count if latest else None,
            site_count=latest.site_count if latest else 0,
            changes=changes,
            incomplete_sites=incomplete,
            ratio_distribution=distribution,
        )

    def trend(self, days: int = 30) -> list[SiteAssetPointView]:
        start, end = self._window(days)
        with self._session_factory() as session:
            rows = self._load_rows(session, start, end)
        grouped: dict[date, list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(row["stat_date"], []).append(row)
        return [
            SiteAssetPointView(
                stat_date=item.stat_date,
                ratio=item.ratio,
                bonus=item.bonus,
                seeding_points=item.seeding_points,
                bonus_per_hour=item.bonus_per_hour,
                seeding_count=item.seeding_count,
                site_count=item.site_count,
            )
            for item in aggregate_site_assets(grouped)
        ]

    def list_sites(
        self,
        days: int = 30,
        *,
        query: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> SiteAssetSiteListView:
        start, end = self._window(days)
        with self._session_factory() as session:
            rows = self._load_rows(session, start, end)

        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(row["site_id"], []).append(row)

        needle = (query or "").strip().lower()
        items: list[SiteAssetSiteView] = []
        for site_rows in grouped.values():
            site_rows.sort(key=lambda row: row["stat_date"])
            first, last = site_rows[0], site_rows[-1]
            if needle and needle not in last["site_name"].lower():
                continue
            ratio_change: float | None = None
            if len(site_rows) > 1 and first["ratio"] is not None and last["ratio"] is not None:
                ratio_change = float(last["ratio"] - first["ratio"])
            items.append(
                SiteAssetSiteView(
                    site_id=last["site_id"],
                    site_name=last["site_name"],
                    site_type=last["site_type"],
                    ratio=last["ratio"],
                    bonus=last["bonus"],
                    seeding_points=last["seeding_points"],
                    bonus_per_hour=last["bonus_per_hour"],
                    seeding_count=last["seeding_count"],
                    ratio_change=ratio_change,
                    fetched_at=last["fetched_at"],
                )
            )

        items.sort(key=lambda item: (item.ratio is None, -(item.ratio or 0.0), item.site_name))
        total = len(items)
        bounded_page = max(page, 1)
        bounded_size = min(max(page_size, 1), 200)
        offset = (bounded_page - 1) * bounded_size
        return SiteAssetSiteListView(
            items=items[offset : offset + bounded_size],
            total=total,
            page=bounded_page,
            page_size=bounded_size,
        )

    def export_csv(self, days: int = 30) -> str:
        start, end = self._window(days)
        with self._session_factory() as session:
            rows = self._load_rows(session, start, end)
        rows.sort(key=lambda row: (row["site_name"], row["stat_date"]))

        stream = io.StringIO(newline="")
        writer = csv.writer(stream)
        writer.writerow(
            [
                "日期",
                "站点",
                "站点类型",
                "分享率",
                "魔力值/积分",
                "做种积分",
                "每小时魔力值/积分",
                "做种数",
                "上传量(bytes)",
                "下载量(bytes)",
                "同步时间(UTC)",
            ]
        )
        for row in rows:
            writer.writerow(
                [
                    row["stat_date"].isoformat(),
                    row["site_name"],
                    row["site_type"],
                    "" if row["ratio"] is None else f"{row['ratio']:.4f}",
                    "" if row["bonus"] is None else row["bonus"],
                    "" if row["seeding_points"] is None else row["seeding_points"],
                    "" if row["bonus_per_hour"] is None else row["bonus_per_hour"],
                    "" if row["seeding_count"] is None else row["seeding_count"],
                    "" if row["uploaded_bytes"] is None else row["uploaded_bytes"],
                    "" if row["downloaded_bytes"] is None else row["downloaded_bytes"],
                    row["fetched_at"].isoformat(),
                ]
            )
        return "\ufeff" + stream.getvalue()
