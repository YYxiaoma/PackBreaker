from __future__ import annotations

import json

import httpx2
import pytest

from backend.app.domain.ai_agent import AIProviderKind
from backend.app.infrastructure.adapters.ai_provider import (
    AIProviderError,
    AIProviderFactory,
)


@pytest.mark.asyncio
async def test_ai_provider_probe_uses_bearer_and_does_not_follow_redirects() -> None:
    seen: list[httpx2.Request] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json={"id": "synthetic-model", "object": "model"})

    provider = AIProviderFactory(transport=httpx2.MockTransport(handler)).create(
        provider_kind=AIProviderKind.OPENAI_COMPATIBLE,
        base_url="https://provider.example/v1",
        api_key="secret-canary-key",
        model="synthetic-model",
        timeout_seconds=5,
    )

    result = await provider.probe()

    assert result.model == "synthetic-model"
    assert len(seen) == 1
    assert seen[0].url == httpx2.URL("https://provider.example/v1/models/synthetic-model")
    assert seen[0].headers["authorization"] == "Bearer secret-canary-key"


@pytest.mark.asyncio
async def test_ai_provider_probe_rejects_redirect_without_following_target() -> None:
    calls = 0

    async def handler(_request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        return httpx2.Response(302, headers={"Location": "https://attacker.example/models"})

    provider = AIProviderFactory(transport=httpx2.MockTransport(handler)).create(
        provider_kind=AIProviderKind.OPENAI_COMPATIBLE,
        base_url="https://provider.example/v1",
        api_key="secret-canary-key",
        model="synthetic-model",
        timeout_seconds=5,
    )

    with pytest.raises(AIProviderError, match="AI_PROVIDER_REDIRECT_REJECTED"):
        await provider.probe()
    assert calls == 1


@pytest.mark.asyncio
async def test_ai_provider_probe_falls_back_to_model_list() -> None:
    paths: list[str] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/models/synthetic-model"):
            return httpx2.Response(404, json={"error": "unsupported retrieve"})
        return httpx2.Response(
            200,
            json={"object": "list", "data": [{"id": "synthetic-model", "object": "model"}]},
        )

    provider = AIProviderFactory(transport=httpx2.MockTransport(handler)).create(
        provider_kind=AIProviderKind.OPENAI_COMPATIBLE,
        base_url="https://provider.example/v1",
        api_key="secret-canary-key",
        model="synthetic-model",
        timeout_seconds=5,
    )

    await provider.probe()

    assert paths == ["/v1/models/synthetic-model", "/v1/models"]


@pytest.mark.asyncio
async def test_ai_provider_chat_parses_function_tool_call() -> None:
    captured: list[dict[str, object]] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        captured.append(json.loads(request.content))
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
                                    "id": "call-1",
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

    provider = AIProviderFactory(transport=httpx2.MockTransport(handler)).create(
        provider_kind=AIProviderKind.OPENAI_COMPATIBLE,
        base_url="https://provider.example/v1",
        api_key="secret-canary-key",
        model="synthetic-model",
        timeout_seconds=5,
    )
    turn = await provider.chat(
        messages=[{"role": "user", "content": "系统怎么样"}],
        tools=[{"type": "function", "function": {"name": "get_system_health"}}],
    )

    assert turn.content is None
    assert turn.tool_calls[0].name == "get_system_health"
    assert turn.tool_calls[0].arguments_json == "{}"
    assert captured[0]["model"] == "synthetic-model"
    assert captured[0]["tool_choice"] == "auto"
