from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from backend.app.api.dependencies import CSRF_COOKIE
from backend.app.application.errors import ApplicationError
from backend.app.application.sites import EnabledSiteAdapter
from backend.app.application.task_actions import (
    CancelTaskAction,
    ExecuteTaskAction,
    TaskActionActor,
    TaskMutationActionResult,
)
from backend.app.application.task_repair_actions import TaskRepairActionResult
from backend.app.application.task_repairs import RepairPlanView
from backend.app.application.tasks import TaskAnalysisService
from backend.app.config import AppSettings
from backend.app.domain.operation import OperationStatus
from backend.app.domain.repair import RepairAction, RepairActionKind, RepairMode, RepairPlan
from backend.app.domain.site_adapter import SiteConnectionResult, TorrentDetails, TorrentPayload
from backend.app.domain.site_search import (
    SearchPage,
    SearchQuery,
    SiteSearchCapabilities,
    normalize_candidate_meta,
)
from backend.app.domain.task_state import TaskStatus
from backend.app.domain.task_units import SourceTaskFile, identify_task_units
from backend.app.domain.torrent import TorrentKind
from backend.app.domain.verification import DownloaderKind
from backend.app.infrastructure.persistence.models import (
    Downloader,
    OperationJournal,
    OperationJournalTombstone,
    TaskCandidateRecord,
    TaskEvent,
    TaskExecutionGateRecord,
    TaskExecutionPlanRecord,
    TaskReviewRevisionRecord,
    TaskReviewVerificationRecord,
    TaskUnitRecord,
    UnpackTask,
    new_uuid,
)
from backend.app.infrastructure.persistence.repositories import (
    OperationIntent,
    OperationJournalRepository,
    TaskCreate,
    TaskRepository,
)
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

        gate_without_csrf = client.post(f"/api/v1/task-units/{unit_id}/execution-gate")
        assert gate_without_csrf.status_code == 403
        gate = client.post(
            f"/api/v1/task-units/{unit_id}/execution-gate",
            headers=_csrf(client),
        )
        assert gate.status_code == 200
        assert gate.json()["eligible"] is True
        assert gate.json()["current"] is True
        assert gate.json()["client_check_required"] is False
        assert gate.json()["verification_level"] == "FULL_VERIFIED"
        assert gate.json()["verification_source"] == "PREFLIGHT"
        assert gate.json()["blocked_reasons"] == []
        assert gate.json()["side_effects_started"] is False
        loaded_gate = client.get(f"/api/v1/task-units/{unit_id}/execution-gate")
        assert loaded_gate.status_code == 200
        assert loaded_gate.json()["gate_digest"] == gate.json()["gate_digest"]
        assert loaded_gate.json()["current"] is True

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
        stale_gate = client.get(f"/api/v1/task-units/{unit_id}/execution-gate")
        assert stale_gate.status_code == 200
        assert stale_gate.json()["current"] is False

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

        blocked_gate = client.post(
            f"/api/v1/task-units/{current_unit['id']}/execution-gate",
            headers=_csrf(client),
        )
        assert blocked_gate.status_code == 200
        assert blocked_gate.json()["eligible"] is False
        assert blocked_gate.json()["blocked_reasons"] == ["REVERIFICATION_REQUIRED"]
        assert blocked_gate.json()["side_effects_started"] is False

        blocked_plan = client.post(
            f"/api/v1/task-units/{current_unit['id']}/execution-plan",
            headers=_csrf(client),
            json={"target_root": "ambiguous", "target_downloader_id": "blocked-target"},
        )
        assert blocked_plan.status_code == 409
        assert blocked_plan.json()["code"] == "EXECUTION_PLAN_GATE_NOT_READY"

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

        eligible_gate = client.post(
            f"/api/v1/task-units/{current_unit['id']}/execution-gate",
            headers=_csrf(client),
        )
        assert eligible_gate.status_code == 200
        assert eligible_gate.json()["eligible"] is True
        assert eligible_gate.json()["client_check_required"] is False
        assert eligible_gate.json()["verification_level"] == "FULL_VERIFIED"
        assert eligible_gate.json()["verification_source"] == "REVIEW_REVERIFICATION"
        assert eligible_gate.json()["blocked_reasons"] == []
        assert eligible_gate.json()["side_effects_started"] is False

        target_root = settings.data_dir / "seeding-target"
        target_root.mkdir()
        target_downloader_id = _create_ready_qb_target(app, settings)
        invalid_target = client.post(
            f"/api/v1/task-units/{current_unit['id']}/execution-plan",
            headers=_csrf(client),
            json={
                "target_root": "../outside",
                "target_downloader_id": target_downloader_id,
            },
        )
        assert invalid_target.status_code == 422
        assert invalid_target.json()["code"] == "EXECUTION_PLAN_TARGET_ROOT_INVALID"

        without_plan_csrf = client.post(
            f"/api/v1/task-units/{current_unit['id']}/execution-plan",
            json={
                "target_root": "seeding-target",
                "target_downloader_id": target_downloader_id,
            },
        )
        assert without_plan_csrf.status_code == 403

        plan = client.post(
            f"/api/v1/task-units/{current_unit['id']}/execution-plan",
            headers=_csrf(client),
            json={
                "target_root": "seeding-target",
                "target_downloader_id": target_downloader_id,
            },
        )
        assert plan.status_code == 200
        plan_body = plan.json()
        assert plan_body["ready"] is True
        assert plan_body["current"] is True
        assert plan_body["current_reasons"] == []
        assert plan_body["hardlink_count"] == 1
        assert plan_body["client_fetch_count"] == 0
        assert plan_body["estimated_download_bytes_upper_bound"] == 0
        assert plan_body["execution_allowed"] is False
        assert plan_body["side_effects_started"] is False
        assert plan_body["target_downloader_id"] == target_downloader_id
        assert plan_body["target_downloader_version"] == 1
        assert plan_body["target_remote_save_path"] == "/downloads/seeding-target"
        assert plan_body["actions"] == [
            {
                "torrent_path": "Movie.2026.mkv",
                "kind": "HARDLINK",
                "length": len(content),
                "source_relative_path": "one/Movie.2026.mkv",
            }
        ]
        assert "/workspace" not in plan.text
        assert str(settings.data_dir) not in plan.text

        repeated_plan = client.post(
            f"/api/v1/task-units/{current_unit['id']}/execution-plan",
            headers=_csrf(client),
            json={
                "target_root": "seeding-target",
                "target_downloader_id": target_downloader_id,
            },
        )
        assert repeated_plan.status_code == 200
        assert repeated_plan.json()["id"] == plan_body["id"]
        assert repeated_plan.json()["plan_digest"] == plan_body["plan_digest"]

        with app.state.runtime.session_factory() as session:
            downloader = session.get(Downloader, target_downloader_id)
            assert downloader is not None
            downloader.capabilities = {**downloader.capabilities, "supports_skip_checking": False}
            session.commit()
        downloader_stale = client.get(f"/api/v1/task-units/{current_unit['id']}/execution-plan")
        assert downloader_stale.status_code == 200
        assert downloader_stale.json()["current"] is False
        assert downloader_stale.json()["current_reasons"] == ["TARGET_DOWNLOADER_CHANGED"]
        with app.state.runtime.session_factory() as session:
            downloader = session.get(Downloader, target_downloader_id)
            assert downloader is not None
            downloader.capabilities = {**downloader.capabilities, "supports_skip_checking": True}
            session.commit()

        (target_root / "Movie.2026.mkv").write_bytes(b"conflict")
        stale_plan = client.get(f"/api/v1/task-units/{current_unit['id']}/execution-plan")
        assert stale_plan.status_code == 200
        assert stale_plan.json()["current"] is False
        assert stale_plan.json()["current_reasons"] == ["TARGET_STATE_CHANGED"]

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
        stale_gate = client.get(f"/api/v1/task-units/{current_unit['id']}/execution-gate")
        assert stale_gate.status_code == 200
        assert stale_gate.json()["current"] is False
        with app.state.runtime.session_factory() as session:
            assert (
                session.scalar(select(func.count()).select_from(TaskReviewVerificationRecord)) == 1
            )
            assert session.scalar(select(func.count()).select_from(TaskExecutionGateRecord)) == 2
            assert session.scalar(select(func.count()).select_from(TaskExecutionPlanRecord)) == 1
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


class _FakePublicTaskActions:
    def __init__(self) -> None:
        self.calls: list[tuple[str, TaskActionActor, str | None]] = []

    async def execute(
        self,
        request: ExecuteTaskAction,
        *,
        actor: TaskActionActor,
        idempotency_key: str | None,
    ) -> TaskMutationActionResult:
        self.calls.append(("execute", actor, idempotency_key))
        return TaskMutationActionResult(
            action="execute",
            task_id=request.task_id,
            status=TaskStatus.ADDING,
            task_version=7,
            execution_plan_id=request.execution_plan_id,
            operation_replayed=False,
            receipt_id="receipt-execute",
            idempotency_replayed=False,
        )

    async def cancel(
        self,
        request: CancelTaskAction,
        *,
        actor: TaskActionActor,
        idempotency_key: str | None,
    ) -> TaskMutationActionResult:
        self.calls.append(("cancel", actor, idempotency_key))
        return TaskMutationActionResult(
            action="cancel",
            task_id=request.task_id,
            status=TaskStatus.CANCELLED,
            task_version=8,
            execution_plan_id="plan-cancel",
            operation_replayed=False,
            receipt_id="receipt-cancel",
            idempotency_replayed=False,
        )


def test_public_cancel_pending_task_is_db_only_and_idempotent(tmp_path: Path) -> None:
    client, app, _settings = _authenticated_client(tmp_path)
    task_id = _create_task(app, "public-pre-side-effect-cancel")
    try:
        missing_key = client.post(
            f"/api/v1/tasks/{task_id}/actions",
            headers=_csrf(client),
            json={
                "action": "cancel",
                "remove_downloader_task": False,
                "rollback_created_resources": False,
            },
        )
        assert missing_key.status_code == 428
        assert missing_key.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"

        first = client.post(
            f"/api/v1/tasks/{task_id}/actions",
            headers={**_csrf(client), "Idempotency-Key": "cancel-pending"},
            json={
                "action": "cancel",
                "remove_downloader_task": False,
                "rollback_created_resources": False,
            },
        )
        replayed = client.post(
            f"/api/v1/tasks/{task_id}/actions",
            headers={**_csrf(client), "Idempotency-Key": "cancel-pending"},
            json={
                "action": "cancel",
                "remove_downloader_task": False,
                "rollback_created_resources": False,
            },
        )

        assert first.status_code == replayed.status_code == 200
        assert first.json()["status"] == "CANCELLED"
        assert first.json()["execution_plan_id"] is None
        assert first.json()["idempotency_replayed"] is False
        assert replayed.json()["receipt_id"] == first.json()["receipt_id"]
        assert replayed.json()["idempotency_replayed"] is True
        with app.state.runtime.session_factory() as session:
            task = TaskRepository(session).get(task_id)
            assert task is not None and task.status == TaskStatus.CANCELLED.value
            assert OperationJournalRepository(session).list_for_task(task_id) == []
            events = list(
                session.scalars(
                    select(TaskEvent)
                    .where(TaskEvent.task_id == task_id)
                    .order_by(TaskEvent.created_at, TaskEvent.id)
                )
            )
            assert [item.event_type for item in events[-3:]] == [
                "TASK_CANCEL_REQUESTED",
                "CANCELLATION_STARTED",
                "CANCELLATION_COMPLETED",
            ]
    finally:
        client.__exit__(None, None, None)


def test_public_execute_cancel_actions_enforce_csrf_idempotency_and_explicit_options(
    tmp_path: Path,
) -> None:
    client, app, _settings = _authenticated_client(tmp_path)
    task_id = _create_task(app, "public-actions")
    try:
        missing_key = client.post(
            f"/api/v1/tasks/{task_id}/actions",
            headers=_csrf(client),
            json={"action": "execute", "execution_plan_id": "plan-1"},
        )
        assert missing_key.status_code == 428
        assert missing_key.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"

        invalid_key = client.post(
            f"/api/v1/tasks/{task_id}/actions",
            headers={**_csrf(client), "Idempotency-Key": "contains space"},
            json={
                "action": "cancel",
                "remove_downloader_task": True,
                "rollback_created_resources": True,
            },
        )
        assert invalid_key.status_code == 422
        assert invalid_key.json()["code"] == "IDEMPOTENCY_KEY_INVALID"

        without_csrf = client.post(
            f"/api/v1/tasks/{task_id}/actions",
            headers={"Idempotency-Key": "execute-1"},
            json={"action": "execute", "execution_plan_id": "plan-1"},
        )
        assert without_csrf.status_code == 403

        missing_cancel_option = client.post(
            f"/api/v1/tasks/{task_id}/actions",
            headers={**_csrf(client), "Idempotency-Key": "cancel-missing-option"},
            json={"action": "cancel", "remove_downloader_task": True},
        )
        assert missing_cancel_option.status_code == 422

        fake = _FakePublicTaskActions()
        app.state.task_action_service = fake
        execute = client.post(
            f"/api/v1/tasks/{task_id}/actions",
            headers={**_csrf(client), "Idempotency-Key": "execute-2"},
            json={"action": "execute", "execution_plan_id": "plan-2"},
        )
        assert execute.status_code == 200
        assert execute.json() == {
            "action": "execute",
            "task_id": task_id,
            "status": "ADDING",
            "task_version": 7,
            "execution_plan_id": "plan-2",
            "operation_replayed": False,
            "idempotency_replayed": False,
            "receipt_id": "receipt-execute",
        }

        cancel = client.post(
            f"/api/v1/tasks/{task_id}/actions",
            headers={**_csrf(client), "Idempotency-Key": "cancel-2"},
            json={
                "action": "cancel",
                "remove_downloader_task": False,
                "rollback_created_resources": True,
            },
        )
        assert cancel.status_code == 200
        assert cancel.json()["status"] == "CANCELLED"
        assert cancel.json()["receipt_id"] == "receipt-cancel"
        assert "actor_id" not in cancel.json()
        assert "idempotency_key" not in cancel.json()
        assert [call[0] for call in fake.calls] == ["execute", "cancel"]
        assert all(call[1].kind == "admin_session" for call in fake.calls)
        assert [call[2] for call in fake.calls] == ["execute-2", "cancel-2"]
    finally:
        client.__exit__(None, None, None)


def test_task_events_support_history_cursor_and_sse_resume(tmp_path: Path) -> None:
    client, app, _settings = _authenticated_client(tmp_path)
    task_id = _create_task(app, "events-primary")
    other_task_id = _create_task(app, "events-other")
    try:
        history = client.get(f"/api/v1/tasks/{task_id}/events")
        assert history.status_code == 200
        assert [item["event_type"] for item in history.json()["items"]] == ["TASK_CREATED"]
        first_event_id = history.json()["items"][0]["id"]

        with app.state.runtime.session_factory() as session:
            second = TaskRepository(session).append_event(
                task_id=task_id,
                event_type="TEST_TIMELINE_EVENT",
                reason="测试任务时间线增量投递",
            )
            other = TaskRepository(session).latest_event(other_task_id)
            assert other is not None
            other_event_id = other.id
            session.commit()
            second_event_id = second.id

        incremental = client.get(
            f"/api/v1/tasks/{task_id}/events",
            params={"after_event_id": first_event_id},
        )
        assert incremental.status_code == 200
        assert [item["id"] for item in incremental.json()["items"]] == [second_event_id]
        assert incremental.json()["items"][0]["reason"] == "测试任务时间线增量投递"

        wrong_cursor = client.get(
            f"/api/v1/tasks/{task_id}/events",
            params={"after_event_id": other_event_id},
        )
        assert wrong_cursor.status_code == 409
        assert wrong_cursor.json()["code"] == "TASK_EVENT_CURSOR_INVALID"

        stream = client.get(
            f"/api/v1/tasks/{task_id}/events/stream",
            params={"after_event_id": first_event_id},
        )
        assert stream.status_code == 200
        assert stream.headers["content-type"].startswith("text/event-stream")
        assert stream.headers["cache-control"] == "no-cache, no-store"
        assert stream.headers["x-accel-buffering"] == "no"
        assert f"id: {second_event_id}\n" in stream.text
        assert "event: task-event\n" in stream.text
        assert '"event_type":"TEST_TIMELINE_EVENT"' in stream.text
        assert '"task_id":"' + task_id + '"' in stream.text
        assert "checkpoint" not in stream.text
    finally:
        client.__exit__(None, None, None)


def test_task_operation_api_redacts_journal_and_reconciles_idempotently(tmp_path: Path) -> None:
    client, app, settings = _authenticated_client(tmp_path)
    task_id = _create_task(app, "operation-center")
    target_root = settings.data_dir / "target"
    target_root.mkdir(parents=True, exist_ok=True)
    target = target_root / "movie.mkv"
    target.write_bytes(b"synthetic-owned-file")
    observed = target.stat(follow_symlinks=False)

    with app.state.runtime.session_factory() as session:
        repository = OperationJournalRepository(session)
        journal, _ = repository.record_intent(
            OperationIntent(
                task_id=task_id,
                idempotency_key="f" * 64,
                operation_type="CREATE_HARDLINK",
                target={"target_root": "target", "relative_path": "movie.mkv"},
                intent={"source_relative_path": "private/source/movie.mkv"},
                before_snapshot={"secret": "must-not-leak"},
            )
        )
        repository.transition_status(
            journal_id=journal.id,
            expected_status=OperationStatus.INTENT_RECORDED,
            to_status=OperationStatus.APPLIED,
            after_snapshot={
                "device": observed.st_dev,
                "inode": observed.st_ino,
                "size": observed.st_size,
                "mtime_ns": observed.st_mtime_ns,
                "file_type": "regular",
                "link_count": observed.st_nlink,
            },
        )
        repository.transition_status(
            journal_id=journal.id,
            expected_status=OperationStatus.APPLIED,
            to_status=OperationStatus.RECONCILE_REQUIRED,
        )
        session.commit()
        journal_id = journal.id

    try:
        listing = client.get(f"/api/v1/tasks/{task_id}/operations")
        assert listing.status_code == 200
        assert len(listing.json()["items"]) == 1
        item = listing.json()["items"][0]
        assert item["id"] == journal_id
        assert item["kind"] == "FILESYSTEM_HARDLINK"
        assert item["status"] == "RECONCILE_REQUIRED"
        assert item["attention_required"] is True
        assert item["reconcile_supported"] is True
        assert set(item) == {
            "id",
            "task_id",
            "kind",
            "status",
            "attention_required",
            "reconcile_supported",
            "created_at",
            "updated_at",
        }
        encoded = listing.text
        assert '"relative_path"' not in encoded
        assert "movie.mkv" not in encoded
        assert "private/source/movie.mkv" not in encoded
        assert "must-not-leak" not in encoded
        assert "ffffffffffffffff" not in encoded

        without_csrf = client.post(
            f"/api/v1/tasks/{task_id}/operations/{journal_id}/actions",
            headers={"Idempotency-Key": "operation-reconcile"},
            json={"action": "reconcile"},
        )
        assert without_csrf.status_code == 403

        missing_key = client.post(
            f"/api/v1/tasks/{task_id}/operations/{journal_id}/actions",
            headers=_csrf(client),
            json={"action": "reconcile"},
        )
        assert missing_key.status_code == 428
        assert missing_key.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"

        first = client.post(
            f"/api/v1/tasks/{task_id}/operations/{journal_id}/actions",
            headers={**_csrf(client), "Idempotency-Key": "operation-reconcile"},
            json={"action": "reconcile"},
        )
        assert first.status_code == 200
        assert first.json()["status"] == "APPLIED"
        assert first.json()["kind"] == "FILESYSTEM_HARDLINK"
        assert first.json()["idempotency_replayed"] is False
        receipt_id = first.json()["receipt_id"]

        replay = client.post(
            f"/api/v1/tasks/{task_id}/operations/{journal_id}/actions",
            headers={**_csrf(client), "Idempotency-Key": "operation-reconcile"},
            json={"action": "reconcile"},
        )
        assert replay.status_code == 200
        assert replay.json()["receipt_id"] == receipt_id
        assert replay.json()["idempotency_replayed"] is True
    finally:
        client.__exit__(None, None, None)


def test_operation_maintenance_report_is_read_only_redacted_and_bounded(tmp_path: Path) -> None:
    client, app, _settings = _authenticated_client(tmp_path)
    task_id = _create_task(app, "maintenance-report")
    secret_marker = "/data/private/report-must-not-leak"
    ownership_marker = "private-maintenance-owner"

    with app.state.runtime.session_factory() as session:
        repository = OperationJournalRepository(session)

        supported, _ = repository.record_intent(
            OperationIntent(
                task_id=task_id,
                idempotency_key="1" * 64,
                operation_type="CREATE_DIRECTORY",
                target={"path": secret_marker},
                intent={"ownership_tag": ownership_marker},
                before_snapshot={"path": secret_marker},
            )
        )
        repository.transition_status(
            journal_id=supported.id,
            expected_status=OperationStatus.INTENT_RECORDED,
            to_status=OperationStatus.APPLIED,
            after_snapshot={"path": secret_marker},
        )
        repository.transition_status(
            journal_id=supported.id,
            expected_status=OperationStatus.APPLIED,
            to_status=OperationStatus.RECONCILE_REQUIRED,
        )

        manual, _ = repository.record_intent(
            OperationIntent(
                task_id=task_id,
                idempotency_key="2" * 64,
                operation_type="UNKNOWN_SIDE_EFFECT",
                target={"path": secret_marker},
                intent={"ownership_tag": ownership_marker},
            )
        )
        repository.transition_status(
            journal_id=manual.id,
            expected_status=OperationStatus.INTENT_RECORDED,
            to_status=OperationStatus.RECONCILE_REQUIRED,
        )

        blocked, _ = repository.record_intent(
            OperationIntent(
                task_id=task_id,
                idempotency_key="3" * 64,
                operation_type="CREATE_HARDLINK",
                target={"path": secret_marker},
                intent={"ownership_tag": ownership_marker},
            )
        )
        repository.transition_status(
            journal_id=blocked.id,
            expected_status=OperationStatus.INTENT_RECORDED,
            to_status=OperationStatus.APPLIED,
            after_snapshot={"path": secret_marker},
        )
        repository.transition_status(
            journal_id=blocked.id,
            expected_status=OperationStatus.APPLIED,
            to_status=OperationStatus.ROLLBACK_PENDING,
        )
        repository.transition_status(
            journal_id=blocked.id,
            expected_status=OperationStatus.ROLLBACK_PENDING,
            to_status=OperationStatus.ROLLBACK_BLOCKED,
        )

        noop, _ = repository.record_intent(
            OperationIntent(
                task_id=task_id,
                idempotency_key="4" * 64,
                operation_type="CREATE_DIRECTORY",
                target={"path": secret_marker},
                intent={"ownership_tag": ownership_marker},
            )
        )
        repository.transition_status(
            journal_id=noop.id,
            expected_status=OperationStatus.INTENT_RECORDED,
            to_status=OperationStatus.NOOP,
        )
        session.commit()

    try:
        response = client.get("/api/v1/operations/maintenance-report?limit=100")
        assert response.status_code == 200
        payload = response.json()
        assert payload["summary"] == {
            "total_journals": 4,
            "attention_required": 3,
            "reconcile_supported": 1,
            "manual_only": 2,
            "retention_candidates": 1,
            "truncated": False,
        }
        assert {item["reason_code"] for item in payload["repair_items"]} == {
            "SAFE_RECONCILE_AVAILABLE",
            "MANUAL_RECONCILE_REQUIRED",
            "ROLLBACK_BLOCKED",
        }
        assert payload["cleanup_candidates"][0]["journal_id"] == noop.id
        assert payload["cleanup_candidates"][0]["reason_code"] == "NO_SIDE_EFFECT"

        allowed_repair_fields = {
            "journal_id",
            "task_id",
            "kind",
            "status",
            "reason_code",
            "reason",
            "recommended_action",
            "action",
            "reconcile_supported",
            "manual_required",
            "created_at",
            "updated_at",
        }
        assert all(set(item) == allowed_repair_fields for item in payload["repair_items"])
        encoded = response.text
        for forbidden in (
            secret_marker,
            ownership_marker,
            "target",
            "intent",
            "before_snapshot",
            "after_snapshot",
            "idempotency_key",
        ):
            assert forbidden not in encoded

        assert client.get("/api/v1/operations/maintenance-report?limit=0").status_code == 422
        assert client.get("/api/v1/operations/maintenance-report?limit=501").status_code == 422
    finally:
        client.__exit__(None, None, None)


def test_operation_retention_plan_and_purge_api_are_redacted_and_idempotent(tmp_path: Path) -> None:
    client, app, _settings = _authenticated_client(tmp_path)
    task_id = _create_task(app, "operation-retention")
    secret_marker = "/data/private/retention-api-must-not-leak"
    old = datetime.now(UTC) - timedelta(days=60)

    with app.state.runtime.session_factory() as session:
        repository = OperationJournalRepository(session)
        journal, _ = repository.record_intent(
            OperationIntent(
                task_id=task_id,
                idempotency_key="6" * 64,
                operation_type="CREATE_DIRECTORY",
                target={"path": secret_marker},
                intent={"secret": secret_marker},
            )
        )
        repository.transition_status(
            journal_id=journal.id,
            expected_status=OperationStatus.INTENT_RECORDED,
            to_status=OperationStatus.NOOP,
        )
        journal.updated_at = old
        task = session.get(UnpackTask, task_id)
        assert task is not None
        task.status = TaskStatus.DONE.value
        task.checkpoint = {}
        task.updated_at = old
        session.commit()
        journal_id = journal.id

    try:
        plan = client.get("/api/v1/operations/retention-plan?retention_days=30&limit=100")
        assert plan.status_code == 200
        payload = plan.json()
        assert payload["retention_days"] == 30
        assert payload["summary"] == {
            "candidates": 1,
            "inspected": 1,
            "eligible": 1,
            "blocked": 0,
            "truncated": False,
        }
        assert payload["items"] == [
            {
                "journal_id": journal_id,
                "task_id": task_id,
                "kind": "FILESYSTEM_DIRECTORY",
                "status": "NOOP",
                "eligible": True,
                "reason_code": "ELIGIBLE",
                "updated_at": payload["items"][0]["updated_at"],
            }
        ]
        assert secret_marker not in plan.text
        assert "target" not in plan.text
        assert "intent" not in plan.text
        assert "idempotency_key" not in plan.text

        without_csrf = client.post(
            f"/api/v1/tasks/{task_id}/operations/{journal_id}/actions",
            headers={"Idempotency-Key": "operation-retention-purge"},
            json={"action": "purge", "retention_days": 30},
        )
        assert without_csrf.status_code == 403

        missing_key = client.post(
            f"/api/v1/tasks/{task_id}/operations/{journal_id}/actions",
            headers=_csrf(client),
            json={"action": "purge", "retention_days": 30},
        )
        assert missing_key.status_code == 428
        assert missing_key.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"

        first = client.post(
            f"/api/v1/tasks/{task_id}/operations/{journal_id}/actions",
            headers={**_csrf(client), "Idempotency-Key": "operation-retention-purge"},
            json={"action": "purge", "retention_days": 30},
        )
        assert first.status_code == 200
        assert first.json()["action"] == "purge"
        assert first.json()["final_status"] == "NOOP"
        assert first.json()["kind"] == "FILESYSTEM_DIRECTORY"
        assert first.json()["purged"] is True
        assert first.json()["idempotency_replayed"] is False
        receipt_id = first.json()["receipt_id"]

        replay = client.post(
            f"/api/v1/tasks/{task_id}/operations/{journal_id}/actions",
            headers={**_csrf(client), "Idempotency-Key": "operation-retention-purge"},
            json={"action": "purge", "retention_days": 30},
        )
        assert replay.status_code == 200
        assert replay.json()["receipt_id"] == receipt_id
        assert replay.json()["idempotency_replayed"] is True

        listing = client.get(f"/api/v1/tasks/{task_id}/operations")
        assert listing.status_code == 200
        assert listing.json()["items"] == []
        with app.state.runtime.session_factory() as session:
            assert session.get(OperationJournal, journal_id) is None
            tombstone = session.get(OperationJournalTombstone, journal_id)
            assert tombstone is not None
            assert tombstone.task_id == task_id
            assert tombstone.final_status == OperationStatus.NOOP.value

        assert client.get("/api/v1/operations/retention-plan?retention_days=0").status_code == 422
        assert client.get("/api/v1/operations/retention-plan?limit=501").status_code == 422
    finally:
        client.__exit__(None, None, None)


def test_transmission_plan_binding_is_allowed_and_requires_verify_capabilities(
    tmp_path: Path,
) -> None:
    client, app, settings = _authenticated_client(tmp_path)
    try:
        target_root = settings.data_dir / "tr-plan-target"
        target_root.mkdir()
        downloader_id = _create_ready_transmission_target(app, settings)
        service = app.state.task_analysis_service

        with app.state.runtime.session_factory() as session:
            binding = service._target_downloader_plan_binding(  # noqa: SLF001
                session,
                downloader_id,
                target_root,
                require_client_verification=False,
            )
        assert binding.downloader_id == downloader_id
        assert binding.downloader_version == 1
        assert binding.remote_save_path == "/downloads/tr-plan-target"

        with app.state.runtime.session_factory() as session:
            downloader = session.get(Downloader, downloader_id)
            assert downloader is not None
            downloader.capabilities = {
                **downloader.capabilities,
                "supports_force_recheck": False,
            }
            session.commit()

        with (
            app.state.runtime.session_factory() as session,
            pytest.raises(ApplicationError) as failure,
        ):
            service._target_downloader_plan_binding(  # noqa: SLF001
                session,
                downloader_id,
                target_root,
                require_client_verification=False,
            )
        assert failure.value.code == "EXECUTION_PLAN_TARGET_DOWNLOADER_NOT_READY"
    finally:
        client.__exit__(None, None, None)


def test_repair_plan_endpoint_is_read_only_redacted_and_mode_only(tmp_path: Path) -> None:
    client, app, _settings = _authenticated_client(tmp_path)

    class _RepairPlanStub:
        def __init__(self) -> None:
            self.calls: list[tuple[str, RepairMode]] = []

        async def generate(self, unit_id: str, *, mode: RepairMode) -> RepairPlanView:
            self.calls.append((unit_id, mode))
            return RepairPlanView(
                task_id="task-safe",
                task_unit_id=unit_id,
                execution_plan_id="plan-safe",
                downloader_kind=DownloaderKind.QBITTORRENT,
                evidence_source="CLIENT_VERIFICATION_INCOMPLETE",
                plan=RepairPlan(
                    mode=mode,
                    torrent_kind=TorrentKind.V1,
                    affected_pieces=(),
                    cross_file_pieces=(),
                    affected_files=(),
                    actions=(
                        RepairAction(
                            RepairActionKind.MANUAL_GUIDANCE,
                            None,
                            "只读人工引导",
                        ),
                    ),
                    isolation_bytes_required=0,
                    estimated_download_bytes_upper_bound=0,
                    required_free_bytes=0,
                    available_bytes=1024,
                    downloader_paused=True,
                    blocked_reasons=(),
                    ready=True,
                    execution_allowed=False,
                ),
            )

    stub = _RepairPlanStub()
    app.state.task_repair_plan_service = stub
    try:
        response = client.get(
            "/api/v1/task-units/unit-safe/repair-plan",
            params={"mode": "GUIDED"},
        )
        assert response.status_code == 200
        payload = response.json()
        assert stub.calls == [("unit-safe", RepairMode.GUIDED)]
        assert payload["mode"] == "GUIDED"
        assert payload["execution_allowed"] is False
        assert payload["evidence_source"] == "CLIENT_VERIFICATION_INCOMPLETE"
        assert payload["actions"][0]["kind"] == "MANUAL_GUIDANCE"
        encoded = response.text
        for forbidden in (
            "torrent_hash",
            "ownership_tag",
            "journal_id",
            "source_path",
            "target_inode",
            "target_device",
            "/data/private-canary",
        ):
            assert forbidden not in encoded

        invalid_mode = client.get(
            "/api/v1/task-units/unit/repair-plan",
            params={"mode": "CLIENT_SUPPLIED_INODE"},
        )
        assert invalid_mode.status_code == 422
        assert client.post("/api/v1/task-units/unit/repair-plan", json={}).status_code == 405
    finally:
        client.__exit__(None, None, None)


def test_repair_execute_endpoint_requires_csrf_idempotency_and_returns_redacted_receipt(
    tmp_path: Path,
) -> None:
    client, app, _settings = _authenticated_client(tmp_path)

    class _RepairActionStub:
        def __init__(self) -> None:
            self.calls: list[tuple[str, TaskActionActor, str | None]] = []

        async def execute(
            self,
            unit_id: str,
            *,
            actor: TaskActionActor,
            idempotency_key: str | None,
        ) -> TaskRepairActionResult:
            self.calls.append((unit_id, actor, idempotency_key))
            if idempotency_key is None:
                raise ApplicationError(
                    code="IDEMPOTENCY_KEY_REQUIRED",
                    status=428,
                    title="缺少幂等键",
                    detail="repair execute 请求必须携带 Idempotency-Key",
                )
            return TaskRepairActionResult(
                action="execute",
                task_id="task-safe",
                task_unit_id=unit_id,
                status=TaskStatus.CLIENT_VERIFYING,
                task_version=12,
                execution_plan_id="plan-safe",
                operation_replayed=False,
                receipt_id="receipt-safe",
                idempotency_replayed=False,
            )

    stub = _RepairActionStub()
    app.state.task_repair_action_service = stub
    path = "/api/v1/task-units/unit-safe/repair/actions"
    try:
        without_csrf = client.post(
            path,
            headers={"Idempotency-Key": "repair-safe-key"},
            json={"action": "execute"},
        )
        assert without_csrf.status_code == 403
        assert stub.calls == []

        missing_key = client.post(path, headers=_csrf(client), json={"action": "execute"})
        assert missing_key.status_code == 428
        assert missing_key.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"

        response = client.post(
            path,
            headers={**_csrf(client), "Idempotency-Key": "repair-safe-key"},
            json={"action": "execute"},
        )
        assert response.status_code == 200
        assert stub.calls[-1][0] == "unit-safe"
        assert stub.calls[-1][1].kind == "admin_session"
        assert stub.calls[-1][2] == "repair-safe-key"
        assert response.json() == {
            "action": "execute",
            "task_id": "task-safe",
            "task_unit_id": "unit-safe",
            "status": "CLIENT_VERIFYING",
            "task_version": 12,
            "execution_plan_id": "plan-safe",
            "operation_replayed": False,
            "idempotency_replayed": False,
            "receipt_id": "receipt-safe",
        }
        encoded = response.text
        for forbidden in (
            "torrent_hash",
            "ownership_tag",
            "journal_id",
            "repair_candidate_key",
            "repair_evidence_digest",
            "inode",
            "device",
        ):
            assert forbidden not in encoded
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


def _create_ready_qb_target(app: FastAPI, settings: AppSettings) -> str:
    secret_id = app.state.secret_store.put(
        kind="DOWNLOADER_CREDENTIAL",
        value=b'{"username":"admin","password":"synthetic-password","api_key":null}',
    )
    downloader_id = new_uuid()
    now = datetime.now(UTC)
    with app.state.runtime.session_factory() as session:
        session.add(
            Downloader(
                id=downloader_id,
                name=f"target-{downloader_id[:8]}",
                type="QBITTORRENT",
                base_url="http://qb.invalid:8080",
                secret_id=secret_id,
                monitor_rules={},
                path_mappings=[
                    {
                        "remote_prefix": "/downloads",
                        "container_prefix": str(settings.data_dir),
                    }
                ],
                capabilities={
                    "client": "qBittorrent",
                    "version": "v5.2.3",
                    "api_version": "2.15.1",
                    "supports_skip_checking": True,
                    "supports_force_recheck": True,
                    "supports_verify_progress": True,
                    "read_only_probe": True,
                },
                connection_status="OK",
                path_mapping_status="OK",
                enabled=True,
                version=1,
                last_test_at=now,
                last_path_diagnostic_at=now,
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()
    return downloader_id


def _create_ready_transmission_target(app: FastAPI, settings: AppSettings) -> str:
    downloader_id = new_uuid()
    now = datetime.now(UTC)
    with app.state.runtime.session_factory() as session:
        session.add(
            Downloader(
                id=downloader_id,
                name=f"target-tr-{downloader_id[:8]}",
                type="TRANSMISSION",
                base_url="http://tr.invalid:9091/transmission/rpc",
                secret_id=None,
                monitor_rules={},
                path_mappings=[
                    {
                        "remote_prefix": "/downloads",
                        "container_prefix": str(settings.data_dir),
                    }
                ],
                capabilities={
                    "client": "Transmission",
                    "version": "4.1.3",
                    "api_version": "6.0.0",
                    "supports_skip_checking": False,
                    "supports_force_recheck": True,
                    "supports_verify_progress": True,
                    "read_only_probe": True,
                },
                connection_status="OK",
                path_mapping_status="OK",
                enabled=True,
                version=1,
                last_test_at=now,
                last_path_diagnostic_at=now,
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()
    return downloader_id


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
