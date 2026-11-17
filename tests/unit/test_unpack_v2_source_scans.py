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
    parts = root.parts[1:]
    roots = service.list_tree_roots()
    current = next(item for item in roots if item.name == parts[0])
    for part in parts[1:]:
        browsed = service.browse_tree(current.selection_token)
        current = next(item for item in browsed.entries if item.name == part)
    browsed = service.browse_tree(current.selection_token)
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


def test_tree_can_browse_outside_operation_roots_but_scan_cannot_use_them(
    tmp_path: Path,
) -> None:
    service, _root = _service(tmp_path)
    outside = tmp_path / "outside"
    (outside / "nested").mkdir(parents=True)

    selection = service._encode_selection_token(outside.as_posix())  # noqa: SLF001
    browsed = service.browse_tree(selection)
    assert browsed.display_path == outside.as_posix()
    assert [item.name for item in browsed.entries] == ["nested"]

    with pytest.raises(ApplicationError) as caught:
        service.create_scan(
            selection_token=selection,
            file_filter={"extensions": [".mkv"]},
        )
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
