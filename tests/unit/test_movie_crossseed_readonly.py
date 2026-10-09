from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

import pytest

from backend.app.domain.site_adapter import TorrentPayload
from backend.app.domain.site_config import SiteKind, site_profile
from backend.app.domain.site_search import (
    CandidateMeta,
    SearchPage,
    SearchQuery,
    normalize_candidate_meta,
)
from backend.app.infrastructure.adapters.sites import SiteAdapterFactory
from scripts import check_movie_crossseed_readonly as probe


def _bencode(value: object) -> bytes:
    if isinstance(value, int):
        return f"i{value}e".encode()
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if isinstance(value, Sequence) and not isinstance(value, (bytes, str)):
        return b"l" + b"".join(_bencode(item) for item in value) + b"e"
    if isinstance(value, Mapping):
        return (
            b"d"
            + b"".join(_bencode(key) + _bencode(item) for key, item in sorted(value.items()))
            + b"e"
        )
    raise TypeError(type(value).__name__)


def _torrent(content: bytes, *, altered_hash: bool = False) -> bytes:
    piece_length = 4
    pieces = b"".join(
        hashlib.sha1(content[start : start + piece_length]).digest()
        for start in range(0, len(content), piece_length)
    )
    if altered_hash:
        pieces = b"0" * len(pieces)
    return _bencode(
        {
            b"info": {
                b"length": len(content),
                b"name": b"Movie.2024.mkv",
                b"piece length": piece_length,
                b"pieces": pieces,
            }
        }
    )


def _source(tmp_path: Path) -> tuple[Path, Path]:
    data_root = tmp_path / "data"
    directory = data_root / "Movie.2024"
    directory.mkdir(parents=True)
    source = directory / "Movie.2024.mkv"
    source.write_bytes(b"abcdefgh")
    return source, data_root


def _config() -> dict[str, object]:
    return {
        "KeepFrds": {
            "url": site_profile(SiteKind.KEEPFRDS).base_url,
            "auth_type": "cookie",
            "cookie": "synthetic-confidential-cookie",
        }
    }


class FakeAdapter:
    def __init__(
        self,
        *,
        duplicate: bool = False,
        missing: bool = False,
        mismatch: bool = False,
        has_more: bool = False,
        total_hint: int | None = None,
    ):
        self.fetch_calls = 0
        self.duplicate = duplicate
        self.missing = missing
        self.mismatch = mismatch
        self.has_more = has_more
        self.total_hint = total_hint

    async def search(self, query: SearchQuery) -> SearchPage:
        assert query.query_text == "movie 2024"
        items: tuple[CandidateMeta, ...] = (
            normalize_candidate_meta(
                site_id="keepfrds",
                torrent_id="100",
                display_name="Movie.2024.mkv",
                total_size=8,
                seeders=10,
            ),
        )
        if self.duplicate:
            items += (
                normalize_candidate_meta(
                    site_id="keepfrds",
                    torrent_id="101",
                    display_name="Movie.2024",
                    total_size=8,
                    seeders=2,
                ),
            )
        if self.missing:
            items = ()
        return SearchPage("keepfrds", 1, items, self.has_more, self.total_hint)

    async def fetch_torrent(self, torrent_id: str) -> TorrentPayload:
        assert torrent_id == "100"
        self.fetch_calls += 1
        return TorrentPayload(
            "keepfrds", torrent_id, _torrent(b"abcdefgh", altered_hash=self.mismatch)
        )


class FakeFactory:
    def __init__(self, adapter: FakeAdapter):
        self.adapter = adapter

    def create(self, **kwargs: object) -> FakeAdapter:
        assert kwargs["kind"] is SiteKind.KEEPFRDS
        assert kwargs["base_url"] == site_profile(SiteKind.KEEPFRDS).base_url
        assert kwargs["credential"] == "synthetic-confidential-cookie"
        return self.adapter


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("duplicate", "missing", "search_only", "expected"),
    (
        (False, False, True, "UNIQUE_CANDIDATE"),
        (True, False, False, "AMBIGUOUS_CANDIDATES"),
        (False, True, False, "NO_SIZE_RELEVANT_CANDIDATE"),
    ),
)
async def test_readonly_candidate_gate_does_not_fetch(
    tmp_path: Path, duplicate: bool, missing: bool, search_only: bool, expected: str
) -> None:
    source, root = _source(tmp_path)
    adapter = FakeAdapter(duplicate=duplicate, missing=missing)
    result = await probe.check_movie_once(
        "keepfrds",
        config=_config(),
        source_path=source,
        data_root=root,
        query_text="Movie 2024",
        search_only=search_only,
        factory=cast(SiteAdapterFactory, FakeFactory(adapter)),
    )
    assert result["status"] == expected
    assert adapter.fetch_calls == 0
    assert "synthetic-confidential-cookie" not in json.dumps(result)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("has_more", "total_hint"),
    ((True, None), (False, 5)),
)
async def test_partial_search_results_cannot_be_treated_as_unique(
    tmp_path: Path, has_more: bool, total_hint: int | None
) -> None:
    source, root = _source(tmp_path)
    adapter = FakeAdapter(has_more=has_more, total_hint=total_hint)
    result = await probe.check_movie_once(
        "keepfrds",
        config=_config(),
        source_path=source,
        data_root=root,
        query_text="Movie 2024",
        search_only=False,
        factory=cast(SiteAdapterFactory, FakeFactory(adapter)),
    )
    assert result == {
        "site": "keepfrds",
        "status": "SEARCH_PAGE_INCOMPLETE",
        "returned_count": 1,
    }
    assert adapter.fetch_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mismatch", "expected"),
    ((False, "FULL_VERIFIED"), (True, "CLIENT_CHECK_REQUIRED")),
)
async def test_readonly_complete_piece_verification_without_side_effects(
    tmp_path: Path, mismatch: bool, expected: str
) -> None:
    source, root = _source(tmp_path)
    adapter = FakeAdapter(mismatch=mismatch)
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    result = await probe.check_movie_once(
        "keepfrds",
        config=_config(),
        source_path=source,
        data_root=root,
        query_text="Movie 2024",
        search_only=False,
        factory=cast(SiteAdapterFactory, FakeFactory(adapter)),
        sleep=fake_sleep,
    )
    assert result["status"] == expected
    assert result["mapped_count"] == result["required_count"] == 1
    assert result["pieces_total"] == 2
    assert result["pieces_verified"] == (2 if not mismatch else 0)
    assert result["torrent_saved"] is False
    assert result["downloader_contacted"] is False
    assert adapter.fetch_calls == 1
    assert sleeps == [2.0]
    assert source.read_bytes() == b"abcdefgh"
    assert "synthetic-confidential-cookie" not in json.dumps(result)
    assert "Movie.2024" not in json.dumps(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation_stage", ("search", "interval", "torrent_fetch"))
async def test_source_snapshot_change_stops_readonly_acceptance(
    tmp_path: Path, mutation_stage: str
) -> None:
    source, root = _source(tmp_path)

    def replace_source() -> None:
        before = source.stat()
        source.write_bytes(b"ijklmnop")
        # Same-size writes can share an mtime tick on coarse filesystems.
        # Make the simulated replacement unambiguously visible to snapshots.
        os.utime(
            source,
            ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000),
        )

    class ChangingAdapter(FakeAdapter):
        async def search(self, query: SearchQuery) -> SearchPage:
            page = await super().search(query)
            if mutation_stage == "search":
                replace_source()
            return page

        async def fetch_torrent(self, torrent_id: str) -> TorrentPayload:
            payload = await super().fetch_torrent(torrent_id)
            if mutation_stage == "torrent_fetch":
                replace_source()
            return payload

    async def change_at_interval(_seconds: float) -> None:
        if mutation_stage == "interval":
            replace_source()

    adapter = ChangingAdapter()
    result = await probe.check_movie_once(
        "keepfrds",
        config=_config(),
        source_path=source,
        data_root=root,
        query_text="Movie 2024",
        search_only=False,
        factory=cast(SiteAdapterFactory, FakeFactory(adapter)),
        sleep=change_at_interval,
    )
    assert result == {"site": "keepfrds", "status": "SOURCE_SNAPSHOT_CHANGED"}
    assert adapter.fetch_calls == (1 if mutation_stage == "torrent_fetch" else 0)


@pytest.mark.asyncio
async def test_ambiguous_same_length_source_mapping_blocks_full_verification(
    tmp_path: Path,
) -> None:
    source, root = _source(tmp_path)
    duplicate = root / source.name
    duplicate.write_bytes(b"abcdefgh")
    adapter = FakeAdapter()

    async def no_delay(_seconds: float) -> None:
        pass

    result = await probe.check_movie_once(
        "keepfrds",
        config=_config(),
        source_path=source,
        data_root=root,
        query_text="Movie 2024",
        search_only=False,
        factory=cast(SiteAdapterFactory, FakeFactory(adapter)),
        sleep=no_delay,
    )
    assert result["status"] == "FILE_MAPPING_INCOMPLETE"
    assert result["mapped_count"] == 0
    assert adapter.fetch_calls == 1
    assert duplicate.read_bytes() == source.read_bytes()


@pytest.mark.asyncio
async def test_readonly_rejects_symlink_source_and_untrusted_origin_without_network(
    tmp_path: Path,
) -> None:
    source, root = _source(tmp_path)
    symlink = source.parent / "linked.mkv"
    symlink.symlink_to(source)
    adapter = FakeAdapter()
    factory = cast(SiteAdapterFactory, FakeFactory(adapter))
    blocked = await probe.check_movie_once(
        "keepfrds",
        config=_config(),
        source_path=symlink,
        data_root=root,
        query_text="Movie 2024",
        search_only=True,
        factory=factory,
    )
    assert blocked["status"] == "SOURCE_BLOCKED"
    altered = _config()
    altered["KeepFrds"] = {
        "url": "https://external.invalid",
        "auth_type": "cookie",
        "cookie": "synthetic-confidential-cookie",
    }
    blocked = await probe.check_movie_once(
        "keepfrds",
        config=altered,
        source_path=source,
        data_root=root,
        query_text="Movie 2024",
        search_only=True,
        factory=factory,
    )
    assert blocked["status"] == "CONFIG_BLOCKED"
    assert adapter.fetch_calls == 0


def test_cli_requires_explicit_search_and_torrent_fetch_ack(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = [
        "--site",
        "keepfrds",
        "--source",
        str(tmp_path / "missing.mkv"),
        "--query",
        "Movie 2024",
    ]
    assert probe.main(args) == 2
    assert "未发起请求" in capsys.readouterr().out
    assert probe.main([*args, "--live"]) == 2
    assert "未发起请求" in capsys.readouterr().out
