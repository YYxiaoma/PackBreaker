from pathlib import Path

from fastapi.testclient import TestClient

from backend.app.api.dependencies import SESSION_COOKIE
from backend.app.config import AppSettings
from backend.app.main import create_app

_PASSWORD = "synthetic correct horse battery staple"


def _settings(tmp_path: Path) -> AppSettings:
    return AppSettings(
        config_dir=(tmp_path / "config").resolve(),
        data_dir=(tmp_path / "data").resolve(),
        trusted_proxies="10.0.0.0/8",
    )


def test_trusted_proxy_https_sets_secure_cookie_and_hsts(tmp_path: Path) -> None:
    app = create_app(settings=_settings(tmp_path))
    with TestClient(
        app,
        base_url="http://testserver",
        client=("10.0.0.5", 50000),
    ) as client:
        client.post("/api/v1/auth/setup", json={"password": _PASSWORD})
        response = client.post(
            "/api/v1/auth/login",
            headers={"X-Forwarded-Proto": "https"},
            json={"password": _PASSWORD},
        )

    assert response.status_code == 200
    session_cookie = next(
        value for value in response.headers.get_list("set-cookie") if SESSION_COOKIE in value
    )
    assert "Secure" in session_cookie
    assert response.headers["Strict-Transport-Security"] == "max-age=31536000"


def test_untrusted_peer_cannot_force_secure_proxy_context(tmp_path: Path) -> None:
    app = create_app(settings=_settings(tmp_path))
    with TestClient(
        app,
        base_url="http://testserver",
        client=("203.0.113.5", 50000),
    ) as client:
        client.post("/api/v1/auth/setup", json={"password": _PASSWORD})
        response = client.post(
            "/api/v1/auth/login",
            headers={"X-Forwarded-Proto": "https"},
            json={"password": _PASSWORD},
        )

    assert response.status_code == 200
    session_cookie = next(
        value for value in response.headers.get_list("set-cookie") if SESSION_COOKIE in value
    )
    assert "Secure" not in session_cookie
    assert "Strict-Transport-Security" not in response.headers
