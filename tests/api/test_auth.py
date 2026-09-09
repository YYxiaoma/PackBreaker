from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app.api.dependencies import CSRF_COOKIE, SESSION_COOKIE
from backend.app.config import AppSettings
from backend.app.infrastructure.persistence.models import AdminSession
from backend.app.infrastructure.security import token_digest
from backend.app.main import create_app

_PASSWORD = "synthetic correct horse battery staple"


def _settings(tmp_path: Path) -> AppSettings:
    return AppSettings(
        config_dir=(tmp_path / "config").resolve(),
        data_dir=(tmp_path / "data").resolve(),
    )


def _setup(client: TestClient) -> None:
    response = client.post("/api/v1/auth/setup", json={"password": _PASSWORD})
    assert response.status_code == 201


def test_auth_status_distinguishes_setup_from_logged_out(tmp_path: Path) -> None:
    with TestClient(
        create_app(settings=_settings(tmp_path)), base_url="https://testserver"
    ) as client:
        initial = client.get("/api/v1/auth/me")
        assert initial.status_code == 200
        assert initial.json() == {
            "configured": False,
            "authenticated": False,
            "permissions": [],
            "expires_at": None,
        }

        _setup(client)
        configured = client.get("/api/v1/auth/me")
        assert configured.status_code == 200
        assert configured.json() == {
            "configured": True,
            "authenticated": False,
            "permissions": [],
            "expires_at": None,
        }


def test_setup_can_only_complete_once(tmp_path: Path) -> None:
    with TestClient(
        create_app(settings=_settings(tmp_path)), base_url="https://testserver"
    ) as client:
        _setup(client)
        response = client.post("/api/v1/auth/setup", json={"password": _PASSWORD})

    assert response.status_code == 409
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "AUTH_SETUP_COMPLETE"
    assert "X-Trace-Id" in response.headers


def test_login_session_csrf_and_logout_flow(tmp_path: Path) -> None:
    app = create_app(settings=_settings(tmp_path))
    with TestClient(app, base_url="https://testserver") as client:
        _setup(client)
        rejected = client.post("/api/v1/auth/login", json={"password": "wrong password 123"})
        assert rejected.status_code == 401
        assert rejected.json()["code"] == "AUTH_INVALID_CREDENTIALS"

        login = client.post("/api/v1/auth/login", json={"password": _PASSWORD})
        assert login.status_code == 200
        session_token = client.cookies.get(SESSION_COOKIE)
        csrf_token = client.cookies.get(CSRF_COOKIE)
        assert session_token is not None
        assert csrf_token is not None
        set_cookie_headers = login.headers.get_list("set-cookie")
        session_header = next(value for value in set_cookie_headers if SESSION_COOKIE in value)
        csrf_header = next(value for value in set_cookie_headers if CSRF_COOKIE in value)
        assert "HttpOnly" in session_header
        assert "Secure" in session_header
        assert "SameSite=strict" in session_header
        assert "HttpOnly" not in csrf_header
        assert "Secure" in csrf_header

        with app.state.runtime.session_factory() as session:
            stored = session.scalar(select(AdminSession))
            assert stored is not None
            assert stored.token_digest == token_digest(session_token)
            assert session_token not in stored.token_digest
            assert csrf_token not in stored.csrf_digest

        me = client.get("/api/v1/auth/me")
        assert me.status_code == 200
        assert me.json()["authenticated"] is True
        assert me.json()["configured"] is True
        assert me.json()["permissions"] == ["admin"]

        missing_csrf = client.post("/api/v1/auth/logout")
        assert missing_csrf.status_code == 403
        assert missing_csrf.json()["code"] == "CSRF_INVALID"

        tampered_csrf = client.post(
            "/api/v1/auth/logout",
            headers={"X-CSRF-Token": "tampered-token"},
        )
        assert tampered_csrf.status_code == 403

        logout = client.post(
            "/api/v1/auth/logout",
            headers={"X-CSRF-Token": csrf_token},
        )
        assert logout.status_code == 204
        after_logout = client.get("/api/v1/auth/me").json()
        assert after_logout["configured"] is True
        assert after_logout["authenticated"] is False


def test_revoked_session_stays_revoked_after_app_restart(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    app = create_app(settings=settings)
    with TestClient(app, base_url="https://testserver") as client:
        _setup(client)
        client.post("/api/v1/auth/login", json={"password": _PASSWORD})
        session_token = client.cookies.get(SESSION_COOKIE)
        csrf_token = client.cookies.get(CSRF_COOKIE)
        assert session_token is not None and csrf_token is not None
        response = client.post(
            "/api/v1/auth/logout",
            headers={"X-CSRF-Token": csrf_token},
        )
        assert response.status_code == 204

    with TestClient(create_app(settings=settings), base_url="https://testserver") as client:
        client.cookies.set(SESSION_COOKIE, session_token)
        response = client.get("/api/v1/auth/me")
        assert response.status_code == 200
        assert response.json()["authenticated"] is False


def test_expired_session_is_rejected(tmp_path: Path) -> None:
    app = create_app(settings=_settings(tmp_path))
    with TestClient(app, base_url="https://testserver") as client:
        _setup(client)
        client.post("/api/v1/auth/login", json={"password": _PASSWORD})
        with app.state.runtime.session_factory() as session:
            stored = session.scalar(select(AdminSession))
            assert stored is not None
            stored.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            session.commit()

        assert client.get("/api/v1/auth/me").json()["authenticated"] is False


def test_login_failures_are_rate_limited(tmp_path: Path) -> None:
    with TestClient(
        create_app(settings=_settings(tmp_path)), base_url="https://testserver"
    ) as client:
        _setup(client)
        for _ in range(5):
            response = client.post(
                "/api/v1/auth/login",
                json={"password": "wrong password 123"},
            )
            assert response.status_code == 401

        limited = client.post(
            "/api/v1/auth/login",
            json={"password": "wrong password 123"},
        )

    assert limited.status_code == 429
    assert limited.json()["code"] == "AUTH_RATE_LIMITED"
    assert int(limited.headers["Retry-After"]) >= 1
