from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app.api.dependencies import CSRF_COOKIE
from backend.app.application.task_actions import TaskMutationActionResult
from backend.app.config import AppSettings
from backend.app.domain.task_state import TaskStatus
from backend.app.infrastructure.persistence.models import ApiToken
from backend.app.infrastructure.security import token_digest
from backend.app.main import create_app

_PASSWORD = "synthetic correct horse battery staple"


def _settings(tmp_path: Path) -> AppSettings:
    return AppSettings(
        config_dir=(tmp_path / "config").resolve(),
        data_dir=(tmp_path / "data").resolve(),
    )


def _authenticated_client(tmp_path: Path) -> tuple[TestClient, FastAPI]:
    app = create_app(settings=_settings(tmp_path))
    client = TestClient(app, base_url="https://testserver")
    client.__enter__()
    setup = client.post("/api/v1/auth/setup", json={"password": _PASSWORD})
    assert setup.status_code == 201
    login = client.post("/api/v1/auth/login", json={"password": _PASSWORD})
    assert login.status_code == 200
    return client, app


def _create_token(
    client: TestClient,
    *,
    scopes: list[str],
    name: str = "automation",
) -> dict[str, object]:
    csrf = client.cookies.get(CSRF_COOKIE)
    assert csrf is not None
    response = client.post(
        "/api/v1/api-tokens",
        headers={"X-CSRF-Token": csrf},
        json={
            "name": name,
            "scopes": scopes,
            "expires_at": (datetime.now(UTC) + timedelta(days=30)).isoformat(),
        },
    )
    assert response.status_code == 201
    return cast(dict[str, object], response.json())


def test_api_token_plaintext_is_returned_once_and_only_digest_is_stored(tmp_path: Path) -> None:
    client, app = _authenticated_client(tmp_path)
    try:
        created = _create_token(client, scopes=["config:read", "tasks:read"])
        plaintext = created["token"]
        assert isinstance(plaintext, str)
        assert plaintext.startswith("pbk_")

        with app.state.runtime.session_factory() as session:
            stored = session.scalar(select(ApiToken))
            assert stored is not None
            assert stored.token_digest == token_digest(plaintext)
            assert plaintext != stored.token_digest

        listing = client.get("/api/v1/api-tokens")
        assert listing.status_code == 200
        item = listing.json()["items"][0]
        assert "token" not in item
        assert "token_digest" not in item
        assert set(item["scopes"]) == {"config:read", "tasks:read"}
    finally:
        client.__exit__(None, None, None)


def test_api_token_scope_is_enforced_on_system_status(tmp_path: Path) -> None:
    client, _app = _authenticated_client(tmp_path)
    try:
        allowed = _create_token(client, scopes=["config:read"], name="allowed")
        denied = _create_token(client, scopes=["tasks:read"], name="denied")

        response = client.get(
            "/api/v1/system/status",
            headers={"Authorization": f"Bearer {allowed['token']}"},
        )
        assert response.status_code == 200
        assert response.json()["authenticated_via"] == "api_token"
        assert response.json()["worker"]["running"] is True

        response = client.get(
            "/api/v1/system/status",
            headers={"Authorization": f"Bearer {denied['token']}"},
        )
        assert response.status_code == 403
        assert response.json()["code"] == "API_TOKEN_SCOPE_FORBIDDEN"
    finally:
        client.__exit__(None, None, None)


def test_tasks_write_api_token_can_execute_without_csrf_but_tasks_read_cannot(
    tmp_path: Path,
) -> None:
    client, app = _authenticated_client(tmp_path)
    try:
        allowed = _create_token(client, scopes=["tasks:write"], name="task-writer")
        denied = _create_token(client, scopes=["tasks:read"], name="task-reader")
        execute = AsyncMock(
            return_value=TaskMutationActionResult(
                action="execute",
                task_id="task-token",
                status=TaskStatus.ADDING,
                task_version=3,
                execution_plan_id="plan-token",
                operation_replayed=False,
                receipt_id="receipt-token",
                idempotency_replayed=False,
            )
        )
        app.state.task_action_service = SimpleNamespace(execute=execute, cancel=AsyncMock())
        payload = {"action": "execute", "execution_plan_id": "plan-token"}

        accepted = client.post(
            "/api/v1/tasks/task-token/actions",
            headers={
                "Authorization": f"Bearer {allowed['token']}",
                "Idempotency-Key": "token-execute",
            },
            json=payload,
        )
        assert accepted.status_code == 200
        assert accepted.json()["status"] == "ADDING"
        assert execute.await_count == 1
        assert execute.await_args is not None
        actor = execute.await_args.kwargs["actor"]
        assert actor.kind == "api_token"
        assert actor.subject_id == allowed["id"]

        rejected = client.post(
            "/api/v1/tasks/task-token/actions",
            headers={
                "Authorization": f"Bearer {denied['token']}",
                "Idempotency-Key": "token-execute-denied",
            },
            json=payload,
        )
        assert rejected.status_code == 403
        assert rejected.json()["code"] == "API_TOKEN_SCOPE_FORBIDDEN"
        assert execute.await_count == 1
    finally:
        client.__exit__(None, None, None)


def test_api_token_revoke_is_persistent_and_idempotent(tmp_path: Path) -> None:
    client, _app = _authenticated_client(tmp_path)
    try:
        created = _create_token(client, scopes=["config:read"])
        token_id = created["id"]
        token = created["token"]
        assert isinstance(token_id, str) and isinstance(token, str)
        csrf = client.cookies.get(CSRF_COOKIE)
        assert csrf is not None

        first = client.delete(
            f"/api/v1/api-tokens/{token_id}",
            headers={"X-CSRF-Token": csrf},
        )
        second = client.delete(
            f"/api/v1/api-tokens/{token_id}",
            headers={"X-CSRF-Token": csrf},
        )
        assert first.status_code == second.status_code == 204

        rejected = client.get(
            "/api/v1/system/status",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert rejected.status_code == 401
        assert rejected.json()["code"] == "API_TOKEN_INVALID"
    finally:
        client.__exit__(None, None, None)


def test_api_token_management_requires_admin_session_and_csrf(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings=settings), base_url="https://testserver") as anonymous:
        assert anonymous.get("/api/v1/api-tokens").status_code == 401

    client, _app = _authenticated_client(tmp_path)
    try:
        response = client.post(
            "/api/v1/api-tokens",
            json={
                "name": "missing-csrf",
                "scopes": ["config:read"],
                "expires_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
            },
        )
        assert response.status_code == 403
        assert response.json()["code"] == "CSRF_INVALID"
    finally:
        client.__exit__(None, None, None)
