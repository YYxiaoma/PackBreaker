"""监控中心 · 种子收益归因的采集与只读查询用例。

口径：上传量来自下载器实测；积分/时魔只有站点适配器返回真实单种子值时才填充，
其余一律保持 None，不做任何估算或外推。
"""

from __future__ import annotations

import asyncio
import csv
import io
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import PurePosixPath
from time import monotonic
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.domain.site_assets import site_asset_stat_date
from backend.app.domain.torrent_yield import (
    daily_delta,
    is_zero_upload,
    split_torrent_key,
    torrent_key,
)
from backend.app.infrastructure.adapters.downloaders import DownloaderTorrent
from backend.app.infrastructure.persistence.database import begin_immediate_write
from backend.app.infrastructure.persistence.models import (
    MovieDedupFileInventory,
    MovieDedupJob,
    MovieDedupPair,
    SeedingWatch,
    TorrentYieldDaily,
)

_SEEDING_TOLERANCE = 0.999
_ZERO_UPLOAD_MIN_SEEDING_SECONDS = 7 * 24 * 3600
# 概览、列表、翻页、搜索会在一分钟内反复命中同一批日行；聚合结果短时缓存，
# 避免每次请求都把整个窗口几十万行读进内存重算。采集写入后立即失效。
_WINDOW_CACHE_TTL = timedelta(seconds=60)


@dataclass(frozen=True, slots=True)
class TorrentYieldSummaryView:
    window_days: int
    # 日行只保留这么多天；窗口口径比它长时前端要提示「更早日期没有采样」。
    retention_days: int
    generated_at: datetime
    total_upload_bytes: int
    total_bonus_contribution: float | None
    total_bonus_per_hour_contribution: float | None
    zero_upload_count: int
    observed_torrents: int
    contribution_available: bool


@dataclass(frozen=True, slots=True)
class TorrentYieldRowView:
    key: str
    downloader_id: str
    torrent_hash: str
    title: str
    site_key: str | None
    remote_torrent_id: str | None
    size_bytes: int | None
    upload_bytes: int
    bonus_contribution: float | None
    bonus_per_hour_contribution: float | None
    contribution_available: bool
    seeding_seconds: int
    zero_upload: bool
    status: str
    reclaimable_bytes: int


@dataclass(frozen=True, slots=True)
class TorrentYieldListView:
    items: list[TorrentYieldRowView]
    total: int
    page: int
    page_size: int


@dataclass(frozen=True, slots=True)
class CleanupPreviewView:
    reclaimable_bytes: int
    bonus_loss: float | None
    bonus_loss_available: bool
    matched_files: int
    unmatched_torrents: list[str]
    warnings: list[str]


class TorrentYieldService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        downloader_service: Any,
        *,
        timezone: str,
        history_days: int,
        refresh_min_interval_seconds: int,
    ) -> None:
        self._session_factory = session_factory
        self._downloader_service = downloader_service
        self._timezone = timezone
        self._history_days = max(30, min(history_days, 180))
        self._refresh_min_interval = max(30, min(refresh_min_interval_seconds, 3600))
        self._last_capture_at: float | None = None
        self._window_cache: tuple[tuple[date, date], datetime, list[TorrentYieldRowView]] | None = (
            None
        )

    def _window_items(self, start: date, end: date) -> list[TorrentYieldRowView]:
        """窗口聚合一分钟内复用；采集写入会清空缓存。"""
        cached = self._window_cache
        now = datetime.now(UTC)
        if (
            cached is not None
            and cached[0] == (start, end)
            and now - cached[1] <= _WINDOW_CACHE_TTL
        ):
            return cached[2]
        items = self._aggregate_rows(self._load_window(start, end), self._reclaimable_map())
        self._window_cache = ((start, end), now, items)
        return items

    def _window(self, days: int) -> tuple[date, date]:
        bounded = min(max(days, 1), 365)
        today = site_asset_stat_date(datetime.now(UTC), self._timezone)
        return today - timedelta(days=bounded - 1), today

    async def capture_once(self) -> dict[str, Any]:
        now_clock = monotonic()
        if (
            self._last_capture_at is not None
            and now_clock - self._last_capture_at < self._refresh_min_interval
        ):
            return {
                "accepted": False,
                "reason": "REFRESH_TOO_SOON",
                "scanned": 0,
                "recorded": 0,
                "skipped": 0,
                "errors": [],
            }
        self._last_capture_at = now_clock
        observed_at = datetime.now(UTC)
        collected: dict[str, tuple[DownloaderTorrent, ...]] = {}
        errors: list[str] = []
        for downloader in self._downloader_service.list_downloaders():
            if not downloader.enabled:
                continue
            try:
                collected[downloader.id] = await self._downloader_service.list_all_torrents(
                    downloader.id
                )
            except Exception as exc:  # noqa: BLE001 - isolate per-downloader failures
                errors.append(f"{downloader.name}: {type(exc).__name__}")
        recorded, skipped = await asyncio.to_thread(self._record_samples, collected, observed_at)
        return {
            "accepted": True,
            "reason": None,
            "scanned": sum(len(items) for items in collected.values()),
            "recorded": recorded,
            "skipped": skipped,
            "errors": errors,
        }

    def record_samples_from_patrol(
        self,
        collected: dict[str, tuple[DownloaderTorrent, ...]],
        observed_at: datetime | None = None,
    ) -> tuple[int, int]:
        """Called by the seeding-health patrol to reuse its already fetched torrent list."""
        return self._record_samples(collected, observed_at or datetime.now(UTC))

    def _record_samples(
        self,
        collected: dict[str, tuple[DownloaderTorrent, ...]],
        observed_at: datetime,
    ) -> tuple[int, int]:
        stat_date = site_asset_stat_date(observed_at, self._timezone)
        recorded = 0
        skipped = 0
        with self._session_factory() as session:
            # 与巡查/手动刷新共享同一读改写临界区，避免同日唯一行竞争。
            begin_immediate_write(session)
            for downloader_id, torrents in collected.items():
                watches = {
                    watch.torrent_hash: watch
                    for watch in session.scalars(
                        select(SeedingWatch).where(SeedingWatch.downloader_id == downloader_id)
                    )
                }
                for torrent in torrents:
                    if float(torrent.progress) < _SEEDING_TOLERANCE:
                        skipped += 1
                        continue
                    watch = watches.get(torrent.torrent_hash)
                    if watch is None or torrent.uploaded_bytes is None:
                        skipped += 1
                        continue
                    record = session.scalar(
                        select(TorrentYieldDaily).where(
                            TorrentYieldDaily.downloader_id == downloader_id,
                            TorrentYieldDaily.torrent_hash == torrent.torrent_hash,
                            TorrentYieldDaily.stat_date == stat_date,
                        )
                    )
                    if record is None:
                        record = TorrentYieldDaily(
                            downloader_id=downloader_id,
                            torrent_hash=torrent.torrent_hash,
                            stat_date=stat_date,
                            seeding_watch_id=watch.id,
                            title=torrent.name[:1024],
                            site_key=watch.site_key,
                            remote_torrent_id=watch.remote_torrent_id,
                            size_bytes=int(torrent.size_bytes),
                            first_uploaded_bytes=torrent.uploaded_bytes,
                            last_uploaded_bytes=torrent.uploaded_bytes,
                            upload_delta_bytes=None,
                            first_seeding_seconds=torrent.seeding_time_seconds,
                            last_seeding_seconds=torrent.seeding_time_seconds,
                            seeding_seconds_delta=None,
                            contribution_source="NOT_AVAILABLE",
                            status="INCOMPLETE",
                            created_at=observed_at,
                            updated_at=observed_at,
                        )
                        session.add(record)
                    else:
                        previous = record.last_uploaded_bytes
                        if previous is not None and torrent.uploaded_bytes < previous:
                            record.first_uploaded_bytes = torrent.uploaded_bytes
                            record.upload_delta_bytes = None
                            record.status = "REBASELINE"
                        else:
                            record.upload_delta_bytes = daily_delta(
                                record.first_uploaded_bytes, torrent.uploaded_bytes
                            )
                            record.status = (
                                "ACTIVE" if record.upload_delta_bytes is not None else "INCOMPLETE"
                            )
                        record.seeding_seconds_delta = daily_delta(
                            record.first_seeding_seconds, torrent.seeding_time_seconds
                        )
                        record.last_uploaded_bytes = torrent.uploaded_bytes
                        record.last_seeding_seconds = torrent.seeding_time_seconds
                        record.site_key = watch.site_key
                        record.remote_torrent_id = watch.remote_torrent_id
                        record.title = torrent.name[:1024]
                        record.size_bytes = int(torrent.size_bytes)
                        record.updated_at = observed_at
                    recorded += 1

            cutoff = site_asset_stat_date(observed_at, self._timezone) - timedelta(
                days=self._history_days
            )
            stale = list(
                session.scalars(
                    select(TorrentYieldDaily).where(TorrentYieldDaily.stat_date < cutoff)
                )
            )
            for row in stale:
                session.delete(row)
            session.commit()
        # 有新的日行写入，聚合缓存立刻作废，避免刷新后仍看到旧数字。
        self._window_cache = None
        return recorded, skipped

    def _load_window(
        self, start: date, end: date, keys: set[str] | None = None
    ) -> list[dict[str, Any]]:
        statement = (
            select(TorrentYieldDaily, SeedingWatch.presence, SeedingWatch.content_path)
            .join(SeedingWatch, SeedingWatch.id == TorrentYieldDaily.seeding_watch_id)
            .where(
                TorrentYieldDaily.stat_date >= start,
                TorrentYieldDaily.stat_date <= end,
            )
            .order_by(TorrentYieldDaily.stat_date.asc())
        )
        if keys is not None:
            parsed = [split_torrent_key(key) for key in keys]
            downloader_ids = {item[0] for item in parsed if item is not None}
            hashes = {item[1] for item in parsed if item is not None}
            if not downloader_ids or not hashes:
                return []
            # 清理预览/导出只看选中的种子；一年窗口若先取全量再过滤，代价是几十万行。
            statement = statement.where(
                TorrentYieldDaily.downloader_id.in_(downloader_ids),
                TorrentYieldDaily.torrent_hash.in_(hashes),
            )
        with self._session_factory() as session:
            result = session.execute(statement)
            rows: list[dict[str, Any]] = []
            for record, presence, content_path in result:
                key = torrent_key(record.downloader_id, record.torrent_hash)
                if keys is not None and key not in keys:
                    continue
                rows.append(
                    {
                        "key": key,
                        "downloader_id": record.downloader_id,
                        "torrent_hash": record.torrent_hash,
                        "stat_date": record.stat_date,
                        "title": record.title,
                        "site_key": record.site_key,
                        "remote_torrent_id": record.remote_torrent_id,
                        "size_bytes": record.size_bytes,
                        "upload_delta_bytes": record.upload_delta_bytes,
                        "seeding_seconds_delta": record.seeding_seconds_delta,
                        "last_seeding_seconds": record.last_seeding_seconds,
                        "bonus_contribution": record.bonus_contribution,
                        "bonus_per_hour_contribution": record.bonus_per_hour_contribution,
                        "contribution_source": record.contribution_source,
                        "status": record.status,
                        "presence": presence,
                        "content_path": content_path,
                    }
                )
            return rows

    @staticmethod
    def _basename(path: str | None, fallback: str) -> str:
        if not path:
            return fallback
        return PurePosixPath(path.replace("\\", "/")).name or fallback

    def _dedup_reclaimable_map(self, session: Session) -> dict[tuple[str, int], int]:
        job = session.scalar(
            select(MovieDedupJob)
            .where(MovieDedupJob.status == "COMPLETED")
            .order_by(MovieDedupJob.finished_at.desc())
        )
        if job is None:
            return {}
        result = session.execute(
            select(MovieDedupPair, MovieDedupFileInventory)
            .join(
                MovieDedupFileInventory,
                MovieDedupFileInventory.id == MovieDedupPair.target_file_id,
            )
            .where(
                MovieDedupPair.job_id == job.id,
                MovieDedupPair.estimated_reclaimable_bytes > 0,
            )
        )
        mapping: dict[tuple[str, int], int] = {}
        seen: set[str] = set()
        for pair, inventory in result:
            if pair.target_file_id in seen:
                continue
            seen.add(pair.target_file_id)
            mapping[(inventory.basename, inventory.size_bytes)] = (
                mapping.get((inventory.basename, inventory.size_bytes), 0)
                + pair.estimated_reclaimable_bytes
            )
        return mapping

    def _aggregate_rows(
        self,
        rows: list[dict[str, Any]],
        reclaimable: dict[tuple[str, int], int],
    ) -> list[TorrentYieldRowView]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(row["key"], []).append(row)

        result: list[TorrentYieldRowView] = []
        for key, items in grouped.items():
            latest = items[-1]
            deltas = [item["upload_delta_bytes"] for item in items]
            real_bonus = [
                item["bonus_contribution"]
                for item in items
                if item["contribution_source"] == "SITE_REPORTED"
                and item["bonus_contribution"] is not None
            ]
            real_hourly = [
                item["bonus_per_hour_contribution"]
                for item in items
                if item["contribution_source"] == "SITE_REPORTED"
                and item["bonus_per_hour_contribution"] is not None
            ]
            seeding_seconds = max((item["last_seeding_seconds"] or 0 for item in items), default=0)
            basename = self._basename(latest["content_path"], latest["title"])
            reclaimable_bytes = reclaimable.get((basename, latest["size_bytes"] or -1), 0)
            result.append(
                TorrentYieldRowView(
                    key=key,
                    downloader_id=latest["downloader_id"],
                    torrent_hash=latest["torrent_hash"],
                    title=latest["title"],
                    site_key=latest["site_key"],
                    remote_torrent_id=latest["remote_torrent_id"],
                    size_bytes=latest["size_bytes"],
                    upload_bytes=sum(value or 0 for value in deltas),
                    bonus_contribution=sum(real_bonus) if real_bonus else None,
                    bonus_per_hour_contribution=real_hourly[-1] if real_hourly else None,
                    contribution_available=bool(real_bonus or real_hourly),
                    seeding_seconds=seeding_seconds,
                    zero_upload=is_zero_upload(
                        deltas,
                        seeding_seconds=seeding_seconds,
                        presence_active=latest["presence"] == "ACTIVE",
                        min_seeding_seconds=_ZERO_UPLOAD_MIN_SEEDING_SECONDS,
                    ),
                    status=latest["status"],
                    reclaimable_bytes=reclaimable_bytes,
                )
            )
        return result

    def _reclaimable_map(self) -> dict[tuple[str, int], int]:
        with self._session_factory() as session:
            return self._dedup_reclaimable_map(session)

    def summary(self, days: int = 30) -> TorrentYieldSummaryView:
        start, end = self._window(days)
        items = self._window_items(start, end)
        bonus_values = [
            item.bonus_contribution for item in items if item.bonus_contribution is not None
        ]
        hourly_values = [
            item.bonus_per_hour_contribution
            for item in items
            if item.bonus_per_hour_contribution is not None
        ]
        return TorrentYieldSummaryView(
            window_days=days,
            retention_days=self._history_days,
            generated_at=datetime.now(UTC),
            total_upload_bytes=sum(item.upload_bytes for item in items),
            total_bonus_contribution=sum(bonus_values) if bonus_values else None,
            total_bonus_per_hour_contribution=sum(hourly_values) if hourly_values else None,
            zero_upload_count=sum(1 for item in items if item.zero_upload),
            observed_torrents=len(items),
            contribution_available=bool(bonus_values or hourly_values),
        )

    def list_torrents(
        self,
        days: int = 30,
        *,
        sort: str = "upload",
        order: str = "desc",
        zero_only: bool = False,
        query: str | None = None,
        site_key: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> TorrentYieldListView:
        start, end = self._window(days)
        items = self._window_items(start, end)
        needle = (query or "").strip().lower()
        if needle:
            items = [
                item for item in items if needle in item.title.lower() or needle in item.key.lower()
            ]
        if site_key:
            items = [item for item in items if item.site_key == site_key]
        if zero_only:
            items = [item for item in items if item.zero_upload]

        field = {
            "upload": "upload_bytes",
            "bonus": "bonus_contribution",
            "hourly": "bonus_per_hour_contribution",
        }.get(sort, "upload_bytes")
        items.sort(
            key=lambda item: (
                getattr(item, field) is None,
                -(getattr(item, field) or 0.0)
                if order == "desc"
                else (getattr(item, field) or 0.0),
                item.key,
            )
        )
        total = len(items)
        bounded_page = max(page, 1)
        bounded_size = min(max(page_size, 1), 200)
        offset = (bounded_page - 1) * bounded_size
        return TorrentYieldListView(
            items=items[offset : offset + bounded_size],
            total=total,
            page=bounded_page,
            page_size=bounded_size,
        )

    def cleanup_preview(self, keys: list[str]) -> CleanupPreviewView:
        normalized = [key for key in keys if split_torrent_key(key) is not None]
        if not normalized:
            return CleanupPreviewView(0, None, False, 0, [], ["未选择任何种子"])
        start, end = self._window(365)
        rows = self._load_window(start, end, keys=set(normalized))
        items = self._aggregate_rows(rows, self._reclaimable_map())
        by_key = {item.key: item for item in items}
        unmatched = [key for key in normalized if key not in by_key]
        matched = [by_key[key] for key in normalized if key in by_key]
        bonuses = [
            item.bonus_contribution for item in matched if item.bonus_contribution is not None
        ]
        available = len(bonuses) == len(matched) and bool(matched)
        warnings: list[str] = []
        if unmatched:
            warnings.append(f"{len(unmatched)} 个种子没有有效观测，未计入估算")
        if not available:
            warnings.append("站点未提供所选种子的真实积分贡献，积分损失暂不可用")
        if any(item.reclaimable_bytes == 0 for item in matched):
            warnings.append("部分种子未匹配到去重估算，空间回收可能被低估")
        return CleanupPreviewView(
            reclaimable_bytes=sum(item.reclaimable_bytes for item in matched),
            bonus_loss=sum(bonuses) if available else None,
            bonus_loss_available=available,
            matched_files=sum(1 for item in matched if item.reclaimable_bytes > 0),
            unmatched_torrents=unmatched,
            warnings=warnings,
        )

    def cleanup_export(self, keys: list[str]) -> str:
        start, end = self._window(365)
        rows = self._load_window(start, end, keys=set(keys))
        items = self._aggregate_rows(rows, self._reclaimable_map())
        stream = io.StringIO(newline="")
        writer = csv.writer(stream)
        writer.writerow(
            [
                "定位键",
                "种子名称",
                "站点",
                "窗口上传量(bytes)",
                "真实积分贡献",
                "可回收空间(bytes)",
                "积分损失(本种子窗口)",
            ]
        )
        for item in items:
            writer.writerow(
                [
                    item.key,
                    item.title,
                    item.site_key or "",
                    item.upload_bytes,
                    item.bonus_contribution if item.bonus_contribution is not None else "N/A",
                    item.reclaimable_bytes,
                    item.bonus_contribution if item.bonus_contribution is not None else "N/A",
                ]
            )
        return "\ufeff" + stream.getvalue()
