"""监控中心 · 站点资产趋势的纯领域规则。"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo


def site_asset_stat_date(fetched_at: datetime, timezone: str) -> date:
    """Convert an observed UTC timestamp into the application's calendar day."""
    if fetched_at.tzinfo is None:
        raise ValueError("fetched_at must be timezone-aware")
    return fetched_at.astimezone(ZoneInfo(timezone)).date()


def aggregate_ratio(uploaded_bytes: int | None, downloaded_bytes: int | None) -> float | None:
    """Aggregate cross-site ratio; zero downloads cannot yield a finite chart value."""
    if uploaded_bytes is None or downloaded_bytes is None or downloaded_bytes <= 0:
        return None
    return uploaded_bytes / downloaded_bytes


def _sum_float(values: Iterable[int | float | None]) -> float | None:
    present = [value for value in values if value is not None]
    return sum(present) if present else None


def _sum_int(values: Iterable[int | None]) -> int | None:
    present = [value for value in values if value is not None]
    return sum(present) if present else None


@dataclass(frozen=True, slots=True)
class SiteAssetDayAggregate:
    stat_date: date
    ratio: float | None
    bonus: float | None
    seeding_points: float | None
    bonus_per_hour: float | None
    seeding_count: int | None
    site_count: int


def aggregate_site_assets(
    rows: Mapping[date, Sequence[Mapping[str, Any]]],
) -> list[SiteAssetDayAggregate]:
    """Aggregate per-site daily snapshots without treating missing sites as zero."""
    result: list[SiteAssetDayAggregate] = []
    for stat_date in sorted(rows):
        daily = rows[stat_date]
        uploaded = _sum_int(
            value if isinstance(value, int) and not isinstance(value, bool) else None
            for value in (row.get("uploaded_bytes") for row in daily)
        )
        downloaded = _sum_int(
            value if isinstance(value, int) and not isinstance(value, bool) else None
            for value in (row.get("downloaded_bytes") for row in daily)
        )
        result.append(
            SiteAssetDayAggregate(
                stat_date=stat_date,
                ratio=aggregate_ratio(uploaded, downloaded),
                bonus=_sum_float(
                    value
                    if isinstance(value, (int, float)) and not isinstance(value, bool)
                    else None
                    for value in (row.get("bonus") for row in daily)
                ),
                seeding_points=_sum_float(
                    value
                    if isinstance(value, (int, float)) and not isinstance(value, bool)
                    else None
                    for value in (row.get("seeding_points") for row in daily)
                ),
                bonus_per_hour=_sum_float(
                    value
                    if isinstance(value, (int, float)) and not isinstance(value, bool)
                    else None
                    for value in (row.get("bonus_per_hour") for row in daily)
                ),
                seeding_count=_sum_int(
                    value if isinstance(value, int) and not isinstance(value, bool) else None
                    for value in (row.get("seeding_count") for row in daily)
                ),
                site_count=len(daily),
            )
        )
    return result
