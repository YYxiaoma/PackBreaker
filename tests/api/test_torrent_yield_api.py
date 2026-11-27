from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from backend.app.api.dependencies import (
    AccessPrincipal,
    require_admin_csrf_principal,
    require_admin_principal,
)
from backend.app.config import AppSettings
from backend.app.infrastructure.persistence.models import (
    Downloader,
    SeedingWatch,
    TorrentYieldDaily,
)
from backend.app.main import create_app


def test_torrent_yield_api_uses_real_values_and_readonly_cleanup(tmp_path: Path) -> None:
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
        today = datetime.now(UTC).date()
        factory = app.state.runtime.session_factory
        with factory() as session:
            session.add(
                Downloader(
                    id="downloader-a",
                    name="合成下载器",
                    type="QBITTORRENT",
                    base_url="http://localhost:8080",
                )
            )
            session.add(
                SeedingWatch(
                    id="watch-a",
                    downloader_id="downloader-a",
                    torrent_hash="hash-a",
                    torrent_name="零上传影片.mkv",
                    size_bytes=1024,
                    save_path="/synthetic",
                    content_path="/synthetic/零上传影片.mkv",
                    site_key="MTEAM",
                    presence="ACTIVE",
                    first_seen_at=datetime.now(UTC),
                    last_seen_at=datetime.now(UTC),
                )
            )
            session.add(
                TorrentYieldDaily(
                    downloader_id="downloader-a",
                    torrent_hash="hash-a",
                    stat_date=today,
                    seeding_watch_id="watch-a",
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

        summary = client.get("/api/v1/monitor/yield/summary", params={"days": 7})
        assert summary.status_code == 200
        assert summary.json()["total_upload_bytes"] == 0
        assert summary.json()["zero_upload_count"] == 0

        listing = client.get("/api/v1/monitor/yield/torrents", params={"days": 7})
        assert listing.status_code == 200
        assert listing.json()["total"] == 1
        assert listing.json()["items"][0]["bonus_contribution"] is None

        key = "downloader-a:hash-a"
        preview = client.post("/api/v1/monitor/yield/cleanup-preview", json={"keys": [key]})
        assert preview.status_code == 200
        assert preview.json()["bonus_loss"] is None
        assert preview.json()["bonus_loss_available"] is False

        exported = client.post("/api/v1/monitor/yield/cleanup-export", json={"keys": [key]})
        assert exported.status_code == 200
        assert "N/A" in exported.text
        # 预览允许 500 个 key，导出必须能接同样的量；改用 query string 时这里会 422。
        assert (
            client.post(
                "/api/v1/monitor/yield/cleanup-export", json={"keys": [key] * 500}
            ).status_code
            == 200
        )

        refreshed = client.post("/api/v1/monitor/yield/refresh")
        assert refreshed.status_code == 202
        assert refreshed.json()["accepted"] is True
