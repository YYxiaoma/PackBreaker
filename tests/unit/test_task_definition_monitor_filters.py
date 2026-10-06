from backend.app.application.task_definition_executions import monitor_downloader_torrent_matches
from backend.app.infrastructure.adapters.downloaders import DownloaderTorrent


def _torrent(
    *,
    name: str = "Wanted.Movie.2026.1080p",
    category: str | None = "movies",
    tags: tuple[str, ...] = ("packbreaker", "hd"),
) -> DownloaderTorrent:
    return DownloaderTorrent(
        torrent_hash="a" * 40,
        name=name,
        status="uploading",
        progress=1.0,
        size_bytes=1024,
        category=category,
        tags=tags,
        tracker="https://tracker.invalid/announce",
        save_path="/downloads",
        content_path=f"/downloads/{name}",
    )


def test_monitor_downloader_filter_requires_name_category_and_any_tag() -> None:
    config = {
        "monitor_filter": {
            "name_contains": "wanted.movie",
            "categories": ["Movies", "Films"],
            "tags": ["4K", "PACKBREAKER"],
        }
    }

    assert monitor_downloader_torrent_matches(config, _torrent()) is True
    assert (
        monitor_downloader_torrent_matches(
            config,
            _torrent(name="Different.Movie.2026.1080p"),
        )
        is False
    )
    assert (
        monitor_downloader_torrent_matches(
            config,
            _torrent(category="tv"),
        )
        is False
    )
    assert (
        monitor_downloader_torrent_matches(
            config,
            _torrent(tags=("other",)),
        )
        is False
    )


def test_monitor_downloader_filter_is_backward_compatible_when_absent() -> None:
    assert monitor_downloader_torrent_matches({}, _torrent()) is True


def test_monitor_downloader_filter_fails_closed_when_malformed() -> None:
    assert monitor_downloader_torrent_matches({"monitor_filter": "bad"}, _torrent()) is False
    assert (
        monitor_downloader_torrent_matches(
            {"monitor_filter": {"categories": "movies"}},
            _torrent(),
        )
        is False
    )
