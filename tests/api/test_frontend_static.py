from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app.config import AppSettings
from backend.app.main import create_app


def _settings(tmp_path: Path, frontend_dir: Path | None) -> AppSettings:
    return AppSettings(
        config_dir=(tmp_path / "config").resolve(),
        data_dir=(tmp_path / "data").resolve(),
        frontend_dir=frontend_dir,
    )


def test_frontend_static_assets_and_api_share_same_app(tmp_path: Path) -> None:
    frontend = (tmp_path / "frontend").resolve()
    assets = frontend / "assets"
    assets.mkdir(parents=True)
    (frontend / "index.html").write_text("<main>PackBreaker UI</main>", encoding="utf-8")
    (assets / "app.js").write_text("console.log('synthetic')", encoding="utf-8")

    app = create_app(settings=_settings(tmp_path, frontend))
    with TestClient(app) as client:
        index = client.get("/")
        asset = client.get("/assets/app.js")
        live = client.get("/api/v1/health/live")
        unknown_api = client.get("/api/v1/does-not-exist")

    assert index.status_code == 200
    assert "PackBreaker UI" in index.text
    assert index.headers["X-Content-Type-Options"] == "nosniff"
    assert asset.status_code == 200
    assert asset.text == "console.log('synthetic')"
    assert live.status_code == 200
    assert live.json()["service"] == "packbreaker"
    assert unknown_api.status_code == 404


def test_configured_frontend_directory_must_contain_build_output(tmp_path: Path) -> None:
    frontend = (tmp_path / "frontend").resolve()
    frontend.mkdir()

    with pytest.raises(ValueError, match="index.html"):
        create_app(settings=_settings(tmp_path, frontend))
