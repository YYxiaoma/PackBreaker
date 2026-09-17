from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import httpx2
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app.api.dependencies import CSRF_COOKIE
from backend.app.config import AppSettings
from backend.app.domain.task_definition import TaskExecutionTrigger
from backend.app.domain.task_state import TaskStatus
from backend.app.infrastructure.adapters.downloaders import DownloaderAdapterFactory
from backend.app.infrastructure.persistence.models import (
    Downloader,
    Site,
    TaskSchedule,
    UnpackTask,
    new_uuid,
)
from backend.app.main import create_app

_PASSWORD = "synthetic correct horse battery staple"


def test_monitor_task_definition_persists_v015_defaults_and_cron(tmp_path: Path) -> None:
    client, app = _authenticated_client(tmp_path)
    try:
        site_id = _create_ready_site(app)
        payload = _monitor_payload(site_id)

        without_csrf = client.post("/api/v1/task-definitions", json=payload)
        assert without_csrf.status_code == 403

        created = client.post(
            "/api/v1/task-definitions",
            headers=_csrf(client),
            json=payload,
        )
        assert created.status_code == 201
        body = created.json()
        assert body["name"] == "夜间目录监控"
        assert body["kind"] == "MONITOR"
        assert body["status"] == "ENABLED"
        assert body["site_id"] == site_id
        assert body["site_name"] == "M-Team 测试站"
        assert body["site_available"] is True
        assert body["source_kind"] == "DIRECTORY"
        assert body["source_directory"] == "incoming"
        assert body["cron_expression"] == "0 */2 * * *"
        assert body["timezone"] == "Asia/Shanghai"
        assert body["file_types"] == ["VIDEO"]
        assert body["video_extensions"] == [
            ".mkv",
            ".mp4",
            ".ts",
            ".m2ts",
            ".avi",
            ".mov",
            ".wmv",
        ]
        assert body["exclude_names"] == ["sample", "trailer"]
        assert body["ignore_temp_files"] is True
        assert body["include_subdirectories"] is True
        assert body["storage_mode"] == "HARDLINK"
        assert body["preserve_structure"] is True
        assert body["conflict_policy"] == "VERIFY_REUSE_OR_STOP"
        assert body["stability_wait_seconds"] == 60
        assert body["only_completed_downloads"] is True
        assert body["initial_scope"] == "NEW_ONLY"
        assert body["debounce_seconds"] == 30
        assert body["overlap_policy"] == "SKIP"
        assert body["max_auto_retries"] == 3
        assert body["retry_intervals_seconds"] == [60, 300, 900]
        assert body["latest_execution"] is None

        listed = client.get("/api/v1/task-definitions", params={"kind": "MONITOR"})
        assert listed.status_code == 200
        assert [item["id"] for item in listed.json()["items"]] == [body["id"]]
    finally:
        client.__exit__(None, None, None)


def test_task_definition_requires_user_name_and_valid_monitor_cron(tmp_path: Path) -> None:
    client, app = _authenticated_client(tmp_path)
    try:
        site_id = _create_ready_site(app)
        blank_name = _monitor_payload(site_id)
        blank_name["name"] = "   "
        rejected_name = client.post(
            "/api/v1/task-definitions",
            headers=_csrf(client),
            json=blank_name,
        )
        assert rejected_name.status_code == 422
        assert rejected_name.json()["code"] == "TASK_DEFINITION_INVALID"
        assert "不会自动生成默认名称" in rejected_name.json()["detail"]

        invalid_cron = _monitor_payload(site_id)
        invalid_cron["cron_expression"] = "61 * * * *"
        rejected_cron = client.post(
            "/api/v1/task-definitions",
            headers=_csrf(client),
            json=invalid_cron,
        )
        assert rejected_cron.status_code == 422
        assert rejected_cron.json()["code"] == "TASK_DEFINITION_INVALID"
        assert "Cron" in rejected_cron.json()["detail"]

        unsupported_storage = _monitor_payload(site_id)
        unsupported_storage["output_policy"] = {
            "output_directory": "output",
            "storage_mode": "COPY",
        }
        rejected_storage = client.post(
            "/api/v1/task-definitions",
            headers=_csrf(client),
            json=unsupported_storage,
        )
        assert rejected_storage.status_code == 422
        assert rejected_storage.json()["code"] == "TASK_DEFINITION_INVALID"
        assert "HARDLINK" in rejected_storage.json()["detail"]

        unsupported_conflict = _monitor_payload(site_id)
        unsupported_conflict["output_policy"] = {
            "output_directory": "output",
            "conflict_policy": "OVERWRITE",
        }
        rejected_conflict = client.post(
            "/api/v1/task-definitions",
            headers=_csrf(client),
            json=unsupported_conflict,
        )
        assert rejected_conflict.status_code == 422
        assert rejected_conflict.json()["code"] == "TASK_DEFINITION_INVALID"
        assert "安全冲突策略" in rejected_conflict.json()["detail"]

        unsupported_structure = _monitor_payload(site_id)
        unsupported_structure["output_policy"] = {
            "output_directory": "output",
            "preserve_structure": False,
        }
        rejected_structure = client.post(
            "/api/v1/task-definitions",
            headers=_csrf(client),
            json=unsupported_structure,
        )
        assert rejected_structure.status_code == 422
        assert rejected_structure.json()["code"] == "TASK_DEFINITION_INVALID"
        assert "保持 torrent 原目录结构" in rejected_structure.json()["detail"]

        unsupported_file_type = _monitor_payload(site_id)
        unsupported_file_type["filters"] = {"file_types": ["ARCHIVE"]}
        rejected_file_type = client.post(
            "/api/v1/task-definitions",
            headers=_csrf(client),
            json=unsupported_file_type,
        )
        assert rejected_file_type.status_code == 422
        assert rejected_file_type.json()["code"] == "TASK_DEFINITION_INVALID"
        assert "仅支持视频文件" in rejected_file_type.json()["detail"]
    finally:
        client.__exit__(None, None, None)


def test_task_definition_precheck_reports_statuses_without_persisting(tmp_path: Path) -> None:
    client, app = _authenticated_client(tmp_path)
    try:
        site_id = _create_ready_site(app)
        target_downloader_id = _create_ready_qb_downloader(app)
        incoming = app.state.settings.data_dir / "incoming"
        incoming.mkdir(parents=True)
        (incoming / "Movie.2026.1080p.mkv").write_bytes(b"precheck-video")
        preview = client.post(
            "/api/v1/task-definitions/directory-preview",
            headers=_csrf(client),
            json={"directory_path": "incoming"},
        )
        assert preview.status_code == 200

        payload = {
            "name": "保存前预检任务",
            "kind": "MANUAL",
            "site_id": site_id,
            "source": {
                "kind": "DIRECTORY",
                "directory_path": "incoming",
                "config": {
                    "selected_files": preview.json()["files"],
                    "target_downloader_id": target_downloader_id,
                },
            },
            "output_policy": {"output_directory": "output"},
        }
        before = client.get("/api/v1/task-definitions").json()["items"]
        precheck = client.post(
            "/api/v1/task-definitions/precheck",
            headers=_csrf(client),
            json=payload,
        )
        assert precheck.status_code == 200
        body = precheck.json()
        assert body["status"] in {"OK", "WARNING"}
        assert any(item["code"] == "SITE" and item["status"] == "OK" for item in body["items"])
        assert any(
            item["code"] == "DOWNLOADER" and item["status"] == "OK" for item in body["items"]
        )
        assert any(item["code"] == "HARDLINK_FILESYSTEM" for item in body["items"])
        assert client.get("/api/v1/task-definitions").json()["items"] == before

        blocked_payload = dict(payload)
        blocked_payload["output_policy"] = {
            "output_directory": "output",
            "storage_mode": "COPY",
        }
        blocked = client.post(
            "/api/v1/task-definitions/precheck",
            headers=_csrf(client),
            json=blocked_payload,
        )
        assert blocked.status_code == 200
        assert blocked.json()["status"] == "BLOCKED"
        assert any(
            item["code"] == "OUTPUT_POLICY" and item["status"] == "BLOCKED"
            for item in blocked.json()["items"]
        )
    finally:
        client.__exit__(None, None, None)


def test_task_definition_cron_preview_uses_runtime_timezone_and_returns_five_runs(
    tmp_path: Path,
) -> None:
    client, _app = _authenticated_client(tmp_path)
    try:
        preview = client.post(
            "/api/v1/task-definitions/cron-preview",
            json={"cron_expression": "0   */2   * * *"},
        )
        assert preview.status_code == 200
        body = preview.json()
        assert body["cron_expression"] == "0 */2 * * *"
        assert body["timezone"] == "Asia/Shanghai"
        assert "每 2 小时" in body["description"]
        assert len(body["next_runs"]) == 5
        assert body["next_runs"] == sorted(body["next_runs"])

        invalid = client.post(
            "/api/v1/task-definitions/cron-preview",
            json={"cron_expression": "61 * * * *"},
        )
        assert invalid.status_code == 422
        assert invalid.json()["code"] == "TASK_CRON_INVALID"
    finally:
        client.__exit__(None, None, None)


def test_disabled_site_is_retained_as_unavailable_task_definition(tmp_path: Path) -> None:
    client, app = _authenticated_client(tmp_path)
    try:
        site_id = _create_ready_site(app, enabled=False)
        created = client.post(
            "/api/v1/task-definitions",
            headers=_csrf(client),
            json=_monitor_payload(site_id),
        )
        assert created.status_code == 201
        assert created.json()["status"] == "SITE_UNAVAILABLE"
        assert created.json()["site_available"] is False
    finally:
        client.__exit__(None, None, None)


def test_monitor_task_can_pause_and_resume_without_losing_schedule_checkpoint(
    tmp_path: Path,
) -> None:
    client, app = _authenticated_client(tmp_path)
    try:
        site_id = _create_ready_site(app)
        (app.state.settings.data_dir / "incoming").mkdir(parents=True)
        created = client.post(
            "/api/v1/task-definitions",
            headers=_csrf(client),
            json=_monitor_payload(site_id),
        )
        assert created.status_code == 201
        definition_id = cast(str, created.json()["id"])

        paused = client.post(
            f"/api/v1/task-definitions/{definition_id}/actions",
            headers=_csrf(client),
            json={"action": "pause"},
        )
        assert paused.status_code == 200
        assert paused.json()["status"] == "PAUSED"
        assert paused.json()["version"] == 2
        with app.state.runtime.session_factory() as session:
            schedule = session.scalar(
                select(TaskSchedule).where(TaskSchedule.task_definition_id == definition_id)
            )
            assert schedule is not None
            assert schedule.enabled is False

        blocked_scan = client.post(
            f"/api/v1/task-definitions/{definition_id}/scan",
            headers=_csrf(client),
        )
        assert blocked_scan.status_code == 409
        assert blocked_scan.json()["code"] == "TASK_DEFINITION_NOT_RUNNABLE"
        assert (
            app.state.task_definition_execution_service.list_due_monitor_scans(
                now=datetime.now(UTC) + timedelta(days=1),
                limit=10,
            )
            == ()
        )

        resumed = client.post(
            f"/api/v1/task-definitions/{definition_id}/actions",
            headers=_csrf(client),
            json={"action": "resume"},
        )
        assert resumed.status_code == 200
        assert resumed.json()["status"] == "ENABLED"
        assert resumed.json()["version"] == 3
        assert datetime.fromisoformat(cast(str, resumed.json()["next_run_at"])) > datetime.now(UTC)
        with app.state.runtime.session_factory() as session:
            schedule = session.scalar(
                select(TaskSchedule).where(TaskSchedule.task_definition_id == definition_id)
            )
            assert schedule is not None
            assert schedule.enabled is True
    finally:
        client.__exit__(None, None, None)


def test_monitor_immediate_scan_establishes_watermark_and_materializes_only_new_files(
    tmp_path: Path,
) -> None:
    client, app = _authenticated_client(tmp_path)
    try:
        site_id = _create_ready_site(app)
        incoming = app.state.settings.data_dir / "incoming"
        incoming.mkdir(parents=True)
        existing_movie = incoming / "Existing.Movie.2024.1080p.mkv"
        existing_movie.write_bytes(b"existing-video")
        payload = _monitor_payload(site_id)
        payload["execution_policy"] = {
            "stability_detection_enabled": False,
            "debounce_seconds": 0,
        }

        created = client.post(
            "/api/v1/task-definitions",
            headers=_csrf(client),
            json=payload,
        )
        assert created.status_code == 201
        definition = created.json()
        definition_id = cast(str, definition["id"])
        original_next_run = definition["next_run_at"]
        assert original_next_run is not None
        assert definition["last_scan_at"] is None
        assert definition["last_successful_scan_at"] is None

        baseline = client.post(
            f"/api/v1/task-definitions/{definition_id}/scan",
            headers=_csrf(client),
        )
        assert baseline.status_code == 200
        assert baseline.json()["outcome"] == "BASELINE_ESTABLISHED"
        assert baseline.json()["discovered_count"] == 1
        assert baseline.json()["new_count"] == 0
        assert baseline.json()["execution"] is None
        assert baseline.json()["next_run_at"] == original_next_run

        (incoming / "New.Movie.2025.2160p.mkv").write_bytes(b"new-video")
        materialized = client.post(
            f"/api/v1/task-definitions/{definition_id}/scan",
            headers=_csrf(client),
        )
        assert materialized.status_code == 200
        body = materialized.json()
        assert body["outcome"] == "MATERIALIZED"
        assert body["discovered_count"] == 2
        assert body["new_count"] == 1
        assert body["execution"]["status"] == "PENDING"
        assert body["execution"]["trigger"] == "IMMEDIATE_SCAN"
        assert [item["name"] for item in body["execution"]["items"]] == ["New.Movie.2025.2160p.mkv"]

        repeated = client.post(
            f"/api/v1/task-definitions/{definition_id}/scan",
            headers=_csrf(client),
        )
        assert repeated.status_code == 200
        assert repeated.json()["outcome"] == "NO_CHANGES"
        assert repeated.json()["new_count"] == 0
        assert repeated.json()["execution"] is None

        before = existing_movie.stat()
        existing_movie.write_bytes(b"replaced-video")
        os.utime(
            existing_movie,
            ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000),
        )
        replaced = client.post(
            f"/api/v1/task-definitions/{definition_id}/scan",
            headers=_csrf(client),
        )
        assert replaced.status_code == 200
        assert replaced.json()["outcome"] == "MATERIALIZED"
        assert replaced.json()["new_count"] == 1
        assert [item["name"] for item in replaced.json()["execution"]["items"]] == [
            "Existing.Movie.2024.1080p.mkv"
        ]

        current = client.get(f"/api/v1/task-definitions/{definition_id}")
        assert current.status_code == 200
        assert current.json()["last_scan_at"] is not None
        assert current.json()["last_successful_scan_at"] is not None
        assert current.json()["next_run_at"] == original_next_run
        with app.state.runtime.session_factory() as session:
            schedule = session.scalar(
                select(TaskSchedule).where(TaskSchedule.task_definition_id == definition_id)
            )
            assert schedule is not None
            assert schedule.scan_checkpoint["watermark_initialized"] is True
            assert len(schedule.scan_checkpoint["seen_object_keys"]) == 3
    finally:
        client.__exit__(None, None, None)


def test_directory_monitor_resumes_large_scan_from_persisted_cursor(tmp_path: Path) -> None:
    client, app = _authenticated_client(tmp_path)
    try:
        site_id = _create_ready_site(app)
        incoming = app.state.settings.data_dir / "incoming"
        incoming.mkdir(parents=True)
        for name in (
            "A.Movie.2020.1080p.mkv",
            "B.Movie.2021.1080p.mkv",
            "C.Movie.2022.1080p.mkv",
            "D.Movie.2023.1080p.mkv",
            "E.Movie.2024.1080p.mkv",
        ):
            (incoming / name).write_bytes(name.encode())

        payload = _monitor_payload(site_id)
        payload["execution_policy"] = {
            "stability_detection_enabled": False,
            "debounce_seconds": 0,
            "initial_scope": "NEW_ONLY",
        }
        created = client.post(
            "/api/v1/task-definitions",
            headers=_csrf(client),
            json=payload,
        )
        assert created.status_code == 201
        definition_id = cast(str, created.json()["id"])
        service = app.state.task_definition_execution_service
        service._directory_scan_batch_size = 2  # noqa: SLF001 - force multiple bounded pages

        first = client.post(
            f"/api/v1/task-definitions/{definition_id}/scan",
            headers=_csrf(client),
        )
        assert first.status_code == 200
        assert first.json()["outcome"] == "BASELINE_CONTINUING"
        assert first.json()["discovered_count"] == 2
        with app.state.runtime.session_factory() as session:
            schedule = session.scalar(
                select(TaskSchedule).where(TaskSchedule.task_definition_id == definition_id)
            )
            assert schedule is not None
            checkpoint = schedule.scan_checkpoint
            assert checkpoint["watermark_initialized"] is False
            assert checkpoint["directory_scan_continuation"] is True
            assert checkpoint["directory_scan"]["cursor"] == "B.Movie.2021.1080p.mkv"
            assert checkpoint["directory_scan"]["discovered_count"] == 2

        due = service.list_due_monitor_scans(now=datetime.now(UTC), limit=10)
        assert [(item.task_definition_id, item.trigger) for item in due] == [
            (definition_id, TaskExecutionTrigger.IMMEDIATE_SCAN)
        ]

        second = client.post(
            f"/api/v1/task-definitions/{definition_id}/scan",
            headers=_csrf(client),
        )
        assert second.status_code == 200
        assert second.json()["outcome"] == "BASELINE_CONTINUING"
        assert second.json()["discovered_count"] == 4

        third = client.post(
            f"/api/v1/task-definitions/{definition_id}/scan",
            headers=_csrf(client),
        )
        assert third.status_code == 200
        assert third.json()["outcome"] == "BASELINE_ESTABLISHED"
        assert third.json()["discovered_count"] == 5
        with app.state.runtime.session_factory() as session:
            schedule = session.scalar(
                select(TaskSchedule).where(TaskSchedule.task_definition_id == definition_id)
            )
            assert schedule is not None
            checkpoint = schedule.scan_checkpoint
            assert checkpoint["watermark_initialized"] is True
            assert checkpoint["directory_scan_continuation"] is False
            assert checkpoint["directory_scan"]["cursor"] is None
            assert checkpoint["directory_scan"]["generation"] == 1
            assert checkpoint["directory_scan"]["discovered_count"] == 0
            assert len(checkpoint["seen_object_keys"]) == 5

        new_movie = incoming / "Z.Movie.2026.2160p.mkv"
        new_movie.write_bytes(b"new-video")
        sweep_one = client.post(
            f"/api/v1/task-definitions/{definition_id}/scan",
            headers=_csrf(client),
        )
        assert sweep_one.status_code == 200
        assert sweep_one.json()["outcome"] == "SCAN_CONTINUING"
        sweep_two = client.post(
            f"/api/v1/task-definitions/{definition_id}/scan",
            headers=_csrf(client),
        )
        assert sweep_two.status_code == 200
        assert sweep_two.json()["outcome"] == "SCAN_CONTINUING"
        sweep_three = client.post(
            f"/api/v1/task-definitions/{definition_id}/scan",
            headers=_csrf(client),
        )
        assert sweep_three.status_code == 200
        assert sweep_three.json()["outcome"] == "MATERIALIZED"
        assert sweep_three.json()["discovered_count"] == 6
        assert sweep_three.json()["new_count"] == 1
        assert [item["name"] for item in sweep_three.json()["execution"]["items"]] == [
            "Z.Movie.2026.2160p.mkv"
        ]
        with app.state.runtime.session_factory() as session:
            schedule = session.scalar(
                select(TaskSchedule).where(TaskSchedule.task_definition_id == definition_id)
            )
            assert schedule is not None
            assert schedule.scan_checkpoint["directory_scan"]["cursor"] is None
            assert schedule.scan_checkpoint["directory_scan"]["generation"] == 2
            assert schedule.scan_checkpoint["directory_scan_continuation"] is False
    finally:
        client.__exit__(None, None, None)


def test_directory_monitor_does_not_advance_cursor_while_page_waits_for_stability(
    tmp_path: Path,
) -> None:
    client, app = _authenticated_client(tmp_path)
    try:
        site_id = _create_ready_site(app)
        incoming = app.state.settings.data_dir / "incoming"
        incoming.mkdir(parents=True)
        first_movie = incoming / "A.Movie.2026.1080p.mkv"
        second_movie = incoming / "B.Movie.2026.1080p.mkv"
        first_movie.write_bytes(b"first")
        second_movie.write_bytes(b"second")
        observed = first_movie.stat()
        modified_at = datetime.fromtimestamp(observed.st_mtime_ns / 1_000_000_000, tz=UTC)

        payload = _monitor_payload(site_id)
        payload["execution_policy"] = {
            "stability_detection_enabled": True,
            "stability_wait_seconds": 60,
            "debounce_seconds": 0,
            "initial_scope": "INCLUDE_EXISTING",
        }
        created = client.post(
            "/api/v1/task-definitions",
            headers=_csrf(client),
            json=payload,
        )
        assert created.status_code == 201
        definition_id = cast(str, created.json()["id"])
        service = app.state.task_definition_execution_service
        service._directory_scan_batch_size = 1  # noqa: SLF001 - exercise page retry semantics

        waiting = asyncio.run(
            service.scan_monitor(
                definition_id,
                trigger=TaskExecutionTrigger.IMMEDIATE_SCAN,
                trace_id="paged-stability-wait",
                now=modified_at + timedelta(seconds=1),
            )
        )
        assert waiting.outcome == "STABILITY_WAIT"
        with app.state.runtime.session_factory() as session:
            schedule = session.scalar(
                select(TaskSchedule).where(TaskSchedule.task_definition_id == definition_id)
            )
            assert schedule is not None
            checkpoint = schedule.scan_checkpoint
            assert checkpoint.get("directory_scan_continuation") is False
            assert checkpoint.get("directory_scan", {}).get("cursor") is None

        ready = asyncio.run(
            service.scan_monitor(
                definition_id,
                trigger=TaskExecutionTrigger.IMMEDIATE_SCAN,
                trace_id="paged-stability-ready",
                now=modified_at + timedelta(seconds=61),
            )
        )
        assert ready.outcome == "MATERIALIZED"
        assert ready.execution is not None
        assert [item.name for item in ready.execution.items] == ["A.Movie.2026.1080p.mkv"]
        with app.state.runtime.session_factory() as session:
            schedule = session.scalar(
                select(TaskSchedule).where(TaskSchedule.task_definition_id == definition_id)
            )
            assert schedule is not None
            checkpoint = schedule.scan_checkpoint
            assert checkpoint["directory_scan_continuation"] is True
            assert checkpoint["directory_scan"]["cursor"] == "A.Movie.2026.1080p.mkv"

        next_page = asyncio.run(
            service.scan_monitor(
                definition_id,
                trigger=TaskExecutionTrigger.IMMEDIATE_SCAN,
                trace_id="paged-stability-next",
                now=modified_at + timedelta(seconds=61),
            )
        )
        assert next_page.outcome == "STABILITY_WAIT"
        with app.state.runtime.session_factory() as session:
            schedule = session.scalar(
                select(TaskSchedule).where(TaskSchedule.task_definition_id == definition_id)
            )
            assert schedule is not None
            checkpoint = schedule.scan_checkpoint
            assert checkpoint["directory_scan_continuation"] is False
            assert checkpoint["directory_scan"]["cursor"] == "A.Movie.2026.1080p.mkv"
    finally:
        client.__exit__(None, None, None)


def test_run_once_after_queue_survives_finishing_scan_and_is_consumed_by_next_scan(
    tmp_path: Path,
) -> None:
    client, app = _authenticated_client(tmp_path)
    try:
        site_id = _create_ready_site(app)
        (app.state.settings.data_dir / "incoming").mkdir(parents=True)
        payload = _monitor_payload(site_id)
        payload["execution_policy"] = {
            "stability_detection_enabled": False,
            "overlap_policy": "RUN_ONCE_AFTER",
        }
        created = client.post(
            "/api/v1/task-definitions",
            headers=_csrf(client),
            json=payload,
        )
        assert created.status_code == 201
        definition_id = cast(str, created.json()["id"])
        service = app.state.task_definition_execution_service

        stale_checkpoint = {
            "schema_version": "packbreaker-monitor-watermark-v1",
            "watermark_initialized": True,
            "seen_object_keys": [],
            "run_once_after_pending": False,
        }
        service._set_run_once_after_pending(definition_id, True)  # noqa: SLF001
        service._update_monitor_schedule(  # noqa: SLF001
            definition_id,
            trigger=TaskExecutionTrigger.IMMEDIATE_SCAN,
            now=datetime.now(UTC),
            checkpoint=stale_checkpoint,
            successful=True,
        )

        due = service.list_due_monitor_scans(now=datetime.now(UTC), limit=10)
        assert [(item.task_definition_id, item.trigger) for item in due] == [
            (definition_id, TaskExecutionTrigger.IMMEDIATE_SCAN)
        ]
        with app.state.runtime.session_factory() as session:
            schedule = session.scalar(
                select(TaskSchedule).where(TaskSchedule.task_definition_id == definition_id)
            )
            assert schedule is not None
            assert schedule.scan_checkpoint["run_once_after_pending"] is True

        scanned = client.post(
            f"/api/v1/task-definitions/{definition_id}/scan",
            headers=_csrf(client),
        )
        assert scanned.status_code == 200
        with app.state.runtime.session_factory() as session:
            schedule = session.scalar(
                select(TaskSchedule).where(TaskSchedule.task_definition_id == definition_id)
            )
            assert schedule is not None
            assert schedule.scan_checkpoint["run_once_after_pending"] is False
    finally:
        client.__exit__(None, None, None)


def test_directory_stability_wait_schedules_follow_up_before_next_cron(tmp_path: Path) -> None:
    client, app = _authenticated_client(tmp_path)
    try:
        site_id = _create_ready_site(app)
        incoming = app.state.settings.data_dir / "incoming"
        incoming.mkdir(parents=True)
        (incoming / "Stable.Movie.2026.1080p.mkv").write_bytes(b"stable-video")
        payload = _monitor_payload(site_id)
        payload["execution_policy"] = {
            "stability_detection_enabled": True,
            "stability_wait_seconds": 60,
            "initial_scope": "INCLUDE_EXISTING",
            "debounce_seconds": 0,
        }
        created = client.post(
            "/api/v1/task-definitions",
            headers=_csrf(client),
            json=payload,
        )
        assert created.status_code == 201
        definition_id = cast(str, created.json()["id"])
        cron_next = datetime.fromisoformat(cast(str, created.json()["next_run_at"]))

        waiting = client.post(
            f"/api/v1/task-definitions/{definition_id}/scan",
            headers=_csrf(client),
        )
        assert waiting.status_code == 200
        assert waiting.json()["outcome"] == "STABILITY_WAIT"
        assert waiting.json()["execution"] is None

        with app.state.runtime.session_factory() as session:
            schedule = session.scalar(
                select(TaskSchedule).where(TaskSchedule.task_definition_id == definition_id)
            )
            assert schedule is not None
            stability_next = datetime.fromisoformat(
                cast(str, schedule.scan_checkpoint["stability_next_check_at"])
            )
        assert stability_next < cron_next

        service = app.state.task_definition_execution_service
        due = service.list_due_monitor_scans(
            now=stability_next + timedelta(seconds=1),
            limit=10,
        )
        assert [(item.task_definition_id, item.trigger) for item in due] == [
            (definition_id, TaskExecutionTrigger.IMMEDIATE_SCAN)
        ]

        materialized = asyncio.run(
            service.scan_monitor(
                definition_id,
                trigger=TaskExecutionTrigger.IMMEDIATE_SCAN,
                trace_id="synthetic-stability-follow-up",
                now=stability_next + timedelta(seconds=1),
            )
        )
        assert materialized.outcome == "MATERIALIZED"
        assert materialized.execution is not None
        assert [item.name for item in materialized.execution.items] == [
            "Stable.Movie.2026.1080p.mkv"
        ]
    finally:
        client.__exit__(None, None, None)


def test_directory_browser_preview_and_manual_snapshot_execution_are_safe(tmp_path: Path) -> None:
    client, app = _authenticated_client(tmp_path)
    try:
        site_id = _create_ready_site(app)
        target_downloader_id = _create_ready_qb_downloader(app)
        incoming = app.state.settings.data_dir / "incoming"
        subdir = incoming / "nested"
        subdir.mkdir(parents=True)
        movie = incoming / "Movie.2026.1080p.mkv"
        movie.write_bytes(b"directory-video")
        (incoming / "sample.mkv").write_bytes(b"sample")

        root = client.get("/api/v1/task-definitions/source-directories", params={"path": "."})
        assert root.status_code == 200
        assert any(item["path"] == "incoming" for item in root.json()["entries"])
        nested = client.get(
            "/api/v1/task-definitions/source-directories", params={"path": "incoming"}
        )
        assert nested.status_code == 200
        assert nested.json()["entries"] == [{"name": "nested", "path": "incoming/nested"}]
        escaped = client.get(
            "/api/v1/task-definitions/source-directories", params={"path": "../etc"}
        )
        assert escaped.status_code == 422
        assert escaped.json()["code"] == "TASK_DEFINITION_INVALID"

        preview = client.post(
            "/api/v1/task-definitions/directory-preview",
            headers=_csrf(client),
            json={"directory_path": "incoming"},
        )
        assert preview.status_code == 200
        preview_body = preview.json()
        assert preview_body["directory_path"] == "incoming"
        assert preview_body["matched_count"] == 1
        assert [item["relative_path"] for item in preview_body["files"]] == ["Movie.2026.1080p.mkv"]
        selected = preview_body["files"]

        created = client.post(
            "/api/v1/task-definitions",
            headers=_csrf(client),
            json={
                "name": "手动目录快照任务",
                "kind": "MANUAL",
                "site_id": site_id,
                "source": {
                    "kind": "DIRECTORY",
                    "directory_path": "incoming",
                    "config": {
                        "selected_files": selected,
                        "target_downloader_id": target_downloader_id,
                    },
                },
                "output_policy": {"output_directory": "output"},
            },
        )
        assert created.status_code == 201
        definition_id = cast(str, created.json()["id"])
        executed = client.post(
            f"/api/v1/task-definitions/{definition_id}/executions",
            headers=_csrf(client),
        )
        assert executed.status_code == 201
        execution_body = executed.json()
        assert execution_body["status"] == "PENDING"
        assert len(execution_body["items"]) == 1
        item_id = cast(str, execution_body["items"][0]["id"])
        unpack_task_id = cast(str, execution_body["items"][0]["unpack_task_id"])
        plan_before_review = client.post(
            f"/api/v1/task-definitions/{definition_id}/executions/{execution_body['id']}/items/{item_id}/execution-plan",
            headers=_csrf(client),
        )
        assert plan_before_review.status_code == 404, plan_before_review.text
        assert plan_before_review.json()["code"] == "EXECUTION_GATE_NOT_FOUND"
        with app.state.runtime.session_factory() as session:
            task = session.get(UnpackTask, unpack_task_id)
            assert task is not None
            assert task.source_downloader_id != definition_id
            assert task.checkpoint["source_kind"] == "DIRECTORY"
            assert task.checkpoint["source_directory"] == "incoming"
            assert task.checkpoint["source_identity"] == task.source_downloader_id
            task.status = TaskStatus.DONE.value
            session.commit()

        reconciled = client.get(
            f"/api/v1/task-definitions/{definition_id}/executions/{execution_body['id']}"
        )
        assert reconciled.status_code == 200
        reconciled_body = reconciled.json()
        assert reconciled_body["status"] == "COMPLETED"
        assert reconciled_body["success_count"] == 1
        assert reconciled_body["items"][0]["phase"] == "COMPLETED"
        assert reconciled_body["items"][0]["result"] == "SUCCESS"
        final_event = reconciled_body["events"][-1]
        assert final_event["event_code"] == "TASK_EXECUTION_ITEM_STATE_CHANGED"
        assert final_event["context"]["to_phase"] == "COMPLETED"
        assert final_event["context"]["to_result"] == "SUCCESS"

        changed_preview = client.post(
            "/api/v1/task-definitions/directory-preview",
            headers=_csrf(client),
            json={"directory_path": "incoming"},
        )
        assert changed_preview.status_code == 200
        changed_selected = changed_preview.json()["files"]
        changed_definition = client.post(
            "/api/v1/task-definitions",
            headers=_csrf(client),
            json={
                "name": "手动目录变化阻断任务",
                "kind": "MANUAL",
                "site_id": site_id,
                "source": {
                    "kind": "DIRECTORY",
                    "directory_path": "incoming",
                    "config": {
                        "selected_files": changed_selected,
                        "target_downloader_id": target_downloader_id,
                    },
                },
                "output_policy": {"output_directory": "output"},
            },
        )
        assert changed_definition.status_code == 201
        movie.write_bytes(b"directory-video-changed")
        changed_execution = client.post(
            f"/api/v1/task-definitions/{changed_definition.json()['id']}/executions",
            headers=_csrf(client),
        )
        assert changed_execution.status_code == 201
        changed_body = changed_execution.json()
        assert changed_body["status"] == "FAILED"
        assert changed_body["items"][0]["unpack_task_id"] is None
        assert changed_body["items"][0]["error_code"] == "SOURCE_SNAPSHOT_CHANGED"

        copy_definition = client.post(
            "/api/v1/task-definitions",
            headers=_csrf(client),
            json={
                "name": "目录 COPY 安全阻断任务",
                "kind": "MANUAL",
                "site_id": site_id,
                "source": {
                    "kind": "DIRECTORY",
                    "directory_path": "incoming",
                    "config": {
                        "selected_files": changed_selected,
                        "target_downloader_id": target_downloader_id,
                    },
                },
                "output_policy": {"output_directory": "output", "storage_mode": "COPY"},
            },
        )
        assert copy_definition.status_code == 422
        assert copy_definition.json()["code"] == "TASK_DEFINITION_INVALID"
        assert "HARDLINK" in copy_definition.json()["detail"]
    finally:
        client.__exit__(None, None, None)


def test_directory_monitor_debounce_waits_for_mtime_quiet_window(tmp_path: Path) -> None:
    client, app = _authenticated_client(tmp_path)
    try:
        site_id = _create_ready_site(app)
        incoming = app.state.settings.data_dir / "incoming"
        incoming.mkdir(parents=True)
        movie = incoming / "Quiet.Movie.2026.1080p.mkv"
        movie.write_bytes(b"quiet-video")
        file_stat = movie.stat()
        modified_at = datetime.fromtimestamp(file_stat.st_mtime_ns / 1_000_000_000, tz=UTC)
        payload = _monitor_payload(site_id)
        payload["execution_policy"] = {
            "stability_detection_enabled": False,
            "initial_scope": "INCLUDE_EXISTING",
            "debounce_seconds": 30,
        }
        created = client.post(
            "/api/v1/task-definitions",
            headers=_csrf(client),
            json=payload,
        )
        assert created.status_code == 201
        definition_id = cast(str, created.json()["id"])
        service = app.state.task_definition_execution_service

        waiting = asyncio.run(
            service.scan_monitor(
                definition_id,
                trigger=TaskExecutionTrigger.IMMEDIATE_SCAN,
                trace_id="synthetic-debounce-wait",
                now=modified_at + timedelta(seconds=5),
            )
        )
        assert waiting.outcome == "DEBOUNCE_WAIT"
        assert waiting.execution is None
        with app.state.runtime.session_factory() as session:
            schedule = session.scalar(
                select(TaskSchedule).where(TaskSchedule.task_definition_id == definition_id)
            )
            assert schedule is not None
            debounce_next = datetime.fromisoformat(
                cast(str, schedule.scan_checkpoint["debounce_next_check_at"])
            )
        assert debounce_next == modified_at + timedelta(seconds=30)
        due = service.list_due_monitor_scans(now=debounce_next + timedelta(seconds=1), limit=10)
        assert [(item.task_definition_id, item.trigger) for item in due] == [
            (definition_id, TaskExecutionTrigger.IMMEDIATE_SCAN)
        ]
        materialized = asyncio.run(
            service.scan_monitor(
                definition_id,
                trigger=TaskExecutionTrigger.IMMEDIATE_SCAN,
                trace_id="synthetic-debounce-ready",
                now=debounce_next + timedelta(seconds=1),
            )
        )
        assert materialized.outcome == "MATERIALIZED"
        assert materialized.execution is not None
        assert [item.name for item in materialized.execution.items] == [
            "Quiet.Movie.2026.1080p.mkv"
        ]
    finally:
        client.__exit__(None, None, None)


def test_manual_downloader_task_materializes_selected_torrent_into_safe_pending_run(
    tmp_path: Path,
) -> None:
    client, app = _authenticated_client(tmp_path)
    torrent_hash = "a" * 40
    try:
        site_id = _create_ready_site(app)
        downloader_id = _create_ready_qb_downloader(app)
        movie_root = app.state.settings.data_dir / "source" / "Movie.Pack"
        movie_root.mkdir(parents=True)
        (movie_root / "Movie.2024.1080p.mkv").write_bytes(b"synthetic-video")
        (movie_root / "sample.mkv").write_bytes(b"sample")

        def handler(request: httpx2.Request) -> httpx2.Response:
            if request.url.path.endswith("/auth/login"):
                return httpx2.Response(200, text="Ok.", headers={"Set-Cookie": "SID=fake; path=/"})
            if request.url.path.endswith("/torrents/info"):
                return httpx2.Response(
                    200,
                    json=[
                        {
                            "hash": torrent_hash,
                            "name": "Movie.Pack",
                            "state": "uploading",
                            "progress": 1.0,
                            "size": 15,
                            "category": "movie",
                            "tags": "pack,manual",
                            "tracker": "https://tracker.invalid/announce",
                            "save_path": "/downloads",
                            "content_path": "/downloads/Movie.Pack",
                        }
                    ],
                )
            return httpx2.Response(404)

        app.state.downloader_service._adapter_factory = DownloaderAdapterFactory(  # noqa: SLF001
            transport=httpx2.MockTransport(handler)
        )

        precheck = client.post(
            "/api/v1/task-definitions/precheck",
            headers=_csrf(client),
            json={
                "name": "下载器来源预检任务",
                "kind": "MANUAL",
                "site_id": site_id,
                "source": {
                    "kind": "DOWNLOADER",
                    "downloader_id": downloader_id,
                    "config": {
                        "selected_torrent_hashes": [torrent_hash],
                        "selected_torrents": [
                            {
                                "torrent_hash": torrent_hash,
                                "name": "Movie.Pack",
                                "save_path": "/downloads",
                                "content_path": "/downloads/Movie.Pack",
                            }
                        ],
                    },
                },
                "output_policy": {"output_directory": "output"},
            },
        )
        assert precheck.status_code == 200
        assert any(
            item["code"] == "HARDLINK_FILESYSTEM" and item["status"] == "OK"
            for item in precheck.json()["items"]
        )

        missing_selection = client.post(
            "/api/v1/task-definitions",
            headers=_csrf(client),
            json={
                "name": "无种子手动任务",
                "kind": "MANUAL",
                "site_id": site_id,
                "source": {"kind": "DOWNLOADER", "downloader_id": downloader_id},
                "output_policy": {"output_directory": "output"},
            },
        )
        assert missing_selection.status_code == 422
        assert missing_selection.json()["code"] == "TASK_DEFINITION_INVALID"

        created = client.post(
            "/api/v1/task-definitions",
            headers=_csrf(client),
            json={
                "name": "手动真实种子拆包",
                "kind": "MANUAL",
                "site_id": site_id,
                "source": {
                    "kind": "DOWNLOADER",
                    "downloader_id": downloader_id,
                    "config": {
                        "selected_torrent_hashes": [torrent_hash],
                        "selected_torrents": [{"torrent_hash": torrent_hash, "name": "Movie.Pack"}],
                    },
                },
                "output_policy": {"output_directory": "output"},
            },
        )
        assert created.status_code == 201
        definition_id = cast(str, created.json()["id"])

        execution = client.post(
            f"/api/v1/task-definitions/{definition_id}/executions",
            headers=_csrf(client),
        )
        assert execution.status_code == 201
        body = execution.json()
        assert body["status"] == "PENDING"
        assert body["phase"] == "WAITING"
        assert body["discovered_count"] == 1
        assert body["success_count"] == 0
        assert body["failed_count"] == 0
        assert body["skipped_count"] == 0
        assert len(body["items"]) == 1
        assert body["items"][0]["name"] == "Movie.2024.1080p.mkv"
        assert body["items"][0]["unpack_task_id"] is not None
        execution_id = cast(str, body["id"])
        original_task_id = cast(str, body["items"][0]["unpack_task_id"])

        history = client.get(
            f"/api/v1/task-definitions/{definition_id}/executions",
            params={
                "page": 1,
                "page_size": 10,
                "status": "PENDING",
                "trigger": "MANUAL",
                "search": "Movie.2024",
            },
        )
        assert history.status_code == 200
        history_body = history.json()
        assert history_body["total"] == 1
        assert history_body["page"] == 1
        assert history_body["page_size"] == 10
        assert history_body["items"][0]["id"] == execution_id
        assert history_body["items"][0]["discovered_count"] == 1
        assert history_body["items"][0]["source_execution_id"] is None

        detail = client.get(f"/api/v1/task-definitions/{definition_id}/executions/{execution_id}")
        assert detail.status_code == 200
        detail_body = detail.json()
        assert detail_body["config_snapshot"]["site_id"] == site_id
        assert detail_body["config_snapshot"]["source"]["downloader_id"] == downloader_id
        assert detail_body["items"][0]["progress"] is None
        assert detail_body["items"][0]["technical_detail"] is None
        assert [event["event_code"] for event in detail_body["events"]] == [
            "TASK_EXECUTION_STARTED",
            "TASK_UNPACK_RUN_MATERIALIZED",
            "TASK_EXECUTION_MATERIALIZED",
        ]
        assert all(event["trace_id"] == body["trace_id"] for event in detail_body["events"])

        with app.state.runtime.session_factory() as session:
            task = session.scalar(select(UnpackTask).where(UnpackTask.id == original_task_id))
            assert task is not None
            assert task.status == "PENDING"
            assert task.source_downloader_id == downloader_id
            assert task.source_hash == torrent_hash
            assert task.checkpoint["task_definition_id"] == definition_id
            assert task.checkpoint["site_id"] == site_id
            assert task.checkpoint["source_root"] == "source/Movie.Pack"

            task.status = "FAILED"
            task.error_code = "SYNTHETIC_UNPACK_FAILURE"
            task.version += 1
            session.commit()

        reconciled = client.get(
            f"/api/v1/task-definitions/{definition_id}/executions/{execution_id}"
        )
        assert reconciled.status_code == 200
        assert reconciled.json()["status"] == "FAILED"
        assert reconciled.json()["failed_count"] == 1
        assert reconciled.json()["items"][0]["retryable"] is True
        assert reconciled.json()["items"][0]["error_code"] == "SYNTHETIC_UNPACK_FAILURE"

        due_retries = app.state.task_definition_execution_service.list_due_auto_retries(
            now=datetime.now(UTC) + timedelta(seconds=61),
            limit=10,
        )
        assert len(due_retries) == 1
        assert due_retries[0].execution_id == execution_id
        assert due_retries[0].attempt == 1

        listed = client.get("/api/v1/task-definitions", params={"kind": "MANUAL"})
        assert listed.status_code == 200
        assert listed.json()["items"][0]["latest_execution"]["failed_count"] == 1

        retry_headers = {**_csrf(client), "Idempotency-Key": "retry-failed-synthetic-001"}
        retried = client.post(
            f"/api/v1/task-definitions/{definition_id}/executions/{execution_id}/retry-failed",
            headers=retry_headers,
        )
        assert retried.status_code == 201
        retry_body = retried.json()
        assert retry_body["trigger"] == "FAILED_RETRY"
        assert retry_body["source_execution_id"] == execution_id
        assert retry_body["status"] == "PENDING"
        assert retry_body["phase"] == "WAITING"
        assert retry_body["discovered_count"] == 1
        assert len(retry_body["items"]) == 1
        assert retry_body["items"][0]["retry_count"] == 1
        rerun_task_id = cast(str, retry_body["items"][0]["unpack_task_id"])
        assert rerun_task_id != original_task_id

        replayed = client.post(
            f"/api/v1/task-definitions/{definition_id}/executions/{execution_id}/retry-failed",
            headers=retry_headers,
        )
        assert replayed.status_code == 201
        assert replayed.json()["id"] == retry_body["id"]
        assert replayed.json()["items"][0]["unpack_task_id"] == rerun_task_id

        failed_history = client.get(
            f"/api/v1/task-definitions/{definition_id}/executions",
            params={"status": "FAILED", "page_size": 10},
        )
        assert failed_history.status_code == 200
        assert failed_history.json()["total"] == 1
        assert failed_history.json()["items"][0]["id"] == execution_id

        with app.state.runtime.session_factory() as session:
            rerun = session.get(UnpackTask, rerun_task_id)
            assert rerun is not None
            assert rerun.status == "PENDING"
            assert rerun.parent_task_id == original_task_id
            assert rerun.run_number == 2
            assert rerun.checkpoint["task_definition_id"] == definition_id
            assert rerun.checkpoint["site_id"] == site_id
            assert rerun.checkpoint["source_root"] == "source/Movie.Pack"
            assert rerun.checkpoint["task_execution_id"] == retry_body["id"]
            logical_runs = tuple(
                session.scalars(
                    select(UnpackTask)
                    .where(
                        UnpackTask.source_downloader_id == downloader_id,
                        UnpackTask.source_hash == torrent_hash,
                    )
                    .order_by(UnpackTask.run_number)
                )
            )
            assert [item.run_number for item in logical_runs] == [1, 2]
    finally:
        client.__exit__(None, None, None)


def _monitor_payload(site_id: str) -> dict[str, object]:
    return {
        "name": "夜间目录监控",
        "kind": "MONITOR",
        "site_id": site_id,
        "source": {"kind": "DIRECTORY", "directory_path": "incoming"},
        "cron_expression": "0 */2 * * *",
        "output_policy": {"output_directory": "output"},
    }


def _authenticated_client(tmp_path: Path) -> tuple[TestClient, FastAPI]:
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
    return client, app


def _csrf(client: TestClient) -> dict[str, str]:
    token = client.cookies.get(CSRF_COOKIE)
    assert token is not None
    return {"X-CSRF-Token": token}


def _create_ready_site(app: FastAPI, *, enabled: bool = True) -> str:
    site_id = new_uuid()
    now = datetime.now(UTC)
    with app.state.runtime.session_factory() as session:
        session.add(
            Site(
                id=site_id,
                name="M-Team 测试站",
                type="MTEAM",
                base_url="https://api.m-team.cc",
                credential_kind="API_KEY",
                secret_id=None,
                capabilities={},
                connection_status="OK",
                enabled=enabled,
                version=1,
                last_test_at=now,
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()
    return site_id


def _create_ready_qb_downloader(app: FastAPI) -> str:
    secret_id = app.state.secret_store.put(
        kind="DOWNLOADER_CREDENTIAL",
        value=b'{"username":"admin","password":"synthetic-password","api_key":null}',
    )
    downloader_id = new_uuid()
    now = datetime.now(UTC)
    source_root = app.state.settings.data_dir / "source"
    source_root.mkdir(parents=True, exist_ok=True)
    with app.state.runtime.session_factory() as session:
        session.add(
            Downloader(
                id=downloader_id,
                name="v0.1.5 qB",
                type="QBITTORRENT",
                base_url="http://qb.invalid:8080",
                secret_id=secret_id,
                monitor_rules={},
                path_mappings=[
                    {"remote_prefix": "/downloads", "container_prefix": str(source_root)}
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
