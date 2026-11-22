"""站点详情链接的解析与生成保持一致。"""

from __future__ import annotations

from backend.app.domain.site_config import SiteKind
from backend.app.domain.site_detail_url import (
    build_torrent_detail_url,
    parse_torrent_detail_id,
)

NEXUS = "https://hdtime.org"
MTEAM = "https://kp.m-team.cc"
ROUSI = "https://rousi.pro"


def test_nexusphp_details_query_form() -> None:
    assert parse_torrent_detail_id(f"{NEXUS}/details.php?id=12345", NEXUS) == "12345"
    assert parse_torrent_detail_id(f"{NEXUS}/details.php?id=12345&hit=1", NEXUS) == "12345"
    assert parse_torrent_detail_id(f"{NEXUS}/details.php", NEXUS) is None
    assert parse_torrent_detail_id(f"{NEXUS}/details.php?id=abc", NEXUS) is None


def test_path_style_detail_forms() -> None:
    # M-Team 与 Rousi Pro 都不使用 details.php。
    assert parse_torrent_detail_id(f"{MTEAM}/detail/123456", MTEAM) == "123456"
    assert parse_torrent_detail_id(f"{MTEAM}/detail/123456/", MTEAM) == "123456"
    assert parse_torrent_detail_id(f"{ROUSI}/torrents/998877", ROUSI) == "998877"
    assert parse_torrent_detail_id(f"{ROUSI}/torrent/998877", ROUSI) == "998877"
    assert parse_torrent_detail_id(f"{ROUSI}/torrents/abc", ROUSI) is None


def test_rejects_foreign_host_and_other_paths() -> None:
    assert parse_torrent_detail_id("https://evil.example/details.php?id=1", NEXUS) is None
    assert parse_torrent_detail_id(f"{ROUSI}/search?q=dune", ROUSI) is None
    assert parse_torrent_detail_id(f"{ROUSI}/torrents/1/upload", ROUSI) is None
    assert parse_torrent_detail_id("javascript:alert(1)", ROUSI) is None


def test_build_matches_parse_per_site() -> None:
    for kind, base, torrent_id in (
        (SiteKind.HDTIME, NEXUS, "12345"),
        (SiteKind.MTEAM, MTEAM, "123456"),
        (SiteKind.ROUSI_PRO, ROUSI, "998877"),
    ):
        url = build_torrent_detail_url(kind, base, torrent_id)
        assert parse_torrent_detail_id(url, base) == torrent_id
