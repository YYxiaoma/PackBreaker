import json
from pathlib import Path

from backend.app.main import create_app


def test_frontend_openapi_snapshot_matches_application() -> None:
    snapshot_path = Path(__file__).parents[2] / "frontend" / "openapi.json"
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))

    assert snapshot == create_app().openapi()


def test_legacy_unpack_routes_are_not_public_api() -> None:
    paths = create_app().openapi()["paths"]

    assert not any(path.startswith("/api/v1/tasks") for path in paths)
    assert not any(path.startswith("/api/v1/task-definitions") for path in paths)
