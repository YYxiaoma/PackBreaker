"""监控中心 · 种子收益归因的纯领域规则（只处理实测数据）。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class SiteTorrentYield:
    """站点可选能力返回的真实单种子贡献；没有字段含义明确的来源时保持 None。"""

    site_key: str
    remote_torrent_id: str
    uploaded_bytes: int | None = None
    bonus_contribution: float | None = None
    bonus_per_hour_contribution: float | None = None
    fetched_at: datetime | None = None


def torrent_key(downloader_id: str, torrent_hash: str) -> str:
    return f"{downloader_id}:{torrent_hash}"


def split_torrent_key(key: str) -> tuple[str, str] | None:
    downloader_id, separator, torrent_hash = key.partition(":")
    if not separator or not downloader_id or not torrent_hash:
        return None
    return downloader_id, torrent_hash


def daily_delta(first: int | None, last: int | None) -> int | None:
    """Same-day cumulative delta; a client reset invalidates the interval."""
    if first is None or last is None or last < first:
        return None
    return last - first


def is_zero_upload(
    deltas: list[int | None],
    *,
    seeding_seconds: int | None,
    presence_active: bool,
    min_seeding_seconds: int = 7 * 24 * 3600,
) -> bool:
    """Only claim zero-upload after two valid observations and a long seed period."""
    valid = [value for value in deltas if value is not None]
    return (
        presence_active
        and len(valid) >= 2
        and all(value == 0 for value in valid)
        and seeding_seconds is not None
        and seeding_seconds >= min_seeding_seconds
    )
