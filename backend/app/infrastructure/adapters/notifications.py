from __future__ import annotations

import re

import httpx2

from backend.app.domain.notification import (
    DeliveryResult,
    NotificationChannelKind,
    NotificationCredential,
    NotificationDeliveryError,
    NotificationMessage,
    NotificationProvider,
    NotificationSeverity,
    NotificationTestResult,
    ServerChanCredential,
    TelegramCredential,
)

_SERVERCHAN_SC3_PATTERN = re.compile(r"^sctp(\d+)t")
_TEST_MESSAGE = NotificationMessage(
    title="PackBreaker 通知测试",
    body="通知渠道连接测试成功。",
    severity=NotificationSeverity.INFO,
    event_key="NOTIFICATION_TEST",
)


class NotificationProviderFactory:
    def __init__(self, *, transport: httpx2.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    def create(
        self,
        *,
        kind: NotificationChannelKind,
        credential: NotificationCredential,
    ) -> NotificationProvider:
        if kind is NotificationChannelKind.TELEGRAM:
            if not isinstance(credential, TelegramCredential):
                raise ValueError("Telegram 通知凭证类型不匹配")
            return TelegramNotificationProvider(credential, transport=self._transport)
        if kind is NotificationChannelKind.SERVERCHAN:
            if not isinstance(credential, ServerChanCredential):
                raise ValueError("Server酱通知凭证类型不匹配")
            return ServerChanNotificationProvider(credential, transport=self._transport)
        raise AssertionError(f"未处理的通知渠道类型: {kind.value}")


class TelegramNotificationProvider:
    def __init__(
        self,
        credential: TelegramCredential,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._token = credential.bot_token
        self._chat_id = credential.chat_id
        self._transport = transport

    async def test_connection(self) -> NotificationTestResult:
        await self.send(_TEST_MESSAGE)
        return NotificationTestResult(True, NotificationChannelKind.TELEGRAM)

    async def send(self, message: NotificationMessage) -> DeliveryResult:
        text = _render_text(message)
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        try:
            async with httpx2.AsyncClient(
                timeout=10.0,
                follow_redirects=False,
                transport=self._transport,
            ) as client:
                response = await client.post(
                    url,
                    json={
                        "chat_id": self._chat_id,
                        "text": text[:4096],
                        "disable_web_page_preview": True,
                    },
                )
        except (httpx2.TimeoutException, httpx2.NetworkError) as exc:
            raise NotificationDeliveryError("NOTIFICATION_UNAVAILABLE", retryable=True) from exc
        except httpx2.HTTPError as exc:
            raise NotificationDeliveryError("NOTIFICATION_HTTP_ERROR", retryable=True) from exc

        if response.status_code == 429 or response.status_code >= 500:
            raise NotificationDeliveryError("NOTIFICATION_TEMPORARY_FAILURE", retryable=True)
        if response.status_code in {401, 403}:
            raise NotificationDeliveryError("NOTIFICATION_AUTH_FAILED", retryable=False)
        if response.status_code != 200:
            raise NotificationDeliveryError("NOTIFICATION_REJECTED", retryable=False)
        try:
            payload = response.json()
        except ValueError as exc:
            raise NotificationDeliveryError(
                "NOTIFICATION_INVALID_RESPONSE", retryable=True
            ) from exc
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise NotificationDeliveryError("NOTIFICATION_REJECTED", retryable=False)
        return DeliveryResult(NotificationChannelKind.TELEGRAM)


class ServerChanNotificationProvider:
    def __init__(
        self,
        credential: ServerChanCredential,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._send_key = credential.send_key
        self._transport = transport

    async def test_connection(self) -> NotificationTestResult:
        await self.send(_TEST_MESSAGE)
        return NotificationTestResult(True, NotificationChannelKind.SERVERCHAN)

    async def send(self, message: NotificationMessage) -> DeliveryResult:
        url = _serverchan_url(self._send_key)
        try:
            async with httpx2.AsyncClient(
                timeout=10.0,
                follow_redirects=False,
                transport=self._transport,
            ) as client:
                response = await client.post(
                    url,
                    data={"title": message.title[:32], "desp": _render_text(message)},
                )
        except (httpx2.TimeoutException, httpx2.NetworkError) as exc:
            raise NotificationDeliveryError("NOTIFICATION_UNAVAILABLE", retryable=True) from exc
        except httpx2.HTTPError as exc:
            raise NotificationDeliveryError("NOTIFICATION_HTTP_ERROR", retryable=True) from exc

        if response.status_code == 429 or response.status_code >= 500:
            raise NotificationDeliveryError("NOTIFICATION_TEMPORARY_FAILURE", retryable=True)
        if response.status_code in {401, 403}:
            raise NotificationDeliveryError("NOTIFICATION_AUTH_FAILED", retryable=False)
        if response.status_code != 200:
            raise NotificationDeliveryError("NOTIFICATION_REJECTED", retryable=False)
        try:
            payload = response.json()
        except ValueError as exc:
            raise NotificationDeliveryError(
                "NOTIFICATION_INVALID_RESPONSE", retryable=True
            ) from exc
        if not isinstance(payload, dict) or payload.get("code") != 0:
            raise NotificationDeliveryError("NOTIFICATION_REJECTED", retryable=False)
        return DeliveryResult(NotificationChannelKind.SERVERCHAN)


def _render_text(message: NotificationMessage) -> str:
    parts = [message.title, message.body]
    if message.repeat_count > 1:
        parts.append(f"聚合事件次数：{message.repeat_count}")
    if message.link is not None:
        parts.append(f"任务链接：{message.link}")
    return "\n\n".join(parts)


def _serverchan_url(send_key: str) -> str:
    if send_key.startswith("SCT"):
        return f"https://sctapi.ftqq.com/{send_key}.send"
    match = _SERVERCHAN_SC3_PATTERN.match(send_key)
    if match is not None:
        return f"https://{match.group(1)}.push.ft07.com/send/{send_key}.send"
    raise NotificationDeliveryError("NOTIFICATION_CREDENTIAL_INVALID", retryable=False)
