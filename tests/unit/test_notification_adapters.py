from __future__ import annotations

from urllib.parse import parse_qs

import httpx2
import pytest

from backend.app.domain.notification import (
    NotificationDeliveryError,
    NotificationMessage,
    NotificationSeverity,
    ServerChanCredential,
    TelegramCredential,
)
from backend.app.infrastructure.adapters.notifications import (
    ServerChanNotificationProvider,
    TelegramNotificationProvider,
)

_MESSAGE = NotificationMessage(
    title="任务完成",
    body="synthetic body",
    severity=NotificationSeverity.INFO,
    event_key="TASK_DONE",
    link="https://packbreaker.invalid/?task_id=task-1",
    repeat_count=2,
)


@pytest.mark.asyncio
async def test_telegram_send_uses_official_endpoint_and_sanitized_payload() -> None:
    token = "123456:synthetic-token"
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json={"ok": True, "result": {"message_id": 1}})

    provider = TelegramNotificationProvider(
        TelegramCredential(token, "-100123"),
        transport=httpx2.MockTransport(handler),
    )
    await provider.send(_MESSAGE)

    assert len(seen) == 1
    assert seen[0].url.host == "api.telegram.org"
    assert seen[0].url.path == f"/bot{token}/sendMessage"
    assert "synthetic body" in seen[0].content.decode()
    assert "-100123" in seen[0].content.decode()


@pytest.mark.asyncio
async def test_telegram_failure_never_echoes_token() -> None:
    token = "123456:notification-secret-canary"

    def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(401, json={"ok": False, "description": token})

    provider = TelegramNotificationProvider(
        TelegramCredential(token, "123"),
        transport=httpx2.MockTransport(handler),
    )
    with pytest.raises(NotificationDeliveryError) as failure:
        await provider.send(_MESSAGE)

    assert failure.value.code == "NOTIFICATION_AUTH_FAILED"
    assert token not in str(failure.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("send_key", "expected_host"),
    [
        ("SCTsynthetickey", "sctapi.ftqq.com"),
        ("sctp123tSyntheticKey", "123.push.ft07.com"),
    ],
)
async def test_serverchan_selects_endpoint_by_sendkey_prefix(
    send_key: str,
    expected_host: str,
) -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json={"code": 0, "message": "success"})

    provider = ServerChanNotificationProvider(
        ServerChanCredential(send_key),
        transport=httpx2.MockTransport(handler),
    )
    await provider.send(_MESSAGE)

    assert seen[0].url.host == expected_host
    assert seen[0].url.path.endswith(f"/{send_key}.send")
    form = parse_qs(seen[0].content.decode())
    assert form["title"] == ["任务完成"]
    assert "聚合事件次数：2" in form["desp"][0]


@pytest.mark.asyncio
async def test_serverchan_rejects_unknown_key_without_network_request() -> None:
    calls = 0

    def handler(_request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        return httpx2.Response(200, json={"code": 0})

    provider = ServerChanNotificationProvider(
        ServerChanCredential("bad-key"),
        transport=httpx2.MockTransport(handler),
    )
    with pytest.raises(NotificationDeliveryError) as failure:
        await provider.send(_MESSAGE)

    assert failure.value.code == "NOTIFICATION_CREDENTIAL_INVALID"
    assert calls == 0
