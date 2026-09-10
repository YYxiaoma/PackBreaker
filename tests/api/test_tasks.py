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
from backend.app.infrastructure.persistence.models import (
    TaskCandidateRecord,
    TaskEvent,
    TaskReviewRevisionRecord,
    TaskReviewVerificationRecord,
    TaskUnitRecord,
)
from backend.app.infrastructure.persistence.repositories import TaskCreate, TaskRepository
from backend.app.main import create_app

_PASSWORD = "synthetic correct horse battery staple"


class _FakeAdapter:
    def __init__(self, torrent_bytes: bytes, *, display_name: str = "Movie.2026") -> None:
        self._torrent_bytes = torrent_bytes
        self._display_name = display_name

    async def capabilities(self) -> SiteSearchCapabilities:
        return SiteSearchCapabilities()

    async def test_connection(self) -> SiteConnectionResult:
        return SiteConnectionResult("fake")

    async def search(self, query: SearchQuery) -> SearchPage:
        candidate = normalize_candidate_meta(
            site_id="fake",
            torrent_id="42",
            display_name=self._display_name,
            total_size=16,
        )
        return SearchPage("fake", query.page, (candidate,), False, 1)

    async def fetch_details(self, torrent_id: str) -> TorrentDetails:
        return TorrentDetails(
            normalize_candidate_meta(
                site_id="fake",
                torrent_id=torrent_id,
                display_name=self._display_name,
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


class _EmptySiteProvider:
    def enabled_adapters(self) -> tuple[EnabledSiteAdapter, ...]:
        return ()

    def enabled_site_versions(self) -> tuple[tuple[str, int], ...]:
        return ()


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
        with app.state.runtime.session_factory() as session:
            task = TaskRepository(session).get(task_id)
            assert task is not None
            assert task.status == TaskStatus.PREFLIGHT.value
            assert analyzed.json()["payload"]["task_version"] == task.version
            events = list(
                session.scalars(
                    select(TaskEvent)
                    .where(TaskEvent.task_id == task_id)
                    .order_by(TaskEvent.created_at, TaskEvent.id)
                )
            )
            assert [item.event_type for item in events[-5:]] == [
                "ANALYSIS_STARTED",
                "ANALYSIS_SEARCHING",
                "ANALYSIS_MATCHING",
                "ANALYSIS_VERIFYING",
                "ANALYSIS_PREFLIGHT_READY",
            ]

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
                to_status=TaskStatus.RETRY,
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


def test_task_analyze_failure_recovers_owned_state_to_retry(tmp_path: Path) -> None:
    client, app, settings = _authenticated_client(tmp_path)
    source_root = settings.data_dir / "retry"
    source_root.mkdir(parents=True)
    content = b"0123456789abcdef"
    source_file = source_root / "Movie.2026.mkv"
    source_file.write_bytes(content)
    unit = identify_task_units((SourceTaskFile(source_file.name, len(content)),))[0]
    task_id = _create_task(app, unit.normalized_unit_key)
    app.state.task_analysis_service = TaskAnalysisService(
        app.state.runtime.session_factory,
        _EmptySiteProvider(),
        data_root=settings.data_dir,
    )

    try:
        failed = client.post(
            f"/api/v1/tasks/{task_id}/actions",
            headers=_csrf(client),
            json={"action": "analyze", "source_root": "retry"},
        )
        assert failed.status_code == 409
        assert failed.json()["code"] == "ANALYSIS_NO_ENABLED_SITES"
        task = client.get(f"/api/v1/tasks/{task_id}").json()
        assert task["status"] == "RETRY"
        with app.state.runtime.session_factory() as session:
            events = list(
                session.scalars(
                    select(TaskEvent)
                    .where(TaskEvent.task_id == task_id)
                    .order_by(TaskEvent.created_at, TaskEvent.id)
                )
            )
        assert [item.event_type for item in events[-2:]] == [
            "ANALYSIS_STARTED",
            "ANALYSIS_RETRY_REQUIRED",
        ]
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


def test_task_review_is_versioned_and_opens_awaiting_confirmation_bridge(tmp_path: Path) -> None:
    client, app, settings = _authenticated_client(tmp_path)
    source_root = settings.data_dir / "review"
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

    try:
        analyzed = client.post(
            f"/api/v1/tasks/{task_id}/actions",
            headers=_csrf(client),
            json={"action": "analyze", "source_root": "review"},
        )
        assert analyzed.status_code == 200
        unit_id = client.get(f"/api/v1/tasks/{task_id}/units").json()["items"][0]["id"]
        candidate_id = client.get(f"/api/v1/tasks/{task_id}/candidates").json()["items"][0]["id"]

        missing = client.get(f"/api/v1/task-units/{unit_id}/decision")
        assert missing.status_code == 404
        assert missing.json()["code"] == "REVIEW_NOT_FOUND"

        payload = {
            "expected_version": 0,
            "approved_candidate_id": candidate_id,
            "rejected_candidate_ids": [],
            "manual_mappings": [],
            "note": "合成审核通过",
        }
        without_csrf = client.post(f"/api/v1/task-units/{unit_id}/decision", json=payload)
        assert without_csrf.status_code == 403

        no_op = client.post(
            f"/api/v1/task-units/{unit_id}/decision",
            headers=_csrf(client),
            json={
                "expected_version": 0,
                "approved_candidate_id": None,
                "rejected_candidate_ids": [],
                "manual_mappings": [],
                "note": None,
            },
        )
        assert no_op.status_code == 422
        assert no_op.json()["code"] == "REVIEW_INPUT_INVALID"
        assert client.get(f"/api/v1/tasks/{task_id}").json()["status"] == "PREFLIGHT"

        created = client.post(
            f"/api/v1/task-units/{unit_id}/decision",
            headers=_csrf(client),
            json=payload,
        )
        assert created.status_code == 200
        assert created.json()["version"] == 1
        assert created.json()["approved_candidate_id"] == candidate_id
        assert created.json()["requires_reverification"] is False
        assert created.json()["execution_allowed"] is False

        task = client.get(f"/api/v1/tasks/{task_id}").json()
        assert task["status"] == "AWAITING_CONFIRMATION"
        bridged_task_version = task["version"]
        current = client.get(f"/api/v1/tasks/{task_id}/preflight/current")
        assert current.status_code == 200
        assert current.json()["current"] is True
        assert current.json()["stale_reasons"] == []

        conflict = client.post(
            f"/api/v1/task-units/{unit_id}/decision",
            headers=_csrf(client),
            json=payload,
        )
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "REVIEW_VERSION_CONFLICT"

        rejected = client.post(
            f"/api/v1/task-units/{unit_id}/decision",
            headers=_csrf(client),
            json={
                "expected_version": 1,
                "approved_candidate_id": None,
                "rejected_candidate_ids": [candidate_id],
                "manual_mappings": [],
                "note": "改为拒绝",
            },
        )
        assert rejected.status_code == 200
        assert rejected.json()["version"] == 2
        assert rejected.json()["rejected_candidate_ids"] == [candidate_id]
        assert client.get(f"/api/v1/tasks/{task_id}").json()["version"] == bridged_task_version

        latest = client.get(f"/api/v1/task-units/{unit_id}/decision")
        assert latest.status_code == 200
        assert latest.json()["version"] == 2
        with app.state.runtime.session_factory() as session:
            assert session.scalar(select(func.count()).select_from(TaskReviewRevisionRecord)) == 2
    finally:
        client.__exit__(None, None, None)


def test_manual_review_mapping_only_accepts_current_ambiguous_candidates(tmp_path: Path) -> None:
    client, app, settings = _authenticated_client(tmp_path)
    source_root = settings.data_dir / "ambiguous"
    (source_root / "one").mkdir(parents=True)
    (source_root / "two").mkdir(parents=True)
    content = b"same-size-content"
    first = source_root / "one" / "Movie.2026.mkv"
    second = source_root / "two" / "Movie.2026.mkv"
    first.write_bytes(content)
    second.write_bytes(content)
    units = identify_task_units(
        (
            SourceTaskFile("one/Movie.2026.mkv", len(content)),
            SourceTaskFile("two/Movie.2026.mkv", len(content)),
        )
    )
    task_id = _create_task(app, units[0].normalized_unit_key)
    app.state.task_analysis_service = TaskAnalysisService(
        app.state.runtime.session_factory,
        _FakeSiteProvider(_FakeAdapter(_v1_torrent(b"Movie.2026.mkv", content, piece_length=4))),
        data_root=settings.data_dir,
    )

    try:
        analyzed = client.post(
            f"/api/v1/tasks/{task_id}/actions",
            headers=_csrf(client),
            json={"action": "analyze", "source_root": "ambiguous"},
        )
        assert analyzed.status_code == 200
        current_unit = next(
            item
            for item in client.get(f"/api/v1/tasks/{task_id}/units").json()["items"]
            if item["normalized_unit_key"] == units[0].normalized_unit_key
        )
        candidate = client.get(f"/api/v1/tasks/{task_id}/candidates").json()["items"][0]
        assert candidate["evidence"]["mappings"][0]["state"] == "AMBIGUOUS"

        invalid = client.post(
            f"/api/v1/task-units/{current_unit['id']}/decision",
            headers=_csrf(client),
            json={
                "expected_version": 0,
                "approved_candidate_id": candidate["id"],
                "rejected_candidate_ids": [],
                "manual_mappings": [
                    {
                        "torrent_path": "Movie.2026.mkv",
                        "source_relative_path": "../outside.mkv",
                    }
                ],
            },
        )
        assert invalid.status_code == 422
        assert invalid.json()["code"] == "REVIEW_INPUT_INVALID"

        mapped = client.post(
            f"/api/v1/task-units/{current_unit['id']}/decision",
            headers=_csrf(client),
            json={
                "expected_version": 0,
                "approved_candidate_id": candidate["id"],
                "rejected_candidate_ids": [],
                "manual_mappings": [
                    {
                        "torrent_path": "Movie.2026.mkv",
                        "source_relative_path": "one/Movie.2026.mkv",
                    }
                ],
            },
        )
        assert mapped.status_code == 200
        assert mapped.json()["requires_reverification"] is True
        assert mapped.json()["execution_allowed"] is False
        assert mapped.json()["manual_mappings"] == [
            {
                "torrent_path": "Movie.2026.mkv",
                "source_relative_path": "one/Movie.2026.mkv",
            }
        ]

        missing_verification = client.get(
            f"/api/v1/task-units/{current_unit['id']}/decision/verification"
        )
        assert missing_verification.status_code == 404
        assert missing_verification.json()["code"] == "REVIEW_VERIFICATION_NOT_FOUND"

        without_csrf = client.post(
            f"/api/v1/task-units/{current_unit['id']}/decision/actions",
            json={"action": "reverify"},
        )
        assert without_csrf.status_code == 403

        verified = client.post(
            f"/api/v1/task-units/{current_unit['id']}/decision/actions",
            headers=_csrf(client),
            json={"action": "reverify"},
        )
        assert verified.status_code == 200
        assert verified.json()["review_version"] == 1
        assert verified.json()["candidate_id"] == candidate["id"]
        assert verified.json()["verification_level"] == "FULL_VERIFIED"
        assert verified.json()["execution_allowed"] is False

        loaded = client.get(f"/api/v1/task-units/{current_unit['id']}/decision/verification")
        assert loaded.status_code == 200
        assert loaded.json()["verification_digest"] == verified.json()["verification_digest"]

        repeated = client.post(
            f"/api/v1/task-units/{current_unit['id']}/decision/actions",
            headers=_csrf(client),
            json={"action": "reverify"},
        )
        assert repeated.status_code == 200
        assert repeated.json()["id"] == verified.json()["id"]

        first.write_bytes(b"changed-content!!!")
        os.utime(first, None)
        stale_reverify = client.post(
            f"/api/v1/task-units/{current_unit['id']}/decision/actions",
            headers=_csrf(client),
            json={"action": "reverify"},
        )
        assert stale_reverify.status_code == 409
        assert stale_reverify.json()["code"] == "REVIEW_PREFLIGHT_STALE"
        with app.state.runtime.session_factory() as session:
            assert (
                session.scalar(select(func.count()).select_from(TaskReviewVerificationRecord)) == 1
            )
    finally:
        client.__exit__(None, None, None)


def test_review_rejects_stale_preflight_before_writing_revision(tmp_path: Path) -> None:
    client, app, settings = _authenticated_client(tmp_path)
    source_root = settings.data_dir / "stale-review"
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

    try:
        assert (
            client.post(
                f"/api/v1/tasks/{task_id}/actions",
                headers=_csrf(client),
                json={"action": "analyze", "source_root": "stale-review"},
            ).status_code
            == 200
        )
        unit_id = client.get(f"/api/v1/tasks/{task_id}/units").json()["items"][0]["id"]
        candidate_id = client.get(f"/api/v1/tasks/{task_id}/candidates").json()["items"][0]["id"]
        source_file.write_bytes(b"fedcba9876543210")
        os.utime(source_file, None)

        stale = client.post(
            f"/api/v1/task-units/{unit_id}/decision",
            headers=_csrf(client),
            json={
                "expected_version": 0,
                "approved_candidate_id": candidate_id,
                "rejected_candidate_ids": [],
                "manual_mappings": [],
            },
        )
        assert stale.status_code == 409
        assert stale.json()["code"] == "REVIEW_PREFLIGHT_STALE"
        with app.state.runtime.session_factory() as session:
            assert session.scalar(select(func.count()).select_from(TaskReviewRevisionRecord)) == 0
    finally:
        client.__exit__(None, None, None)


def test_review_cannot_approve_hard_rejected_candidate(tmp_path: Path) -> None:
    client, app, settings = _authenticated_client(tmp_path)
    source_root = settings.data_dir / "hard-conflict"
    source_root.mkdir(parents=True)
    content = b"0123456789abcdef"
    source_file = source_root / "Movie.2026.mkv"
    source_file.write_bytes(content)
    unit = identify_task_units((SourceTaskFile(source_file.name, len(content)),))[0]
    task_id = _create_task(app, unit.normalized_unit_key)
    app.state.task_analysis_service = TaskAnalysisService(
        app.state.runtime.session_factory,
        _FakeSiteProvider(
            _FakeAdapter(
                _v1_torrent(source_file.name.encode(), content, piece_length=4),
                display_name="Movie.2025",
            )
        ),
        data_root=settings.data_dir,
    )

    try:
        assert (
            client.post(
                f"/api/v1/tasks/{task_id}/actions",
                headers=_csrf(client),
                json={"action": "analyze", "source_root": "hard-conflict"},
            ).status_code
            == 200
        )
        unit_id = client.get(f"/api/v1/tasks/{task_id}/units").json()["items"][0]["id"]
        candidate = client.get(f"/api/v1/tasks/{task_id}/candidates").json()["items"][0]
        assert candidate["rejected"] is True

        response = client.post(
            f"/api/v1/task-units/{unit_id}/decision",
            headers=_csrf(client),
            json={
                "expected_version": 0,
                "approved_candidate_id": candidate["id"],
                "rejected_candidate_ids": [],
                "manual_mappings": [],
            },
        )
        assert response.status_code == 409
        assert response.json()["code"] == "REVIEW_CANDIDATE_HARD_REJECTED"
        with app.state.runtime.session_factory() as session:
            assert session.scalar(select(func.count()).select_from(TaskReviewRevisionRecord)) == 0
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
