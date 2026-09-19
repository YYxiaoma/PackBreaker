from __future__ import annotations

from backend.app.infrastructure.torrent_parser import parse_torrent
from scripts.check_arm64_downloaders_e2e import synthetic_torrent


def test_synthetic_torrent_is_private_and_has_no_real_tracker() -> None:
    content = b"PACKBREAKER-ISOLATED-ARM64-TEST" * 1000
    torrent = synthetic_torrent(content)
    meta = parse_torrent(torrent)
    assert meta.private is True
    assert meta.files[0].path == "movie.mkv"
    assert meta.files[0].length == len(content)
    assert meta.v1_info_hash is not None
    assert b"example.invalid" in torrent
