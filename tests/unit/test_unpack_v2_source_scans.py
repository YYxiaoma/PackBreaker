from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.errors import ApplicationError
from backend.app.application.unpack_source_scans import UnpackSourceScanService
from backend.app.infrastructure.authorized_paths import AuthorizedPathScope
from backend.app.infrastructure.persistence.base import Base


def _service(tmp_path: Path) -> tuple[UnpackSourceScanService, Path]:
    root = tmp_path / "data"
    root.mkdir()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory: sessionmaker[Session] = sessionmaker(bind=engine, expire_on_commit=False)
    return (
        UnpackSourceScanService(
            factory,
            path_scope=AuthorizedPathScope.legacy_only(legacy_data_root=root),
        ),
        root,
    )


def _movies_token(service: UnpackSourceScanService, root: Path) -> str:
    roots = service.list_tree_roots()
    root_token = next(
        item.selection_token for item in roots if item.display_path == root.as_posix()
    )
    browsed = service.browse_tree(root_token)
    return next(item.selection_token for item in browsed.entries if item.name == "movies")


def test_source_scan_persists_media_and_supports_filters_and_selection(tmp_path: Path) -> None:
    service, root = _service(tmp_path)
    movies = root / "movies"
    movies.mkdir()
    (movies / "Film.A.2026.2160p.mkv").write_bytes(b"a" * 10)
    (movies / "Film.B.2026.1080p.mp4").write_bytes(b"b" * 20)
    (movies / "Film.B.2026.nfo").write_text("metadata", encoding="utf-8")
    archive = movies / "archive"
    archive.mkdir()
    (archive / "Film.C.2025.2160p.mkv").write_bytes(b"c" * 30)

    scan = service.create_scan(
        selection_token=_movies_token(service, root),
        file_filter={"extensions": [".mkv", ".mp4"]},
    )

    assert scan.discovered_count == 3
    first = service.list_items(scan.id, cursor=None, limit=2)
    assert len(first.items) == 2
    assert first.has_more is True
    assert first.next_cursor is not None

    second = service.list_items(scan.id, cursor=first.next_cursor, limit=2)
    assert len(second.items) == 1
    assert second.has_more is False

    filtered = service.list_items(
        scan.id,
        cursor=None,
        limit=20,
        resolution="2160p",
    )
    assert {item.filename for item in filtered.items} == {
        "Film.A.2026.2160p.mkv",
        "Film.C.2025.2160p.mkv",
    }

    selected_key = filtered.items[0].source_object_key
    summary = service.update_selection(
        scan.id,
        source_object_keys=(selected_key,),
        selected=True,
    )
    assert summary.selected_count == 1

    selected = service.list_items(
        scan.id,
        cursor=None,
        limit=20,
        selected=True,
    )
    assert [item.source_object_key for item in selected.items] == [selected_key]


def test_tree_token_is_revalidated_and_cannot_escape_authorized_root(tmp_path: Path) -> None:
    service, root = _service(tmp_path)
    (root / "movies").mkdir()

    forged = service._encode_selection_token("/etc")  # noqa: SLF001
    with pytest.raises(ApplicationError) as caught:
        service.browse_tree(forged)

    assert caught.value.code == "UNPACK_DIRECTORY_SELECTION_INVALID"


def test_source_scan_respects_non_recursive_filter(tmp_path: Path) -> None:
    service, root = _service(tmp_path)
    movies = root / "movies"
    movies.mkdir()
    (movies / "Top.2026.1080p.mkv").write_bytes(b"a")
    nested = movies / "nested"
    nested.mkdir()
    (nested / "Nested.2026.1080p.mkv").write_bytes(b"b")

    scan = service.create_scan(
        selection_token=_movies_token(service, root),
        file_filter={
            "extensions": [".mkv"],
            "include_subdirectories": False,
        },
    )

    page = service.list_items(scan.id, cursor=None, limit=20)
    assert [item.filename for item in page.items] == ["Top.2026.1080p.mkv"]
