from __future__ import annotations

from pathlib import Path

from backend.app.application.unpack_manual_torrents import ManualTorrentStore
from backend.app.config import AppSettings

_PAYLOAD = b"d4:infod4:name7:example6:lengthi1024eee"


def _store(tmp_path: Path) -> ManualTorrentStore:
    return ManualTorrentStore(tmp_path / "config" / "torrents")


def test_torrent_store_defaults_to_config_volume(tmp_path: Path) -> None:
    settings = AppSettings(config_dir=tmp_path / "config", data_dir=tmp_path / "data")

    assert settings.resolved_torrent_store_dir == tmp_path / "config" / "torrents"


def test_torrent_store_can_be_overridden(tmp_path: Path) -> None:
    override = tmp_path / "custom-store"
    settings = AppSettings(
        config_dir=tmp_path / "config",
        data_dir=tmp_path / "data",
        torrent_store_dir=override,
    )

    assert settings.resolved_torrent_store_dir == override


def test_torrent_store_rejects_relative_override(tmp_path: Path) -> None:
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        AppSettings(
            config_dir=tmp_path / "config",
            data_dir=tmp_path / "data",
            torrent_store_dir=Path("relative/store"),
        )


def test_import_legacy_directory_moves_content_addressed_files(tmp_path: Path) -> None:
    legacy = tmp_path / "data" / "unpack-manual-torrents"
    legacy.mkdir(parents=True)
    key = ManualTorrentStore.key_for(_PAYLOAD)
    source = legacy / f"{key}.torrent"
    source.write_bytes(_PAYLOAD)

    store = _store(tmp_path)

    assert store.import_legacy_directory(legacy) == 1
    assert store.load(key) == _PAYLOAD
    assert not source.exists()


def test_import_legacy_directory_keeps_tampered_files(tmp_path: Path) -> None:
    legacy = tmp_path / "data" / "unpack-manual-torrents"
    legacy.mkdir(parents=True)
    key = ManualTorrentStore.key_for(_PAYLOAD)
    source = legacy / f"{key}.torrent"
    source.write_bytes(b"tampered payload")

    store = _store(tmp_path)

    assert store.import_legacy_directory(legacy) == 0
    assert source.exists()


def test_import_legacy_directory_ignores_missing_root(tmp_path: Path) -> None:
    store = _store(tmp_path)

    assert store.import_legacy_directory(tmp_path / "missing") == 0


def test_import_legacy_directory_skips_symlink_root(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    target.mkdir()
    key = ManualTorrentStore.key_for(_PAYLOAD)
    (target / f"{key}.torrent").write_bytes(_PAYLOAD)
    link = tmp_path / "legacy"
    link.symlink_to(target, target_is_directory=True)

    store = _store(tmp_path)

    assert store.import_legacy_directory(link) == 0
    assert (target / f"{key}.torrent").exists()
