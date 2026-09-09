from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from backend.app.api.dependencies import CSRF_COOKIE
from backend.app.application.sites import EnabledSiteAdapter
from backend.app.application.tasks import TaskAnalysisService
from backend.app.config import AppSettings
from backend.app.domain.site_adapter import SiteConnectionResult, TorrentDetails, TorrentPayload
from backend.app.domain.site_search import (
    SearchPage,
    SearchQuery,
    SiteSearchCapabilities,
    normalize_candidate_meta,
)
from backend.app.domain.task_state import TaskStatus
from backend.app.domain.task_units import SourceTaskFile, identify_task_units
from backend.app.infrastructure.persistence.models import TaskCandidateRecord, TaskUnitRecord
from backend.app.infrastructure.persistence.repositories import TaskCreate, TaskRepository
from backend.app.main import create_app

_PASSWORD = "synthetic correct horse battery staple"


class _FakeAdapter:
    def __init__(self, torrent_bytes: bytes) -> None:
        self._torrent_bytes = torrent_bytes

    async def capabilities(self) -> SiteSearchCapabilities:
        return SiteSearchCapabilities()

    async def test_connection(self) -> SiteConnectionResult:
        return SiteConnectionResult("fake")

    async def search(self, query: SearchQuery) -> SearchPage:
        candidate = normalize_candidate_meta(
            site_id="fake",
            torrent_id="42",
            display_name="Movie.2026",
            total_size=16,
        )
        return SearchPage("fake", query.page, (candidate,), False, 1)

    async def fetch_details(self, torrent_id: str) -> TorrentDetails:
        return TorrentDetails(
            normalize_candidate_meta(
                site_id="fake",
                torrent_id=torrent_id,
                display_name="Movie.2026",
                total_size=16,
            )
        )

    async def fetch_torrent(self, torrent_id: str) -> TorrentPayload:
        return TorrentPayload("fake", torrent_id, self._torrent_bytes, datetime.now(UTC))


class _FakeSiteProvider:
    def __init__(self, adapter: _FakeAdapter) -> None:
        self._adapter = adapter
        self.version = 1

    def enabled_adapters(self) -> tuple[EnabledSiteAdapter, ...]:
        return (EnabledSiteAdapter("cfg-fake", self.version, "fake", self._adapter),)

    def enabled_site_versions(self) -> tuple[tuple[str, int], ...]:
        return (("cfg-fake", self.version),)


def test_task_analyze_persists_units_candidates_and_reports_preflight_currentity(
    tmp_path: Path,
) -> None:
    client, app, settings = _authenticated_client(tmp_path)
    source_root = settings.data_dir / "movie"
    source_root.mkdir(parents=True)
    content = b"0123456789abcdef"
    source_file = source_root / "Movie.2026.mkv"
    source_file.write_bytes(content)
    unit = identify_task_units((SourceTaskFile(source_file.name, len(content)),))[0]
    task_id = _create_task(app, unit.normalized_unit_key)
    provider = _FakeSiteProvider(
        _FakeAdapter(_v1_torrent(source_file.name.encode(), content, piece_length=4))
    )
    app.state.task_analysis_service = TaskAnalysisService(
        app.state.runtime.session_factory,
        provider,
        data_root=settings.data_dir,
    )

    try:
        without_csrf = client.post(
            f"/api/v1/tasks/{task_id}/actions",
            json={"action": "analyze", "source_root": "movie"},
        )
        assert without_csrf.status_code == 403

        analyzed = client.post(
            f"/api/v1/tasks/{task_id}/actions",
            headers=_csrf(client),
            json={"action": "analyze", "source_root": "movie"},
        )
        assert analyzed.status_code == 200
        assert analyzed.json()["id"] is not None
        assert analyzed.json()["current"] is True
        assert analyzed.json()["stale_reasons"] == []
        assert analyzed.json()["payload"]["candidates"][0]["verification_level"] == "FULL_VERIFIED"

        units = client.get(f"/api/v1/tasks/{task_id}/units")
        assert units.status_code == 200
        assert len(units.json()["items"]) == 1
        assert units.json()["items"][0]["source_root"] == "movie"

        candidates = client.get(f"/api/v1/tasks/{task_id}/candidates")
        assert candidates.status_code == 200
        assert len(candidates.json()["items"]) == 1
        assert candidates.json()["items"][0]["verification_level"] == "FULL_VERIFIED"

        current = client.get(f"/api/v1/tasks/{task_id}/preflight/current")
        assert current.status_code == 200
        assert current.json()["current"] is True

        provider.version = 2
        site_stale = client.get(f"/api/v1/tasks/{task_id}/preflight/current")
        assert site_stale.json()["current"] is False
        assert site_stale.json()["stale_reasons"] == ["SITE_CONFIG_CHANGED"]
        provider.version = 1

        source_file.write_bytes(b"fedcba9876543210")
        os.utime(source_file, None)
        stale = client.get(f"/api/v1/tasks/{task_id}/preflight")
        assert stale.status_code == 200
        assert stale.json()["current"] is False
        assert "SOURCE_CHANGED" in stale.json()["stale_reasons"]

        with app.state.runtime.session_factory() as session:
            task = TaskRepository(session).get(task_id)
            assert task is not None
            TaskRepository(session).transition(
                task_id=task_id,
                expected_version=task.version,
                to_status=TaskStatus.ANALYZING,
                event_type="TEST_TASK_VERSION_CHANGE",
                reason="验证 preflight task version 失效",
            )
            session.commit()
        task_stale = client.get(f"/api/v1/tasks/{task_id}/preflight/current")
        assert "TASK_VERSION_CHANGED" in task_stale.json()["stale_reasons"]

        with app.state.runtime.session_factory() as session:
            assert session.scalar(select(func.count()).select_from(TaskUnitRecord)) == 1
            assert session.scalar(select(func.count()).select_from(TaskCandidateRecord)) == 1
    finally:
        client.__exit__(None, None, None)


def test_task_analyze_rejects_traversal_and_symlink_source_roots(tmp_path: Path) -> None:
    client, app, settings = _authenticated_client(tmp_path)
    source_root = settings.data_dir / "movie"
    source_root.mkdir(parents=True)
    content = b"0123456789abcdef"
    source_file = source_root / "Movie.2026.mkv"
    source_file.write_bytes(content)
    unit = identify_task_units((SourceTaskFile(source_file.name, len(content)),))[0]
    task_id = _create_task(app, unit.normalized_unit_key)
    app.state.task_analysis_service = TaskAnalysisService(
        app.state.runtime.session_factory,
        _FakeSiteProvider(
            _FakeAdapter(_v1_torrent(source_file.name.encode(), content, piece_length=4))
        ),
        data_root=settings.data_dir,
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    (settings.data_dir / "linked").symlink_to(outside, target_is_directory=True)

    try:
        traversal = client.post(
            f"/api/v1/tasks/{task_id}/actions",
            headers=_csrf(client),
            json={"action": "analyze", "source_root": "../outside"},
        )
        assert traversal.status_code == 422
        assert traversal.json()["code"] == "ANALYSIS_SOURCE_ROOT_INVALID"

        symlink = client.post(
            f"/api/v1/tasks/{task_id}/actions",
            headers=_csrf(client),
            json={"action": "analyze", "source_root": "linked"},
        )
        assert symlink.status_code == 422
        assert symlink.json()["code"] == "ANALYSIS_SOURCE_ROOT_INVALID"

        windows_drive = client.post(
            f"/api/v1/tasks/{task_id}/actions",
            headers=_csrf(client),
            json={"action": "analyze", "source_root": "C:/outside"},
        )
        assert windows_drive.status_code == 422
        assert windows_drive.json()["code"] == "ANALYSIS_SOURCE_ROOT_INVALID"
    finally:
        client.__exit__(None, None, None)


def test_task_collection_create_is_idempotent_and_listed(tmp_path: Path) -> None:
    client, _app, _settings = _authenticated_client(tmp_path)
    payload = {
        "task_type": "PACKAGE_UNPACK",
        "source_downloader_id": "source-downloader",
        "source_hash": "synthetic-source-hash",
        "normalized_unit_key": "unit-key-001",
    }
    try:
        without_csrf = client.post("/api/v1/tasks", json=payload)
        assert without_csrf.status_code == 403

        created = client.post("/api/v1/tasks", headers=_csrf(client), json=payload)
        assert created.status_code == 200
        assert created.json()["created"] is True
        task = created.json()["item"]
        assert task["status"] == "PENDING"
        assert task["source_hash"] == payload["source_hash"]

        duplicate = client.post("/api/v1/tasks", headers=_csrf(client), json=payload)
        assert duplicate.status_code == 200
        assert duplicate.json()["created"] is False
        assert duplicate.json()["item"]["id"] == task["id"]

        listed = client.get("/api/v1/tasks")
        assert listed.status_code == 200
        assert [item["id"] for item in listed.json()["items"]] == [task["id"]]

        detail = client.get(f"/api/v1/tasks/{task['id']}")
        assert detail.status_code == 200
        assert detail.json()["normalized_unit_key"] == payload["normalized_unit_key"]

        filtered = client.get("/api/v1/tasks", params={"status": "DONE"})
        assert filtered.status_code == 200
        assert filtered.json()["items"] == []
    finally:
        client.__exit__(None, None, None)


def test_task_collection_rejects_whitespace_only_identity(tmp_path: Path) -> None:
    client, _app, _settings = _authenticated_client(tmp_path)
    try:
        response = client.post(
            "/api/v1/tasks",
            headers=_csrf(client),
            json={
                "task_type": "   ",
                "source_downloader_id": "source",
                "source_hash": "hash",
                "normalized_unit_key": "unit",
            },
        )
        assert response.status_code == 422
        assert response.json()["code"] == "TASK_INPUT_INVALID"
    finally:
        client.__exit__(None, None, None)


def _authenticated_client(tmp_path: Path) -> tuple[TestClient, FastAPI, AppSettings]:
    settings = AppSettings(
        config_dir=(tmp_path / "config").resolve(),
        data_dir=(tmp_path / "data").resolve(),
    )
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    app = create_app(settings=settings)
    client = TestClient(app, base_url="https://testserver")
    client.__enter__()
    assert client.post("/api/v1/auth/setup", json={"password": _PASSWORD}).status_code == 201
    assert client.post("/api/v1/auth/login", json={"password": _PASSWORD}).status_code == 200
    return client, app, settings


def _csrf(client: TestClient) -> dict[str, str]:
    token = client.cookies.get(CSRF_COOKIE)
    assert token is not None
    return {"X-CSRF-Token": token}


def _create_task(app: FastAPI, normalized_unit_key: str) -> str:
    with app.state.runtime.session_factory() as session:
        task, _ = TaskRepository(session).create_or_get(
            TaskCreate(
                "PACKAGE_UNPACK",
                "source",
                "synthetic-source-hash",
                normalized_unit_key,
                "00000000-0000-0000-0000-000000000001",
            )
        )
        session.commit()
        return task.id


def _v1_torrent(name: bytes, content: bytes, *, piece_length: int) -> bytes:
    pieces = b"".join(
        hashlib.sha1(content[offset : offset + piece_length]).digest()
        for offset in range(0, len(content), piece_length)
    )
    return _bencode(
        {
            b"info": {
                b"length": len(content),
                b"name": name,
                b"piece length": piece_length,
                b"pieces": pieces,
            }
        }
    )


def _bencode(value: object) -> bytes:
    if isinstance(value, int):
        return f"i{value}e".encode()
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        return b"l" + b"".join(_bencode(item) for item in value) + b"e"
    if isinstance(value, Mapping):
        items = sorted(value.items(), key=lambda item: item[0])
        return b"d" + b"".join(_bencode(key) + _bencode(item) for key, item in items) + b"e"
    raise TypeError(type(value).__name__)
