from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.api.dependencies import CSRF_COOKIE
from backend.app.application.ai_agent import AIAgentService
from backend.app.application.ai_runtime import AIReadOnlyAgent
from backend.app.application.errors import ApplicationError
from backend.app.config import AppSettings
from backend.app.infrastructure.adapters.ai_provider import AIProviderFactory
from backend.app.main import create_app

_PASSWORD = "synthetic correct horse battery staple"
_API_KEY = "sk-ai-runtime-secret-canary"


def _settings(tmp_path: Path) -> AppSettings:
    return AppSettings(
        config_dir=(tmp_path / "config").resolve(),
        data_dir=(tmp_path / "data").resolve(),
        task_driver_interval_seconds=3600,
        task_definition_driver_interval_seconds=3600,
        notification_driver_interval_seconds=3600,
        backup_driver_interval_seconds=3600,
    )


def _login(client: TestClient) -> dict[str, str]:
    assert client.post("/api/v1/auth/setup", json={"password": _PASSWORD}).status_code == 201
    assert (
        client.post(
            "/api/v1/auth/login", json={"username": "admin", "password": _PASSWORD}
        ).status_code
        == 200
    )
    csrf = client.cookies.get(CSRF_COOKIE)
    assert csrf is not None
    return {"X-CSRF-Token": csrf}


def _save_test_enable_ai(
    client: TestClient,
    app: FastAPI,
    headers: dict[str, str],
    handler: httpx2.AsyncBaseTransport,
    *,
    data_scopes: list[str] | None = None,
) -> AIAgentService:
    scopes = data_scopes or ["SYSTEM_HEALTH"]
    runtime = app.state.runtime
    secret_store = app.state.secret_store
    service = AIAgentService(
        runtime.session_factory,
        secret_store,
        provider_factory=AIProviderFactory(transport=handler),
    )
    app.state.ai_agent_service = service
    current = client.get("/api/v1/ai-agent/settings")
    saved = client.put(
        "/api/v1/ai-agent/settings",
        headers={**headers, "If-Match": current.headers["etag"]},
        json={
            "enabled": False,
            "provider_kind": "OPENAI_COMPATIBLE",
            "base_url": "https://provider.example/v1",
            "model": "model-a",
            "request_timeout_seconds": 15,
            "max_context_messages": 10,
            "data_scopes": scopes,
            "api_key_action": "SET",
            "api_key": _API_KEY,
        },
    )
    assert saved.status_code == 200
    assert (
        client.post("/api/v1/ai-agent/test", headers=headers, json={"use_saved": True}).status_code
        == 200
    )
    tested = client.get("/api/v1/ai-agent/settings")
    enabled = client.put(
        "/api/v1/ai-agent/settings",
        headers={**headers, "If-Match": tested.headers["etag"]},
        json={
            "enabled": True,
            "provider_kind": "OPENAI_COMPATIBLE",
            "base_url": "https://provider.example/v1",
            "model": "model-a",
            "request_timeout_seconds": 15,
            "max_context_messages": 10,
            "data_scopes": scopes,
            "api_key_action": "KEEP",
        },
    )
    assert enabled.status_code == 200
    return service


@pytest.mark.asyncio
async def test_read_only_agent_executes_allowed_tool_and_returns_final_answer(
    tmp_path: Path,
) -> None:
    chat_requests: list[dict[str, object]] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        if request.method == "GET":
            return httpx2.Response(200, json={"id": "model-a", "object": "model"})
        payload = json.loads(request.content)
        chat_requests.append(payload)
        messages = payload["messages"]
        assert isinstance(messages, list)
        if not any(isinstance(item, dict) and item.get("role") == "tool" for item in messages):
            return httpx2.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "health-1",
                                        "type": "function",
                                        "function": {
                                            "name": "get_system_health",
                                            "arguments": "{}",
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                },
            )
        return httpx2.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": "系统健康信息已读取。"}}]
            },
        )

    app = create_app(settings=_settings(tmp_path))
    with TestClient(app, base_url="https://testserver") as client:
        headers = _login(client)
        service = _save_test_enable_ai(
            client,
            app,
            headers,
            httpx2.MockTransport(handler),
        )
        agent = AIReadOnlyAgent(service, app.state.ai_tool_service)
        answer = await agent.respond(history=(), user_message="系统现在怎么样？")

    assert answer.content == "系统健康信息已读取。"
    assert answer.tool_summary == ("get_system_health",)
    assert len(chat_requests) == 2
    assert _API_KEY not in json.dumps(chat_requests, ensure_ascii=False)


@pytest.mark.asyncio
async def test_redacted_log_tool_never_sends_raw_secret_canary_to_provider(
    tmp_path: Path,
) -> None:
    canary = "PACKBREAKER-AI-CONTEXT-SECRET-CANARY-91d4"
    marker = "ai-context-redaction-probe"
    chat_requests: list[dict[str, object]] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        if request.method == "GET":
            return httpx2.Response(200, json={"id": "model-a", "object": "model"})
        payload = json.loads(request.content)
        chat_requests.append(payload)
        messages = payload["messages"]
        assert isinstance(messages, list)
        if not any(isinstance(item, dict) and item.get("role") == "tool" for item in messages):
            return httpx2.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "logs-1",
                                        "type": "function",
                                        "function": {
                                            "name": "query_redacted_logs",
                                            "arguments": json.dumps(
                                                {
                                                    "window_minutes": 60,
                                                    "limit": 10,
                                                    "query": marker,
                                                }
                                            ),
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                },
            )
        return httpx2.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "日志已安全读取。"}}]},
        )

    app = create_app(settings=_settings(tmp_path))
    with TestClient(app, base_url="https://testserver") as client:
        headers = _login(client)
        log_path = app.state.runtime.settings.log_dir / "packbreaker.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                        "level": "ERROR",
                        "logger": "packbreaker.ai.context-canary",
                        "message": f"token={canary} marker={marker}",
                        "fields": {"password": canary, "trace_id": marker},
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
        service = _save_test_enable_ai(
            client,
            app,
            headers,
            httpx2.MockTransport(handler),
            data_scopes=["REDACTED_LOGS"],
        )
        agent = AIReadOnlyAgent(service, app.state.ai_tool_service)
        answer = await agent.respond(history=(), user_message=f"检查 {marker} 日志")

    assert answer.content == "日志已安全读取。"
    assert answer.tool_summary == ("query_redacted_logs",)
    assert len(chat_requests) == 2
    provider_context = json.dumps(chat_requests, ensure_ascii=False)
    assert marker in provider_context
    assert "[REDACTED]" in provider_context
    assert canary not in provider_context
    assert _API_KEY not in provider_context


@pytest.mark.asyncio
async def test_read_only_agent_rejects_provider_request_for_unknown_tool(tmp_path: Path) -> None:
    async def handler(request: httpx2.Request) -> httpx2.Response:
        if request.method == "GET":
            return httpx2.Response(200, json={"id": "model-a", "object": "model"})
        return httpx2.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "bad-1",
                                    "type": "function",
                                    "function": {"name": "run_shell", "arguments": "{}"},
                                }
                            ],
                        }
                    }
                ]
            },
        )

    app = create_app(settings=_settings(tmp_path))
    with TestClient(app, base_url="https://testserver") as client:
        headers = _login(client)
        service = _save_test_enable_ai(
            client,
            app,
            headers,
            httpx2.MockTransport(handler),
        )
        agent = AIReadOnlyAgent(service, app.state.ai_tool_service)
        with pytest.raises(ApplicationError) as rejected:
            await agent.respond(history=(), user_message="执行 shell")
    assert rejected.value.code == "AI_TOOL_UNAUTHORIZED"
