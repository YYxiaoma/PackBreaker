from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from backend.app.api.dependencies import CSRF_COOKIE
from backend.app.config import AppSettings
from backend.app.infrastructure.persistence.models import (
    Downloader,
    Site,
    UnpackDefinition,
    UnpackDefinitionSelectedSource,
    UnpackExecution,
    UnpackExecutionItem,
    UnpackExternalOperationJournal,
    UnpackMatchCandidate,
    UnpackSourceScan,
    new_uuid,
)
from backend.app.main import create_app

_PASSWORD = "synthetic correct horse battery staple"


def _authenticated_client(tmp_path: Path) -> tuple[TestClient, FastAPI]:
    settings = AppSettings(
        config_dir=(tmp_path / "config").resolve(),
        data_dir=(tmp_path / "data").resolve(),
    )
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    app = create_app(settings=settings)
    client = TestClient(app, base_url="https://testserver")
    client.__enter__()
    assert (
        client.post(
            "/api/v1/auth/setup",
            json={"username": "admin", "password": _PASSWORD},
        ).status_code
        == 201
    )
    assert (
        client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": _PASSWORD},
        ).status_code
        == 200
    )
    return client, app


def _csrf(client: TestClient) -> dict[str, str]:
    token = client.cookies.get(CSRF_COOKIE)
    assert token is not None
    return {"X-CSRF-Token": token}


def _create_ready_site(app: FastAPI) -> str:
    site_id = new_uuid()
    now = datetime.now(UTC)
    with app.state.runtime.session_factory() as session:
        session.add(
            Site(
                id=site_id,
                name="M-Team v2 测试站",
                type="MTEAM",
                base_url="https://api.m-team.cc",
                credential_kind="API_KEY",
                secret_id=None,
                capabilities={},
                connection_status="OK",
                enabled=True,
                version=1,
                last_test_at=now,
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()
    return site_id


def _create_ready_downloader(app: FastAPI) -> str:
    downloader_id = new_uuid()
    now = datetime.now(UTC)
    with app.state.runtime.session_factory() as session:
        session.add(
            Downloader(
                id=downloader_id,
                name="v2 目标 qB",
                type="QBITTORRENT",
                base_url="http://qb.test",
                monitor_rules={},
                path_mappings=[],
                capabilities={"supports_selective_files": True},
                connection_status="OK",
                path_mapping_status="OK",
                enabled=True,
                version=1,
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()
    return downloader_id


def test_unpack_write_api_requires_admin_csrf(tmp_path: Path) -> None:
    client, _app = _authenticated_client(tmp_path)
    try:
        response = client.post(
            "/api/v1/unpack/definitions/synthetic-definition/actions",
            json={"action": "run"},
        )

        assert response.status_code == 403
        assert response.json()["code"] == "CSRF_INVALID"
    finally:
        client.__exit__(None, None, None)


def _seed_item_action_fixture(
    app: FastAPI,
    *,
    item_status: str,
) -> tuple[str, str]:
    now = datetime.now(UTC)
    definition_id = new_uuid()
    execution_id = new_uuid()
    item_id = new_uuid()
    candidate_id = new_uuid()
    with app.state.runtime.session_factory() as session:
        session.add(
            UnpackDefinition(
                id=definition_id,
                name="影片动作 API 测试",
                trigger_kind="MANUAL",
                status="PENDING_EXECUTION",
                source_kind="DIRECTORY",
                execution_scope_kind="ALL_MATCHING_MEDIA",
                source_config={"directory_path": app.state.settings.data_dir.as_posix()},
                file_filter={"extensions": [".mkv"]},
                site_ids=["site-synthetic"],
                output_config={
                    "output_directory": app.state.settings.data_dir.as_posix(),
                    "storage_mode": "HARDLINK",
                    "conflict_policy": "VERIFY_REUSE_OR_STOP",
                    "target_downloader_id": "downloader-synthetic",
                },
                retry_enabled=True,
                max_retries=3,
                auto_match_threshold_bps=9800,
                version=1,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            UnpackExecution(
                id=execution_id,
                definition_id=definition_id,
                trigger="MANUAL",
                status=(
                    "REVIEW_REQUIRED"
                    if item_status in {"REVIEW_REQUIRED", "MATCHED_AUTO"}
                    else "COMPLETED_WITH_ERRORS"
                ),
                config_snapshot={},
                discovery_complete=True,
                total_count=1,
                matched_auto_count=1 if item_status == "MATCHED_AUTO" else 0,
                review_count=1 if item_status == "REVIEW_REQUIRED" else 0,
                content_verified_count=0,
                content_mismatch_count=0,
                timeout_count=1 if item_status == "MATCH_TIMEOUT" else 0,
                error_count=1 if item_status == "MATCH_ERROR" else 0,
                completed_count=0,
                started_at=now,
                finished_at=(now if item_status in {"MATCH_TIMEOUT", "MATCH_ERROR"} else None),
                version=1,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            UnpackExecutionItem(
                id=item_id,
                execution_id=execution_id,
                source_object_key="source-action-api",
                source_snapshot={},
                media_identity={"title": "Movie"},
                status=item_status,
                selected_candidate_id=(candidate_id if item_status == "MATCHED_AUTO" else None),
                candidate_generation=2,
                retry_count=0,
                last_error_code=("UNPACK_MATCH_ERROR" if item_status == "MATCH_ERROR" else None),
                last_error_message=(
                    "匹配失败，可重试该影片" if item_status == "MATCH_ERROR" else None
                ),
                match_finished_at=now,
                version=3,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            UnpackMatchCandidate(
                id=candidate_id,
                item_id=item_id,
                generation=2,
                site_id="site-synthetic",
                candidate_key="torrent-synthetic",
                title="Movie 2026",
                size_bytes=123,
                score_bps=9800,
                is_exact_match=False,
                evidence={"hard_conflicts": []},
                verification_status="NOT_CHECKED",
                raw_ref={"torrent_id": "torrent-synthetic"},
                created_at=now,
            )
        )
        session.commit()
    return item_id, candidate_id


def test_manual_selected_media_api_flow_saves_pending_then_runs(tmp_path: Path) -> None:
    client, app = _authenticated_client(tmp_path)
    try:
        site_id = _create_ready_site(app)
        downloader_id = _create_ready_downloader(app)
        movies = app.state.settings.data_dir / "movies"
        output = app.state.settings.data_dir / "seeding"
        movies.mkdir()
        output.mkdir()
        (movies / "Film.A.2026.2160p.mkv").write_bytes(b"a" * 16)
        (movies / "Film.B.2026.1080p.mkv").write_bytes(b"b" * 32)
        (movies / "Film.B.2026.nfo").write_text("metadata", encoding="utf-8")

        roots = client.get("/api/v1/files/tree/roots")
        assert roots.status_code == 200
        root = next(
            item
            for item in roots.json()["items"]
            if item["display_path"] == app.state.settings.data_dir.as_posix()
        )
        tree = client.get(
            "/api/v1/files/tree",
            params={"selection_token": root["selection_token"]},
        )
        assert tree.status_code == 200
        movies_entry = next(item for item in tree.json()["entries"] if item["name"] == "movies")

        scan = client.post(
            "/api/v1/unpack/source-scans",
            headers=_csrf(client),
            json={
                "selection_token": movies_entry["selection_token"],
                "file_filter": {"extensions": [".mkv"]},
            },
        )
        assert scan.status_code == 201
        scan_body = scan.json()
        assert scan_body["discovered_count"] == 2
        assert scan_body["selected_count"] == 0

        filtered = client.get(
            f"/api/v1/unpack/source-scans/{scan_body['id']}/items",
            params={"resolution": "2160p"},
        )
        assert filtered.status_code == 200
        assert len(filtered.json()["items"]) == 1
        selected_key = filtered.json()["items"][0]["source_object_key"]

        selected = client.put(
            f"/api/v1/unpack/source-scans/{scan_body['id']}/selection",
            headers=_csrf(client),
            json={"source_object_keys": [selected_key], "selected": True},
        )
        assert selected.status_code == 200
        assert selected.json() == {"discovered_count": 2, "selected_count": 1}

        created = client.post(
            "/api/v1/unpack/definitions",
            headers=_csrf(client),
            json={
                "name": "电影库手动拆包",
                "trigger_kind": "MANUAL",
                "source_kind": "DIRECTORY",
                "execution_scope_kind": "SELECTED_MEDIA",
                "source_config": {"directory_path": movies.as_posix()},
                "file_filter": {"extensions": [".mkv"]},
                "site_ids": [site_id],
                "output_config": {
                    "output_directory": output.as_posix(),
                    "storage_mode": "HARDLINK",
                    "conflict_policy": "VERIFY_REUSE_OR_STOP",
                    "target_downloader_id": downloader_id,
                },
                "retry_enabled": True,
                "max_retries": 3,
                "auto_match_threshold_bps": 9680,
                "source_scan_id": scan_body["id"],
            },
        )
        assert created.status_code == 201
        body = created.json()
        assert body["status"] == "PENDING_EXECUTION"
        assert body["selected_source_count"] == 1
        assert body["auto_match_threshold_bps"] == 9680
        assert body["output_config"]["target_downloader_id"] == downloader_id

        with app.state.runtime.session_factory() as session:
            assert session.get(UnpackSourceScan, scan_body["id"]) is None
            assert (
                session.scalar(
                    select(func.count(UnpackDefinitionSelectedSource.id)).where(
                        UnpackDefinitionSelectedSource.definition_id == body["id"]
                    )
                )
                == 1
            )
            assert (
                session.scalar(
                    select(func.count(UnpackExecution.id)).where(
                        UnpackExecution.definition_id == body["id"]
                    )
                )
                == 0
            )

        run = client.post(
            f"/api/v1/unpack/definitions/{body['id']}/actions",
            headers=_csrf(client),
            json={"action": "run"},
        )
        assert run.status_code == 200
        assert run.json()["definition"]["status"] == "PENDING_EXECUTION"
        assert run.json()["execution_id"] is not None
        execution_id = run.json()["execution_id"]
        app.state.unpack_discovery_service.discover_next_page(execution_id, limit=100)

        execution_response = client.get(f"/api/v1/unpack/executions/{execution_id}")
        assert execution_response.status_code == 200
        assert execution_response.json()["status"] == "MATCHING"
        assert execution_response.json()["total_count"] == 1
        assert execution_response.json()["discovery_complete"] is True

        items_response = client.get(f"/api/v1/unpack/executions/{execution_id}/items")
        assert items_response.status_code == 200
        assert len(items_response.json()["items"]) == 1
        assert items_response.json()["items"][0]["status"] == "MATCH_PENDING"
        assert items_response.json()["items"][0]["source_snapshot"]["file_type"] == "regular"
        item_id = items_response.json()["items"][0]["id"]

        with app.state.runtime.session_factory() as session:
            session.add_all(
                [
                    UnpackMatchCandidate(
                        id="candidate-high",
                        item_id=item_id,
                        generation=0,
                        site_id=site_id,
                        candidate_key="torrent-high",
                        title="Film A 2026 2160p",
                        size_bytes=16,
                        seeders=20,
                        score_bps=9680,
                        is_exact_match=False,
                        evidence={"hard_conflicts": []},
                        verification_status="NOT_CHECKED",
                        raw_ref={"torrent_id": "torrent-high"},
                        created_at=datetime.now(UTC),
                    ),
                    UnpackMatchCandidate(
                        id="candidate-conflict",
                        item_id=item_id,
                        generation=0,
                        site_id=site_id,
                        candidate_key="torrent-conflict",
                        title="Film A 2025 2160p",
                        size_bytes=16,
                        seeders=99,
                        score_bps=9900,
                        is_exact_match=False,
                        evidence={"hard_conflicts": ["YEAR_CONFLICT"]},
                        verification_status="NOT_CHECKED",
                        raw_ref={"torrent_id": "torrent-conflict"},
                        created_at=datetime.now(UTC),
                    ),
                ]
            )
            session.commit()

        candidates_response = client.get(f"/api/v1/unpack/items/{item_id}/candidates")
        assert candidates_response.status_code == 200
        candidates_body = candidates_response.json()
        assert candidates_body["generation"] == 0
        assert candidates_body["item_version"] == 1
        assert candidates_body["default_candidate_id"] == "candidate-high"
        assert [item["id"] for item in candidates_body["candidates"]] == [
            "candidate-high",
            "candidate-conflict",
        ]
        with app.state.runtime.session_factory() as session:
            item = session.get(UnpackExecutionItem, item_id)
            assert item is not None
            assert item.selected_candidate_id is None

        with app.state.runtime.session_factory() as session:
            assert (
                session.scalar(
                    select(func.count(UnpackExecution.id)).where(
                        UnpackExecution.definition_id == body["id"]
                    )
                )
                == 1
            )
    finally:
        client.__exit__(None, None, None)


def test_monitor_definition_is_pending_until_run_enables_it(tmp_path: Path) -> None:
    client, app = _authenticated_client(tmp_path)
    try:
        site_id = _create_ready_site(app)
        downloader_id = _create_ready_downloader(app)
        movies = app.state.settings.data_dir / "movies"
        output = app.state.settings.data_dir / "seeding"
        movies.mkdir()
        output.mkdir()

        created = client.post(
            "/api/v1/unpack/definitions",
            headers=_csrf(client),
            json={
                "name": "目录监控拆包",
                "trigger_kind": "MONITOR",
                "source_kind": "DIRECTORY",
                "execution_scope_kind": "ALL_MATCHING_MEDIA",
                "source_config": {"directory_path": movies.as_posix()},
                "file_filter": {"extensions": [".mkv"]},
                "site_ids": [site_id],
                "output_config": {
                    "output_directory": output.as_posix(),
                    "storage_mode": "HARDLINK",
                    "conflict_policy": "VERIFY_REUSE_OR_STOP",
                    "target_downloader_id": downloader_id,
                },
                "cron_expression": "0 3 * * *",
            },
        )
        assert created.status_code == 201
        body = created.json()
        assert body["status"] == "PENDING_EXECUTION"

        run = client.post(
            f"/api/v1/unpack/definitions/{body['id']}/actions",
            headers=_csrf(client),
            json={"action": "run"},
        )
        assert run.status_code == 200
        assert run.json()["definition"]["status"] == "ENABLED"
        assert run.json()["execution_id"] is None
    finally:
        client.__exit__(None, None, None)


def test_unpack_item_review_api_requires_version_and_replays_idempotently(
    tmp_path: Path,
) -> None:
    client, app = _authenticated_client(tmp_path)
    try:
        item_id, candidate_id = _seed_item_action_fixture(
            app,
            item_status="REVIEW_REQUIRED",
        )
        missing_version = client.put(
            f"/api/v1/unpack/items/{item_id}/review",
            headers={
                **_csrf(client),
                "Idempotency-Key": "api-review-1",
            },
            json={
                "decision": "APPROVE",
                "candidate_id": candidate_id,
                "generation": 2,
            },
        )
        assert missing_version.status_code == 428

        headers = {
            **_csrf(client),
            "If-Match": '"3"',
            "Idempotency-Key": "api-review-1",
        }
        reviewed = client.put(
            f"/api/v1/unpack/items/{item_id}/review",
            headers=headers,
            json={
                "decision": "APPROVE",
                "candidate_id": candidate_id,
                "generation": 2,
            },
        )
        assert reviewed.status_code == 200
        body = reviewed.json()
        assert body["item_status"] == "MATCHED_MANUAL"
        assert body["item_version"] == 4
        assert body["replayed"] is False

        replay = client.put(
            f"/api/v1/unpack/items/{item_id}/review",
            headers=headers,
            json={
                "decision": "APPROVE",
                "candidate_id": candidate_id,
                "generation": 2,
            },
        )
        assert replay.status_code == 200
        assert replay.json()["decision_id"] == body["decision_id"]
        assert replay.json()["replayed"] is True
    finally:
        client.__exit__(None, None, None)


def test_unpack_definition_edit_delete_requires_csrf_and_current_version(tmp_path: Path) -> None:
    client, app = _authenticated_client(tmp_path)
    try:
        site_id = _create_ready_site(app)
        downloader_id = _create_ready_downloader(app)
        movies = app.state.settings.data_dir / "movies"
        output = app.state.settings.data_dir / "seeding"
        movies.mkdir()
        output.mkdir()
        payload = {
            "name": "影片匹配任务",
            "trigger_kind": "MANUAL",
            "source_kind": "DIRECTORY",
            "execution_scope_kind": "ALL_MATCHING_MEDIA",
            "source_config": {"directory_path": movies.as_posix()},
            "file_filter": {"extensions": [".mkv"]},
            "site_ids": [site_id],
            "output_config": {
                "output_directory": output.as_posix(),
                "storage_mode": "HARDLINK",
                "conflict_policy": "VERIFY_REUSE_OR_STOP",
                "target_downloader_id": downloader_id,
            },
        }
        created = client.post("/api/v1/unpack/definitions", json=payload, headers=_csrf(client))
        assert created.status_code == 201
        row = created.json()
        endpoint = f"/api/v1/unpack/definitions/{row['id']}"
        assert (
            client.put(
                endpoint, json={**payload, "name": "更新名称"}, headers=_csrf(client)
            ).status_code
            == 428
        )
        assert (
            client.put(
                endpoint, json={**payload, "name": "更新名称"}, headers={"If-Match": "1"}
            ).status_code
            == 403
        )
        updated = client.put(
            endpoint,
            json={**payload, "name": "更新名称"},
            headers={**_csrf(client), "If-Match": "1"},
        )
        assert updated.status_code == 200
        assert updated.json()["version"] == 2
        assert updated.json()["name"] == "更新名称"
        assert (
            client.delete(endpoint, headers={**_csrf(client), "If-Match": "1"}).status_code == 409
        )
        deleted = client.delete(endpoint, headers={**_csrf(client), "If-Match": "2"})
        assert deleted.status_code == 204
        assert client.get(endpoint).status_code == 404
    finally:
        client.__exit__(None, None, None)


def test_no_match_count_is_reported_in_execution_detail_and_list(tmp_path: Path) -> None:
    client, app = _authenticated_client(tmp_path)
    try:
        item_id, _candidate_id = _seed_item_action_fixture(app, item_status="NO_MATCH")
        with app.state.runtime.session_factory() as session:
            item = session.get(UnpackExecutionItem, item_id)
            assert item is not None
            execution_id = item.execution_id
        detail = client.get(f"/api/v1/unpack/executions/{execution_id}")
        assert detail.status_code == 200
        assert detail.json()["no_match_count"] == 1
        listed = client.get("/api/v1/unpack/executions")
        assert listed.status_code == 200
        matching = next(row for row in listed.json()["items"] if row["id"] == execution_id)
        assert matching["no_match_count"] == 1
    finally:
        client.__exit__(None, None, None)


def test_delete_completed_unmatched_task_removes_history_without_media_mutation(
    tmp_path: Path,
) -> None:
    client, app = _authenticated_client(tmp_path)
    try:
        item_id, _candidate_id = _seed_item_action_fixture(app, item_status="NO_MATCH")
        with app.state.runtime.session_factory() as session:
            item = session.get(UnpackExecutionItem, item_id)
            assert item is not None
            execution_id = item.execution_id
            execution = session.get(UnpackExecution, execution_id)
            assert execution is not None
            definition_id = execution.definition_id
        response = client.delete(
            f"/api/v1/unpack/definitions/{definition_id}",
            headers={**_csrf(client), "If-Match": "1"},
        )
        assert response.status_code == 204
        with app.state.runtime.session_factory() as session:
            assert session.get(UnpackDefinition, definition_id) is None
            assert session.get(UnpackExecution, execution_id) is None
            assert session.get(UnpackExecutionItem, item_id) is None
    finally:
        client.__exit__(None, None, None)


def test_bulk_retry_no_match_api_uses_execution_version(tmp_path: Path) -> None:
    client, app = _authenticated_client(tmp_path)
    try:
        item_id, _candidate_id = _seed_item_action_fixture(app, item_status="NO_MATCH")
        with app.state.runtime.session_factory() as session:
            item = session.get(UnpackExecutionItem, item_id)
            assert item is not None
            execution_id = item.execution_id
            execution = session.get(UnpackExecution, execution_id)
            assert execution is not None
            execution.status = "FAILED"
            session.commit()
            version = execution.version
        endpoint = f"/api/v1/unpack/executions/{execution_id}/actions"
        result = client.post(
            endpoint,
            headers={**_csrf(client), "If-Match": str(version)},
            json={"action": "retry_failed_matches"},
        )
        assert result.status_code == 200
        assert result.json()["retried_count"] == 1
        assert (
            client.get(f"/api/v1/unpack/executions/{execution_id}").json()["status"] == "MATCHING"
        )
        assert (
            client.post(
                endpoint,
                headers={**_csrf(client), "If-Match": str(version)},
                json={"action": "retry_failed_matches"},
            ).status_code
            == 409
        )
    finally:
        client.__exit__(None, None, None)


def test_unpack_item_retry_api_reopens_matching_with_new_generation(tmp_path: Path) -> None:
    client, app = _authenticated_client(tmp_path)
    try:
        item_id, _candidate_id = _seed_item_action_fixture(
            app,
            item_status="MATCH_ERROR",
        )
        response = client.post(
            f"/api/v1/unpack/items/{item_id}/actions",
            headers={**_csrf(client), "If-Match": "3"},
            json={"action": "retry_match"},
        )

        assert response.status_code == 200
        assert response.json() == {
            "item_id": item_id,
            "generation": 3,
            "retry_count": 0,
            "item_version": 4,
            "item_status": "MATCH_PENDING",
        }
        with app.state.runtime.session_factory() as session:
            item = session.get(UnpackExecutionItem, item_id)
            assert item is not None
            execution = session.get(UnpackExecution, item.execution_id)
            assert execution is not None
            assert execution.status == "MATCHING"
            assert execution.finished_at is None
    finally:
        client.__exit__(None, None, None)


def test_unpack_item_delete_api_requires_csrf_and_version_and_preserves_media(
    tmp_path: Path,
) -> None:
    client, app = _authenticated_client(tmp_path)
    try:
        item_id, candidate_id = _seed_item_action_fixture(app, item_status="NO_MATCH")
        media = app.state.settings.data_dir / "movie.mkv"
        media.write_bytes(b"original movie")
        endpoint = f"/api/v1/unpack/items/{item_id}"
        assert client.delete(endpoint, headers={"If-Match": "3"}).status_code == 403
        assert (
            client.delete(endpoint, headers={**_csrf(client), "If-Match": "2"}).status_code == 409
        )
        response = client.delete(endpoint, headers={**_csrf(client), "If-Match": "3"})
        assert response.status_code == 204
        assert media.read_bytes() == b"original movie"
        with app.state.runtime.session_factory() as session:
            assert session.get(UnpackExecutionItem, item_id) is None
            assert session.get(UnpackMatchCandidate, candidate_id) is None
    finally:
        client.__exit__(None, None, None)


def test_item_list_marks_external_journal_and_blocks_record_deletion(tmp_path: Path) -> None:
    client, app = _authenticated_client(tmp_path)
    try:
        item_id, _candidate_id = _seed_item_action_fixture(app, item_status="MATCH_ERROR")
        with app.state.runtime.session_factory() as session:
            item = session.get(UnpackExecutionItem, item_id)
            assert item is not None
            execution_id = item.execution_id
        endpoint = f"/api/v1/unpack/executions/{execution_id}/items"
        initial = client.get(endpoint)
        assert initial.status_code == 200
        assert initial.json()["items"][0]["has_external_operations"] is False
        now = datetime.now(UTC)
        with app.state.runtime.session_factory() as session:
            session.add(
                UnpackExternalOperationJournal(
                    id=new_uuid(),
                    item_id=item_id,
                    idempotency_key="f" * 64,
                    operation_type="UNPACK_AUX_STAGING_DIR",
                    target={},
                    intent={},
                    status="APPLIED",
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()
        blocked = client.get(endpoint)
        assert blocked.status_code == 200
        assert blocked.json()["items"][0]["has_external_operations"] is True
        deleted = client.delete(
            f"/api/v1/unpack/items/{item_id}",
            headers={**_csrf(client), "If-Match": "3"},
        )
        assert deleted.status_code == 409
        assert deleted.json()["code"] == "UNPACK_ITEM_DELETE_EXTERNAL_JOURNAL"
    finally:
        client.__exit__(None, None, None)


def test_unpack_auto_matched_item_can_enter_manual_review_api(tmp_path: Path) -> None:
    client, app = _authenticated_client(tmp_path)
    try:
        item_id, candidate_id = _seed_item_action_fixture(
            app,
            item_status="MATCHED_AUTO",
        )
        response = client.put(
            f"/api/v1/unpack/items/{item_id}/review",
            headers={
                **_csrf(client),
                "If-Match": "3",
                "Idempotency-Key": "api-auto-review-1",
            },
            json={
                "decision": "APPROVE",
                "candidate_id": candidate_id,
                "generation": 2,
            },
        )

        assert response.status_code == 200
        assert response.json()["item_status"] == "MATCHED_MANUAL"
        with app.state.runtime.session_factory() as session:
            item = session.get(UnpackExecutionItem, item_id)
            execution = session.get(UnpackExecution, item.execution_id) if item else None
            assert item is not None and execution is not None
            assert item.match_origin == "MANUAL"
            assert execution.status == "CONTENT_VERIFYING"
    finally:
        client.__exit__(None, None, None)
