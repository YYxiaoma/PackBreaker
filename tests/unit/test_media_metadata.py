from __future__ import annotations

from pathlib import Path

from backend.app.application.media_metadata import (
    AdjacentMediaMetadata,
    read_adjacent_media_metadata,
)
from backend.app.domain.media_matching import MediaDescriptor, parse_media_name


def _read_metadata(media: Path, root: Path, descriptor: MediaDescriptor) -> AdjacentMediaMetadata:
    snapshot = media.stat()
    return read_adjacent_media_metadata(
        str(media),
        str(root),
        expected_device=snapshot.st_dev,
        expected_inode=snapshot.st_ino,
        expected_size=snapshot.st_size,
        expected_mtime_ns=str(snapshot.st_mtime_ns),
        descriptor=descriptor,
    )


def test_adjacent_nfo_extracts_external_ids_and_xml_titles(tmp_path: Path) -> None:
    media = tmp_path / "Film.2024.mkv"
    media.write_bytes(b"media-placeholder")
    (tmp_path / "Film.2024.nfo").write_text(
        "Film.2024\n"
        "IMDB URL........: https://www.imdb.com/title/tt1234567/\n"
        "DOUBAN URL......: https://movie.douban.com/subject/1295644/\n",
        encoding="utf-8",
    )

    metadata = _read_metadata(media, tmp_path, parse_media_name("Film.2024"))

    assert {(item.namespace, item.value) for item in metadata.external_ids} == {
        ("imdb", "tt1234567"),
        ("douban", "1295644"),
    }


def test_generic_nfo_with_unrelated_title_is_not_used(tmp_path: Path) -> None:
    media = tmp_path / "Film.2024.mkv"
    media.write_bytes(b"media-placeholder")
    (tmp_path / "movie.nfo").write_text(
        '<movie><title>Unrelated Film</title><uniqueid type="imdb">tt7654321</uniqueid></movie>',
        encoding="utf-8",
    )

    metadata = _read_metadata(media, tmp_path, parse_media_name("Film.2024"))

    assert metadata.external_ids == ()
    assert metadata.titles == ()


def test_matching_generic_nfo_can_supply_a_title_alias_and_external_id(tmp_path: Path) -> None:
    media = tmp_path / "Film.2024.mkv"
    media.write_bytes(b"media-placeholder")
    (tmp_path / "movie.nfo").write_text(
        "<movie><title>Film</title><originaltitle>Original Film</originaltitle>"
        '<uniqueid type="imdb">tt1234567</uniqueid></movie>',
        encoding="utf-8",
    )

    metadata = _read_metadata(media, tmp_path, parse_media_name("Film.2024"))

    assert "Original Film" in metadata.titles
    assert ("imdb", "tt1234567") in {(item.namespace, item.value) for item in metadata.external_ids}
