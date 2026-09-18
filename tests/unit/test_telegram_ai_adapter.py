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
                    {
                        "update_id": 44,
                        "callback_query": {
                            "id": "callback-44",
                            "from": {"id": 9988},
                            "message": {
                                "message_id": 9,
                                "chat": {"id": -100123},
                            },
                            "data": "pb1:A:11111111-1111-1111-1111-111111111111:aaaaaaaa",
                        },
                    },
                ],
            },
        )

    client = TelegramAIClient(
        "123:synthetic-token",
        transport=httpx2.MockTransport(handler),
    )
    updates = await client.poll(offset=42, timeout_seconds=5, limit=10)

    assert [item.update_id for item in updates] == [42, 43, 44]
    assert updates[0].chat_id == "-100123"
    assert updates[0].user_id == "9988"
    assert updates[0].text == "系统状态？"
    assert updates[1].text is None
    assert updates[2].callback_query_id == "callback-44"
    assert updates[2].callback_data == "pb1:A:11111111-1111-1111-1111-111111111111:aaaaaaaa"
    assert updates[2].chat_id == "-100123"
    assert updates[2].user_id == "9988"
    assert updates[2].message_id == "9"
    assert payloads[0]["offset"] == 42
    assert payloads[0]["allowed_updates"] == ["message", "callback_query"]


@pytest.mark.asyncio
async def test_telegram_ai_client_sends_inline_keyboard_and_answers_callback() -> None:
    requests: list[tuple[str, dict[str, object]]] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        requests.append((request.url.path, body))
        if request.url.path.endswith("/sendMessage"):
            return httpx2.Response(
                200,
                json={
                    "ok": True,
                    "result": {"message_id": 99, "chat": {"id": -100123}},
                },
            )
        return httpx2.Response(200, json={"ok": True, "result": True})

    client = TelegramAIClient(
        "123:synthetic-token",
        transport=httpx2.MockTransport(handler),
    )
    sent = await client.send_message(
        chat_id="-100123",
        text="审批请求",
        reply_markup={
            "inline_keyboard": [
                [{"text": "批准", "callback_data": "pb1:A:synthetic"}],
            ]
        },
    )
    await client.answer_callback_query(
        callback_query_id="callback-1",
        text="已批准",
    )
    await client.clear_inline_keyboard(chat_id="-100123", message_id="99")

    assert sent.chat_id == "-100123"
    assert sent.message_id == "99"
    assert requests[0][0].endswith("/sendMessage")
    assert requests[0][1]["reply_markup"] == {
        "inline_keyboard": [[{"text": "批准", "callback_data": "pb1:A:synthetic"}]]
    }
    assert requests[1] == (
        "/bot123:synthetic-token/answerCallbackQuery",
        {
            "callback_query_id": "callback-1",
            "text": "已批准",
            "show_alert": False,
        },
    )
    assert requests[2] == (
        "/bot123:synthetic-token/editMessageReplyMarkup",
        {
            "chat_id": "-100123",
            "message_id": 99,
            "reply_markup": {"inline_keyboard": []},
        },
    )


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
