from __future__ import annotations

import json

import httpx2
import pytest

from backend.app.infrastructure.adapters.telegram_ai import TelegramAIClient, TelegramAIError


@pytest.mark.asyncio
async def test_telegram_ai_client_polls_and_parses_text_messages() -> None:
    payloads: list[dict[str, object]] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        payloads.append(json.loads(request.content))
        return httpx2.Response(
            200,
            json={
                "ok": True,
                "result": [
                    {
                        "update_id": 42,
                        "message": {
                            "message_id": 7,
                            "chat": {"id": -100123},
                            "from": {"id": 9988},
                            "text": "系统状态？",
                        },
                    },
                    {"update_id": 43, "message": {"message_id": 8, "chat": {"id": 1}}},
                ],
            },
        )

    client = TelegramAIClient(
        "123:synthetic-token",
        transport=httpx2.MockTransport(handler),
    )
    updates = await client.poll(offset=42, timeout_seconds=5, limit=10)

    assert [item.update_id for item in updates] == [42, 43]
    assert updates[0].chat_id == "-100123"
    assert updates[0].user_id == "9988"
    assert updates[0].text == "系统状态？"
    assert updates[1].text is None
    assert payloads[0]["offset"] == 42
    assert payloads[0]["allowed_updates"] == ["message"]


@pytest.mark.asyncio
async def test_telegram_ai_client_does_not_follow_redirects() -> None:
    calls = 0

    async def handler(_request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        return httpx2.Response(302, headers={"Location": "https://attacker.example/steal"})

    client = TelegramAIClient(
        "123:synthetic-token",
        transport=httpx2.MockTransport(handler),
    )
    with pytest.raises(TelegramAIError, match="AI_TELEGRAM_REDIRECT_REJECTED"):
        await client.poll(offset=1, timeout_seconds=5, limit=10)
    assert calls == 1
