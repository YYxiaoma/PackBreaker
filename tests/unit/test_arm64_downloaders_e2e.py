from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from backend.app.infrastructure.adapters.downloaders import QbittorrentTorrentState
from backend.app.infrastructure.torrent_parser import parse_torrent
from scripts.check_arm64_downloaders_e2e import _assert_qb_state, synthetic_torrent


def test_synthetic_torrent_is_private_and_has_no_real_tracker() -> None:
    content = b"PACKBREAKER-ISOLATED-ARM64-TEST" * 1000
    torrent = synthetic_torrent(content)
    meta = parse_torrent(torrent)
    assert meta.private is True
    assert meta.files[0].path == "movie.mkv"
    assert meta.files[0].length == len(content)
    assert meta.v1_info_hash is not None
    assert b"example.invalid" in torrent


def test_real_qb_probe_fails_closed_on_identity_drift_without_exposing_path(
    capsys: pytest.CaptureFixture[str],
) -> None:
    state = QbittorrentTorrentState(
        torrent_hash="a" * 40,
        save_path="/downloads/private-test-path",
        content_path=None,
        state="stoppedUP",
        tags=(),
        progress=1.0,
    )
    with pytest.raises(AssertionError):
        _assert_qb_state((state,), save_path="/downloads/expected", tag="packbreaker-arm64-ci")
    out = capsys.readouterr().out
    assert "save_path_matches=False" in out and "tag_present=False" in out
    assert "private-test-path" not in out and "expected" not in out


def test_real_arm64_downloader_image_pull_retries_only_registry_reads() -> None:
    root = Path(__file__).resolve().parents[2]
    script_path = root / "scripts" / "check-arm64-real-downloaders-e2e.sh"
    subprocess.run(["bash", "-n", str(script_path)], check=True, timeout=8)
    script = script_path.read_text(encoding="utf-8")
    pull_fn = script.split("check_image() {", 1)[1].split("\n}\n", 1)[0]
    assert "for attempt in 1 2 3; do" in pull_fn
    assert "docker pull --platform linux/arm64" in pull_fn
    assert "return 1" in pull_fn
    assert 'sleep "$((attempt * 10))"' in pull_fn
    assert "docker image inspect" in pull_fn
    assert "run_probe" not in pull_fn
    assert "run_journal_backed_task_probe" not in pull_fn
