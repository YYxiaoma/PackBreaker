import json
from pathlib import Path

from backend.app.main import create_app


def test_frontend_openapi_snapshot_matches_application() -> None:
    snapshot_path = Path(__file__).parents[2] / "frontend" / "openapi.json"
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))

    assert snapshot == create_app().openapi()
