from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.config import AppSettings
from backend.app.main import create_app

_PASSWORD = "synthetic correct horse battery staple"


def _settings(tmp_path: Path, release_notes_path: Path) -> AppSettings:
    config = (tmp_path / "config").resolve()
    data = (tmp_path / "data").resolve()
    config.mkdir(mode=0o700)
    data.mkdir()
    return AppSettings(
        config_dir=config,
        data_dir=data,
        release_notes_path=release_notes_path,
    )


def _authenticated_client(tmp_path: Path, release_notes_path: Path) -> TestClient:
    app = create_app(settings=_settings(tmp_path, release_notes_path))
    client = TestClient(app, base_url="https://testserver")
    client.__enter__()
    setup = client.post("/api/v1/auth/setup", json={"username": "admin", "password": _PASSWORD})
    assert setup.status_code == 201
    login = client.post("/api/v1/auth/login", json={"username": "admin", "password": _PASSWORD})
    assert login.status_code == 200
    return client


def _catalog_file(tmp_path: Path, *, versions: list[str]) -> Path:
    path = tmp_path / "release_notes.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "entries": [
                    {
                        "version": version,
                        "tag": f"v{version}",
                        "released_on": "2026-11-18",
                        "summary": f"{version} 摘要",
                        "sections": [{"title": "新增", "items": [f"{version} 的新功能"]}],
                    }
                    for version in versions
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def test_release_notes_endpoint_returns_catalog(tmp_path: Path) -> None:
    client = _authenticated_client(tmp_path, _catalog_file(tmp_path, versions=["1.1.9", "1.1.10"]))
    try:
        response = client.get("/api/v1/system/release-notes")
    finally:
        client.__exit__(None, None, None)

    assert response.status_code == 200
    payload = response.json()
    assert payload["current_version"]
    assert [entry["version"] for entry in payload["entries"]] == ["1.1.10", "1.1.9"]
    assert payload["entries"][0]["sections"][0]["title"] == "新增"
    assert payload["entries"][0]["sections"][0]["items"] == ["1.1.10 的新功能"]


def test_release_notes_endpoint_tolerates_missing_file(tmp_path: Path) -> None:
    client = _authenticated_client(tmp_path, tmp_path / "absent.json")
    try:
        response = client.get("/api/v1/system/release-notes")
    finally:
        client.__exit__(None, None, None)

    assert response.status_code == 200
    assert response.json()["entries"] == []


def test_release_notes_endpoint_requires_authentication(tmp_path: Path) -> None:
    app: FastAPI = create_app(settings=_settings(tmp_path, tmp_path / "absent.json"))
    with TestClient(app, base_url="https://testserver") as client:
        response = client.get("/api/v1/system/release-notes")

    assert response.status_code == 401
