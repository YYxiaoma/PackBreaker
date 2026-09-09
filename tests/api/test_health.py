from uuid import UUID

from fastapi.testclient import TestClient

from backend.app.main import create_app


def test_live_health_returns_trace_id() -> None:
    client = TestClient(create_app())

    response = client.get("/api/v1/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "packbreaker"}
    parsed_trace_id = UUID(response.headers["X-Trace-Id"])
    assert str(parsed_trace_id) == response.headers["X-Trace-Id"]
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "default-src 'none'" in response.headers["Content-Security-Policy"]


def test_valid_caller_trace_id_is_preserved() -> None:
    client = TestClient(create_app())
    trace_id = "7aa9d6f7-61f3-4a75-a1ec-6210291163e1"

    response = client.get("/api/v1/health/live", headers={"X-Trace-Id": trace_id})

    assert response.headers["X-Trace-Id"] == trace_id


def test_invalid_caller_trace_id_is_replaced() -> None:
    client = TestClient(create_app())

    response = client.get("/api/v1/health/live", headers={"X-Trace-Id": "not-a-uuid"})

    assert response.headers["X-Trace-Id"] != "not-a-uuid"
    parsed_trace_id = UUID(response.headers["X-Trace-Id"])
    assert str(parsed_trace_id) == response.headers["X-Trace-Id"]


def test_https_responses_include_hsts() -> None:
    client = TestClient(create_app(), base_url="https://testserver")

    response = client.get("/api/v1/health/live")

    assert response.headers["Strict-Transport-Security"] == "max-age=31536000"
