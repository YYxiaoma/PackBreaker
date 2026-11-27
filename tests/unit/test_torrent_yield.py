from __future__ import annotations

import csv
import io
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.torrent_yield import TorrentYieldService
from backend.app.domain.torrent_yield import (
    daily_delta,
    is_zero_upload,
    split_torrent_key,
    torrent_key,
)
from backend.app.infrastructure.adapters.downloaders import DownloaderTorrent
from backend.app.infrastructure.persistence.base import Base
from backend.app.infrastructure.persistence.database import begin_immediate_write
from backend.app.infrastructure.persistence.models import (
    Downloader,
    MovieDedupFileInventory,
    MovieDedupJob,
    MovieDedupPair,
    SeedingWatch,
    TorrentYieldDaily,
)


def _factory() -> sessionmaker[Session]:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def _seed_downloader_and_watch(
    session: Session,
    *,
    hash_value: str,
    title: str,
    content_path: str,
) -> tuple[str, str]:
    downloader = Downloader(
        id="downloader-a",
        name="合成下载器",
        type="QBITTORRENT",
        base_url="http://localhost:8080",
    )
    session.add(downloader)
    watch = SeedingWatch(
        id=f"watch-{hash_value}",
        downloader_id=downloader.id,
        torrent_hash=hash_value,
        torrent_name=title,
        size_bytes=1024,
        save_path="/synthetic",
        content_path=content_path,
        site_key="MTEAM",
        presence="ACTIVE",
        first_seen_at=datetime.now(UTC),
        last_seen_at=datetime.now(UTC),
    )
    session.add(watch)
    session.commit()
    return downloader.id, watch.id


def test_daily_delta_and_zero_upload_rules() -> None:
    assert daily_delta(100, 150) == 50
    assert daily_delta(150, 100) is None
    assert daily_delta(None, 100) is None

    assert is_zero_upload([0, 0], seeding_seconds=8 * 86400, presence_active=True)
    assert not is_zero_upload([0], seeding_seconds=8 * 86400, presence_active=True)
    assert not is_zero_upload([0, 0], seeding_seconds=86400, presence_active=True)
    assert not is_zero_upload([0, 0], seeding_seconds=8 * 86400, presence_active=False)

    key = torrent_key("downloader-a", "abc123")
    assert split_torrent_key(key) == ("downloader-a", "abc123")


def test_yield_service_aggregates_real_data_only() -> None:
    factory = _factory()
    today = datetime.now(UTC).date()
    yesterday = today - timedelta(days=1)
    with factory() as session:
        downloader_id, watch_id = _seed_downloader_and_watch(
            session,
            hash_value="zero-hash",
            title="零上传影片.mkv",
            content_path="/library/零上传影片.mkv",
        )
        for stat_date in (yesterday, today):
            session.add(
                TorrentYieldDaily(
                    downloader_id=downloader_id,
                    torrent_hash="zero-hash",
                    stat_date=stat_date,
                    seeding_watch_id=watch_id,
                    title="零上传影片.mkv",
                    site_key="MTEAM",
                    size_bytes=1024,
                    upload_delta_bytes=0,
                    last_seeding_seconds=8 * 86400,
                    status="ACTIVE",
                    contribution_source="NOT_AVAILABLE",
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
            )
        session.commit()

    service = TorrentYieldService(
        factory,
        cast(object, None),
        timezone="Asia/Shanghai",
        history_days=60,
        refresh_min_interval_seconds=30,
    )
    summary = service.summary(7)
    assert summary.total_upload_bytes == 0
    assert summary.zero_upload_count == 1
    assert summary.total_bonus_contribution is None
    assert summary.contribution_available is False

    listing = service.list_torrents(7, zero_only=True)
    assert listing.total == 1
    assert listing.items[0].zero_upload is True
    assert listing.items[0].bonus_contribution is None


def test_cleanup_preview_matches_dedup_estimate_and_reports_missing_bonus() -> None:
    factory = _factory()
    today = datetime.now(UTC).date()
    with factory() as session:
        downloader_id, watch_id = _seed_downloader_and_watch(
            session,
            hash_value="dedup-hash",
            title="重复影片.mkv",
            content_path="/library/重复影片.mkv",
        )
        session.add(
            TorrentYieldDaily(
                downloader_id=downloader_id,
                torrent_hash="dedup-hash",
                stat_date=today,
                seeding_watch_id=watch_id,
                title="重复影片.mkv",
                site_key="MTEAM",
                size_bytes=1024,
                upload_delta_bytes=10,
                last_seeding_seconds=86400,
                status="ACTIVE",
                contribution_source="NOT_AVAILABLE",
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
        job = MovieDedupJob(
            id="dedup-job",
            name="合成去重",
            source_root="library-a",
            target_root="library-b",
            mode="AUTO",
            cross_filesystem_policy="STOP",
            status="COMPLETED",
            phase="COMPLETED",
            trace_id="synthetic",
            version=1,
            finished_at=datetime.now(UTC),
        )
        session.add(job)
        target = MovieDedupFileInventory(
            id="target-file",
            job_id=job.id,
            side="B",
            relative_path="重复影片.mkv",
            basename="重复影片.mkv",
            extension=".mkv",
            device=1,
            inode=2,
            size_bytes=1024,
            allocated_bytes=2048,
            link_count=1,
            mtime_ns=1,
            ctime_ns=1,
            mode=0,
            uid=0,
            gid=0,
            scan_status="FULL_HASHED",
        )
        source = MovieDedupFileInventory(
            id="source-file",
            job_id=job.id,
            side="A",
            relative_path="other/重复影片.mkv",
            basename="重复影片.mkv",
            extension=".mkv",
            device=1,
            inode=3,
            size_bytes=1024,
            allocated_bytes=2048,
            link_count=1,
            mtime_ns=1,
            ctime_ns=1,
            mode=0,
            uid=0,
            gid=0,
            scan_status="FULL_HASHED",
        )
        session.add_all([target, source])
        session.add(
            MovieDedupPair(
                id="dedup-pair",
                job_id=job.id,
                source_file_id=source.id,
                target_file_id=target.id,
                match_level="FULL_HASHED",
                status="VERIFIED_DUPLICATE",
                resolved_action="HARDLINK",
                estimated_reclaimable_bytes=4096,
            )
        )
        session.commit()

    service = TorrentYieldService(
        factory,
        cast(object, None),
        timezone="Asia/Shanghai",
        history_days=60,
        refresh_min_interval_seconds=30,
    )
    preview = service.cleanup_preview([torrent_key("downloader-a", "dedup-hash")])
    assert preview.reclaimable_bytes == 4096
    assert preview.bonus_loss is None
    assert preview.bonus_loss_available is False
    assert any("积分损失暂不可用" in warning for warning in preview.warnings)


def test_cleanup_export_uses_per_torrent_bonus_not_total() -> None:
    factory = _factory()
    today = datetime.now(UTC).date()
    with factory() as session:
        session.add(
            Downloader(
                id="downloader-a",
                name="合成下载器",
                type="QBITTORRENT",
                base_url="http://localhost:8080",
            )
        )
        for torrent_hash, bonus in (("hash-a", 10.0), ("hash-b", 20.0)):
            session.add(
                SeedingWatch(
                    id=f"watch-{torrent_hash}",
                    downloader_id="downloader-a",
                    torrent_hash=torrent_hash,
                    torrent_name=f"{torrent_hash}.mkv",
                    size_bytes=1024,
                    save_path="/synthetic",
                    content_path=f"/synthetic/{torrent_hash}.mkv",
                    site_key="MTEAM",
                    presence="ACTIVE",
                    first_seen_at=datetime.now(UTC),
                    last_seen_at=datetime.now(UTC),
                )
            )
            session.add(
                TorrentYieldDaily(
                    downloader_id="downloader-a",
                    torrent_hash=torrent_hash,
                    stat_date=today,
                    seeding_watch_id=f"watch-{torrent_hash}",
                    title=f"{torrent_hash}.mkv",
                    site_key="MTEAM",
                    size_bytes=1024,
                    upload_delta_bytes=100,
                    last_seeding_seconds=86400,
                    status="ACTIVE",
                    contribution_source="SITE_REPORTED",
                    bonus_contribution=bonus,
                    bonus_per_hour_contribution=1.0,
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
            )
        session.commit()

    service = TorrentYieldService(
        factory,
        cast(object, None),
        timezone="Asia/Shanghai",
        history_days=60,
        refresh_min_interval_seconds=30,
    )
    content = service.cleanup_export(
        [torrent_key("downloader-a", "hash-a"), torrent_key("downloader-a", "hash-b")]
    )
    rows = list(csv.reader(io.StringIO(content.lstrip("\ufeff"))))
    assert rows[0][-1] == "积分损失(本种子窗口)"
    assert sorted(row[-1] for row in rows[1:]) == ["10.0", "20.0"]
    assert not any(row[-1] == "30.0" for row in rows[1:])


def test_record_samples_enters_immediate_write(monkeypatch: pytest.MonkeyPatch) -> None:
    from backend.app.application import torrent_yield as yield_module

    calls: list[object] = []
    original = begin_immediate_write

    def tracked(session: Session) -> None:
        calls.append(session)
        original(session)

    monkeypatch.setattr(yield_module, "begin_immediate_write", tracked)

    factory = _factory()
    with factory() as session:
        _seed_downloader_and_watch(
            session,
            hash_value="a" * 40,
            title="sample.mkv",
            content_path="/synthetic/sample.mkv",
        )

    service = TorrentYieldService(
        factory,
        cast(object, None),
        timezone="Asia/Shanghai",
        history_days=60,
        refresh_min_interval_seconds=30,
    )
    torrent = cast(
        DownloaderTorrent,
        SimpleNamespace(
            torrent_hash="a" * 40,
            name="sample.mkv",
            progress=1.0,
            size_bytes=1024,
            seeding_time_seconds=86400,
            uploaded_bytes=4096,
        ),
    )
    service.record_samples_from_patrol(
        {"downloader-a": (torrent,)}, datetime(2026, 1, 2, 12, tzinfo=UTC)
    )

    assert len(calls) == 1
