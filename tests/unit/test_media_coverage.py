from __future__ import annotations

import base64
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.media_coverage import MediaCoverageService
from backend.app.domain.media_coverage import (
    coverage_bucket,
    coverage_suggestion,
    external_ids_movie_key,
    normalize_media_title,
)
from backend.app.infrastructure.authorized_paths import AuthorizedPathScope
from backend.app.infrastructure.persistence.base import Base
from backend.app.infrastructure.persistence.models import (
    Downloader,
    MediaCoverageRun,
    SeedingWatch,
    new_uuid,
    utc_now,
)


def _factory() -> sessionmaker[Session]:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def test_bucket_and_suggestion_rules() -> None:
    assert coverage_bucket(0, unknown_only=False) == "ZERO"
    assert coverage_bucket(1, unknown_only=False) == "ONE"
    assert coverage_bucket(2, unknown_only=False) == "TWO"
    assert coverage_bucket(5, unknown_only=False) == "THREE_PLUS"
    assert coverage_bucket(0, unknown_only=True) == "UNKNOWN"
    assert coverage_suggestion("ZERO", confidence="HIGH") == "ADD_SEED"
    assert coverage_suggestion("THREE_PLUS", confidence="HIGH") == "REVIEW_LOW_YIELD"
    assert coverage_suggestion("ONE", confidence="LOW") == "CONFIRM_IDENTITY"
    assert normalize_media_title("Movie.Name.2020.1080p") == "movie name 2020 1080p"
    assert external_ids_movie_key([{"namespace": "IMDB", "value": "tt1234567"}]) is not None


def test_scan_produces_zero_and_one_site_buckets(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    (library / "Alpha.Movie.2020.1080p.mkv").write_bytes(b"a" * 2048)
    (library / "Beta.Movie.2021.720p.mkv").write_bytes(b"b" * 1024)

    factory = _factory()
    run_id = new_uuid()
    with factory() as session:
        session.add(
            MediaCoverageRun(
                id=run_id,
                scope_fingerprint="synthetic",
                scope_label="library",
                status="RUNNING",
                phase="PENDING",
                started_at=utc_now(),
            )
        )
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
                id="watch-alpha",
                downloader_id="downloader-a",
                torrent_hash="a" * 40,
                torrent_name="Alpha.Movie.2020.1080p",
                size_bytes=2048,
                save_path="/synthetic",
                content_path=str(library / "Alpha.Movie.2020.1080p.mkv"),
                site_key="MTEAM",
                remote_torrent_id="alpha-1",
                presence="ACTIVE",
                first_seen_at=datetime.now(UTC),
                last_seen_at=datetime.now(UTC),
            )
        )
        session.commit()

    path_scope = AuthorizedPathScope(
        legacy_data_root=tmp_path.resolve(),
        config_dir=(tmp_path / "config").resolve(),
        authorized_roots=(tmp_path.resolve(),),
    )
    service = MediaCoverageService(
        factory,
        path_scope=path_scope,
        site_service=cast(object, None),
        site_probe_enabled=False,
        site_probe_budget=20,
        max_site_items=5000,
        max_movies=1000,
    )
    service._scan_sync(run_id, library, "synthetic", "library")

    summary = service.summary()
    assert summary.total_movies == 2
    assert summary.buckets["ONE"] == 1
    assert summary.buckets["ZERO"] == 1
    assert summary.buckets["UNKNOWN"] == 0
    assert summary.last_run is not None
    assert summary.last_run.status == "COMPLETED"

    listing = service.list_movies(bucket="ZERO")
    assert listing.total == 1
    assert "beta" in listing.items[0].display_title.casefold()

    csv_text = service.export_csv(bucket="ZERO")
    assert csv_text.startswith("\ufeff")
    assert "Beta.Movie.2021.720p.mkv" in csv_text
    assert str(library) not in csv_text


@pytest.mark.asyncio
async def test_create_run_releases_lock_when_run_insert_fails(tmp_path: Path) -> None:
    path_scope = AuthorizedPathScope(
        legacy_data_root=tmp_path.resolve(),
        config_dir=(tmp_path / "config").resolve(),
        authorized_roots=(tmp_path.resolve(),),
    )

    def broken_factory() -> Session:
        raise RuntimeError("database unavailable")

    service = MediaCoverageService(
        cast(sessionmaker[Session], broken_factory),
        path_scope=path_scope,
        site_service=cast(object, None),
        site_probe_enabled=False,
        site_probe_budget=20,
        max_site_items=5000,
        max_movies=1000,
    )
    token = "v1." + base64.urlsafe_b64encode(str(tmp_path).encode()).decode().rstrip("=")

    with pytest.raises(RuntimeError, match="database unavailable"):
        await service.create_run(token)

    assert not service._lock.locked()


def test_reconcile_stale_runs_marks_interrupted(tmp_path: Path) -> None:
    factory = _factory()
    with factory() as session:
        session.add(
            MediaCoverageRun(
                id="stale-run",
                scope_fingerprint="synthetic",
                scope_label="library",
                status="RUNNING",
                phase="SCANNING_LIBRARY",
                started_at=utc_now(),
            )
        )
        session.commit()

    service = MediaCoverageService(
        factory,
        path_scope=AuthorizedPathScope(
            legacy_data_root=tmp_path.resolve(),
            config_dir=(tmp_path / "config").resolve(),
            authorized_roots=(tmp_path.resolve(),),
        ),
        site_service=cast(object, None),
        site_probe_enabled=False,
        site_probe_budget=20,
        max_site_items=5000,
        max_movies=1000,
    )
    service.reconcile_stale_runs()

    with factory() as session:
        run = session.get(MediaCoverageRun, "stale-run")
        assert run is not None
        assert run.status == "FAILED"
        assert run.error_code == "INTERRUPTED"
        assert run.finished_at is not None


def test_signature_fallback_matches_translated_torrent_title(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    (library / "A.Clockwork.Orange.1971.BluRay.1080p.x265.10bit.MNHD-FRDS.mkv").write_bytes(
        b"a" * 2048
    )

    factory = _factory()
    run_id = new_uuid()
    with factory() as session:
        session.add(
            MediaCoverageRun(
                id=run_id,
                scope_fingerprint="synthetic",
                scope_label="library",
                status="RUNNING",
                phase="PENDING",
                started_at=utc_now(),
            )
        )
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
                id="watch-clockwork",
                downloader_id="downloader-a",
                torrent_hash="b" * 40,
                torrent_name="发条橙子.A.Clockwork.Orange.1971.BluRay.1080p.x265.10bit.MNHD-FRDS",
                size_bytes=4096,
                save_path="/synthetic",
                content_path=str(library / "A.Clockwork.Orange.1971.mkv"),
                site_key="mteam",
                remote_torrent_id="1234",
                presence="ACTIVE",
                first_seen_at=datetime.now(UTC),
                last_seen_at=datetime.now(UTC),
            )
        )
        session.commit()

    service = MediaCoverageService(
        factory,
        path_scope=AuthorizedPathScope(
            legacy_data_root=tmp_path.resolve(),
            config_dir=(tmp_path / "config").resolve(),
            authorized_roots=(tmp_path.resolve(),),
        ),
        site_service=cast(object, None),
        site_probe_enabled=False,
        site_probe_budget=20,
        max_site_items=5000,
        max_movies=1000,
    )
    service._scan_sync(run_id, library, "synthetic", "library")

    movies = service.list_movies(bucket="ONE")
    assert movies.total == 1
    assert movies.items[0].site_keys == ["mteam"]
    assert movies.items[0].match_level == "TOKEN_SIGNATURE"
    assert service.summary().buckets["ZERO"] == 0


_PACK_DIR = "倩女幽魂合集.A.Chinese.Ghost.Story.1987-1991.BluRay.1080p.x265.10bit.2Audio.MNHD-FRDS"


def _seed_pack_library(tmp_path: Path) -> tuple[Path, str]:
    library = tmp_path / "library"
    pack = library / _PACK_DIR
    for folder, filename in (
        ("倩女幽魂.A.Chinese.Ghost.Story.1987", "A.Chinese.Ghost.Story.1987.mkv"),
        ("倩女幽魂2人间道.A.Chinese.Ghost.Story.II.1990", "A.Chinese.Ghost.Story.II.1990.mkv"),
        ("倩女幽魂3道道道.A.Chinese.Ghost.Story.III.1991", "A.Chinese.Ghost.Story.III.1991.mkv"),
    ):
        target = pack / folder
        target.mkdir(parents=True)
        (target / filename).write_bytes(b"x" * 1000)
    content_path = "/downloads2/待刮削/电影/" + _PACK_DIR
    return library, content_path


def _coverage_service(factory: sessionmaker[Session], tmp_path: Path) -> MediaCoverageService:
    return MediaCoverageService(
        factory,
        path_scope=AuthorizedPathScope(
            legacy_data_root=tmp_path.resolve(),
            config_dir=(tmp_path / "config").resolve(),
            authorized_roots=(tmp_path.resolve(),),
        ),
        site_service=cast(object, None),
        site_probe_enabled=False,
        site_probe_budget=20,
        max_site_items=5000,
        max_movies=1000,
    )


def _seed_pack_watches(
    factory: sessionmaker[Session], content_path: str, size_bytes: int, sites: tuple[str, ...]
) -> None:
    with factory() as session:
        session.add(
            Downloader(
                id="downloader-a",
                name="合成下载器",
                type="QBITTORRENT",
                base_url="http://localhost:8080",
            )
        )
        for index, site in enumerate(sites):
            session.add(
                SeedingWatch(
                    id=f"watch-pack-{site}",
                    downloader_id="downloader-a",
                    torrent_hash=f"{index:040d}",
                    torrent_name=_PACK_DIR,
                    size_bytes=size_bytes,
                    save_path="/downloads2/待刮削/电影",
                    content_path=content_path,
                    site_key=site,
                    remote_torrent_id=f"pack-{index}",
                    presence="ACTIVE",
                    first_seen_at=datetime.now(UTC),
                    last_seen_at=datetime.now(UTC),
                )
            )
        session.commit()


def test_pack_directory_credits_every_movie_in_the_collection(tmp_path: Path) -> None:
    library, content_path = _seed_pack_library(tmp_path)
    factory = _factory()
    run_id = new_uuid()
    with factory() as session:
        session.add(
            MediaCoverageRun(
                id=run_id,
                scope_fingerprint="synthetic",
                scope_label="library",
                status="RUNNING",
                phase="PENDING",
                started_at=utc_now(),
            )
        )
        session.commit()
    _seed_pack_watches(factory, content_path, 3000, ("MTEAM", "HDFANS", "HDSKY"))

    service = _coverage_service(factory, tmp_path)
    service._scan_sync(run_id, library, "synthetic", "library")

    listing = service.list_movies(page_size=10)
    assert listing.total == 3
    for item in listing.items:
        assert item.coverage_count == 3
        assert item.bucket == "THREE_PLUS"
        assert item.match_level == "CONTENT_PATH"
    summary = service.summary()
    assert summary.buckets["ZERO"] == 0


def test_pack_directory_size_mismatch_falls_back_to_single_movie(tmp_path: Path) -> None:
    library, content_path = _seed_pack_library(tmp_path)
    factory = _factory()
    run_id = new_uuid()
    with factory() as session:
        session.add(
            MediaCoverageRun(
                id=run_id,
                scope_fingerprint="synthetic",
                scope_label="library",
                status="RUNNING",
                phase="PENDING",
                started_at=utc_now(),
            )
        )
        session.commit()
    # 只勾选了一部影片：种子大小与目录总容量相差 3 倍，不能把合集记到全部影片。
    _seed_pack_watches(factory, content_path, 1000, ("MTEAM",))

    service = _coverage_service(factory, tmp_path)
    service._scan_sync(run_id, library, "synthetic", "library")

    listing = service.list_movies(page_size=10)
    assert listing.total == 3
    credited = [item for item in listing.items if item.coverage_count > 0]
    assert len(credited) == 1
    assert credited[0].match_level != "CONTENT_PATH"


def test_pack_name_fallback_requires_size_and_year_compatibility() -> None:
    from backend.app.domain.media_matching import media_file_token_signature

    factory = _factory()
    service = MediaCoverageService(
        factory,
        path_scope=AuthorizedPathScope(
            legacy_data_root=Path.cwd(),
            config_dir=(Path.cwd() / "config").resolve(),
            authorized_roots=(Path.cwd(),),
        ),
        site_service=cast(object, None),
        site_probe_enabled=False,
        site_probe_budget=20,
        max_site_items=5000,
        max_movies=1000,
    )
    movies: dict[str, dict[str, Any]] = {
        "ghost-1": {
            "display_title": "a chinese ghost story",
            "year": 1987,
            "size_bytes": 1000,
            "relative_path_hint": (
                "A.Chinese.Ghost.Story.1987.BluRay.1080p.x265.10bit.2Audio.MNHD-FRDS.mkv"
            ),
        },
        "ghost-2": {
            "display_title": "a chinese ghost story ii",
            "year": 1990,
            "size_bytes": 1000,
            "relative_path_hint": (
                "A.Chinese.Ghost.Story.II.1990.BluRay.1080p.x265.10bit.2Audio.MNHD-FRDS.mkv"
            ),
        },
        "ghost-3": {
            "display_title": "a chinese ghost story iii",
            "year": 1991,
            "size_bytes": 1000,
            "relative_path_hint": (
                "A.Chinese.Ghost.Story.III.1991.BluRay.1080p.x265.10bit.2Audio.MNHD-FRDS.mkv"
            ),
        },
    }
    by_token: dict[str, set[str]] = {}
    tokens_by_movie: dict[str, set[str]] = {}
    for key, movie in movies.items():
        tokens = set(media_file_token_signature(Path(movie["relative_path_hint"]).name))
        tokens_by_movie[key] = tokens
        for token in tokens:
            by_token.setdefault(token, set()).add(key)

    matches = service._match_pack_name(_PACK_DIR, 3000, movies, by_token, tokens_by_movie)
    assert sorted(key for key, _, _ in matches) == ["ghost-1", "ghost-2", "ghost-3"]
    assert {level for _, level, _ in matches} == {"COLLECTION"}
    # 容量对不上：说明候选影片并不都属于这条种子。
    assert service._match_pack_name(_PACK_DIR, 12000, movies, by_token, tokens_by_movie) == []
    # 不含合集特征的普通单片种子不应扩展成多对多。
    assert (
        service._match_pack_name(
            "A.Chinese.Ghost.Story.II.1990.BluRay.1080p.x265.10bit.2Audio.MNHD-FRDS",
            1000,
            movies,
            by_token,
            tokens_by_movie,
        )
        == []
    )


def test_single_movie_directory_still_checks_capacity(tmp_path: Path) -> None:
    """单影片目录也要过容量校验：合集种子只落了一部时不能直接算 CONTENT_PATH。"""

    library = tmp_path / "library"
    folder = library / "Solo.Movie.2020.1080p.BluRay.x264-GRP"
    folder.mkdir(parents=True)
    (folder / "Solo.Movie.2020.1080p.BluRay.x264-GRP.mkv").write_bytes(b"x" * 1000)
    content_path = "/downloads2/" + folder.name

    factory = _factory()
    service = _coverage_service(factory, tmp_path)

    def scan(run_id: str, size_bytes: int) -> list[Any]:
        _seed_pack_watches(factory, content_path, size_bytes, ("MTEAM",))
        service._scan_sync(run_id, library, "synthetic", "library")
        return list(service.list_movies(page_size=10).items)

    run_id = new_uuid()
    with factory() as session:
        session.add(
            MediaCoverageRun(
                id=run_id,
                scope_fingerprint="synthetic",
                scope_label="library",
                status="RUNNING",
                phase="PENDING",
                started_at=utc_now(),
            )
        )
        session.commit()
    items = scan(run_id, 1040)
    assert items[0].match_level == "CONTENT_PATH"
    assert items[0].coverage_count == 1

    # 换一条目录外层大小为 5 倍的种子：目录里只有一部，明显不是它的内容。
    run_id = new_uuid()
    with factory() as session:
        session.add(
            MediaCoverageRun(
                id=run_id,
                scope_fingerprint="synthetic",
                scope_label="library",
                status="RUNNING",
                phase="PENDING",
                started_at=utc_now(),
            )
        )
        session.commit()
    with factory() as session:
        session.query(SeedingWatch).delete()
        session.query(Downloader).delete()
        session.commit()
    items = scan(run_id, 5000)
    assert all(item.match_level != "CONTENT_PATH" for item in items)


def test_pack_name_uses_inode_deduped_capacity() -> None:
    """硬链接让 size_bytes 翻倍时，站点容量校验必须按物理内容算，否则合集永远对不上。"""

    from backend.app.domain.media_matching import media_file_token_signature

    factory = _factory()
    service = MediaCoverageService(
        factory,
        path_scope=AuthorizedPathScope(
            legacy_data_root=Path.cwd(),
            config_dir=(Path.cwd() / "config").resolve(),
            authorized_roots=(Path.cwd(),),
        ),
        site_service=cast(object, None),
        site_probe_enabled=False,
        site_probe_budget=20,
        max_site_items=5000,
        max_movies=1000,
    )
    names = (
        "A.Chinese.Ghost.Story.1987.BluRay.1080p.x265.10bit.2Audio.MNHD-FRDS.mkv",
        "A.Chinese.Ghost.Story.II.1990.BluRay.1080p.x265.10bit.2Audio.MNHD-FRDS.mkv",
        "A.Chinese.Ghost.Story.III.1991.BluRay.1080p.x265.10bit.2Audio.MNHD-FRDS.mkv",
    )
    movies: dict[str, dict[str, Any]] = {}
    by_token: dict[str, set[str]] = {}
    tokens_by_movie: dict[str, set[str]] = {}
    for index, name in enumerate(names, start=1):
        key = f"ghost-{index}"
        movies[key] = {
            "display_title": f"a chinese ghost story {index}",
            "year": 1986 + index,
            # 每个影片在库里有两份硬链接副本，size_bytes 合计是物理量的两倍。
            "size_bytes": 2000,
            "inode_bytes": 1000,
            "relative_path_hint": name,
        }
        tokens = set(media_file_token_signature(Path(name).name))
        tokens_by_movie[key] = tokens
        for token in tokens:
            by_token.setdefault(token, set()).add(key)

    matches = service._match_pack_name(_PACK_DIR, 3000, movies, by_token, tokens_by_movie)
    assert sorted(key for key, _, _ in matches) == ["ghost-1", "ghost-2", "ghost-3"]
    # 没有 inode 数据时退回 size_bytes，仍然按旧口径工作，不因新字段缺失而崩。
    for movie in movies.values():
        movie.pop("inode_bytes")
    assert service._match_pack_name(_PACK_DIR, 3000, movies, by_token, tokens_by_movie) == []


def test_scope_round_trip(tmp_path: Path) -> None:
    factory = _factory()
    service = MediaCoverageService(
        factory,
        path_scope=AuthorizedPathScope(
            legacy_data_root=tmp_path.resolve(),
            config_dir=(tmp_path / "config").resolve(),
            authorized_roots=(tmp_path.resolve(),),
        ),
        site_service=cast(object, None),
        site_probe_enabled=False,
        site_probe_budget=20,
        max_site_items=5000,
        max_movies=1000,
    )
    token = "v1." + base64.urlsafe_b64encode(str(tmp_path).encode()).decode().rstrip("=")

    assert service.get_scope() is None
    saved = service.save_scope(token, "  媒体库A  ")
    assert saved.scope_label == "媒体库A"
    fetched = service.get_scope()
    assert fetched is not None
    assert fetched.selection_token == token
    assert fetched.scope_label == "媒体库A"
