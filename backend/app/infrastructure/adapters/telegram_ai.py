from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx2


@dataclass(frozen=True, slots=True)
class TelegramInboundUpdate:
    update_id: int
    message_id: str | None
    chat_id: str | None
    user_id: str | None
    text: str | None


class TelegramAIError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class TelegramAIClientFactory:
    def __init__(self, *, transport: httpx2.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    def create(self, *, bot_token: str, proxy_url: str | None) -> TelegramAIClient:
        return TelegramAIClient(
            bot_token,
            proxy_url=proxy_url,
            transport=self._transport,
        )


class TelegramAIClient:
    def __init__(
        self,
        bot_token: str,
        *,
        proxy_url: str | None = None,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        token = bot_token.strip()
        if not token or len(token) > 256 or any(char in token for char in ("\r", "\n", "\x00")):
            raise ValueError("Telegram Bot Token 格式无效")
        self._base_url = f"https://api.telegram.org/bot{token}"
        self._proxy_url = proxy_url
        self._transport = transport

    async def poll(
        self,
        *,
        offset: int,
        timeout_seconds: int = 20,
        limit: int = 20,
    ) -> tuple[TelegramInboundUpdate, ...]:
        if offset < 1 or not 1 <= timeout_seconds <= 50 or not 1 <= limit <= 100:
            raise ValueError("Telegram Long Polling 参数无效")
        response = await self._request(
            "POST",
            "/getUpdates",
            timeout=float(timeout_seconds + 10),
            json={
                "offset": offset,
                "timeout": timeout_seconds,
                "limit": limit,
                "allowed_updates": ["message"],
            },
        )
        payload = self._telegram_payload(response)
        result = payload.get("result")
        if not isinstance(result, list):
            raise TelegramAIError("AI_TELEGRAM_INVALID_RESPONSE", retryable=True)
        updates: list[TelegramInboundUpdate] = []
        for item in result:
            parsed = self._parse_update(item)
            if parsed is not None and parsed.update_id >= offset:
                updates.append(parsed)
        updates.sort(key=lambda item: item.update_id)
        return tuple(updates)

    async def send_message(self, *, chat_id: str, text: str) -> None:
        normalized_chat = chat_id.strip()
        normalized_text = text.strip()
        if not normalized_chat or len(normalized_chat) > 64:
            raise ValueError("Telegram Chat ID 格式无效")
        if not normalized_text:
            raise ValueError("Telegram 回复不能为空")
        response = await self._request(
            "POST",
            "/sendMessage",
            timeout=15.0,
            json={
                "chat_id": normalized_chat,
                "text": normalized_text[:4096],
                "disable_web_page_preview": True,
            },
        )
        self._telegram_payload(response)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        timeout: float,
        json: dict[str, object],
    ) -> httpx2.Response:
        try:
            async with httpx2.AsyncClient(
                timeout=timeout,
                follow_redirects=False,
                transport=self._transport,
                proxy=self._proxy_url,
            ) as client:
                response = await client.request(
                    method,
                    f"{self._base_url}{path}",
                    headers={"Accept": "application/json", "Content-Type": "application/json"},
                    json=json,
                )
        except (httpx2.TimeoutException, httpx2.NetworkError) as exc:
            raise TelegramAIError("AI_TELEGRAM_UNAVAILABLE", retryable=True) from exc
        except httpx2.HTTPError as exc:
            raise TelegramAIError("AI_TELEGRAM_HTTP_ERROR", retryable=True) from exc
        if len(response.content) > 2 * 1024 * 1024:
            raise TelegramAIError("AI_TELEGRAM_RESPONSE_TOO_LARGE", retryable=False)
        if 300 <= response.status_code < 400:
            raise TelegramAIError("AI_TELEGRAM_REDIRECT_REJECTED", retryable=False)
        if response.status_code in {401, 403}:
            raise TelegramAIError("AI_TELEGRAM_AUTH_FAILED", retryable=False)
        if response.status_code == 429 or response.status_code >= 500:
            raise TelegramAIError("AI_TELEGRAM_TEMPORARY_FAILURE", retryable=True)
        if not 200 <= response.status_code < 300:
            raise TelegramAIError("AI_TELEGRAM_REJECTED", retryable=False)
        return response

    @staticmethod
    def _telegram_payload(response: httpx2.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise TelegramAIError("AI_TELEGRAM_INVALID_RESPONSE", retryable=True) from exc
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise TelegramAIError("AI_TELEGRAM_REJECTED", retryable=False)
        return payload

    @staticmethod
    def _parse_update(value: object) -> TelegramInboundUpdate | None:
        if not isinstance(value, dict):
            return None
        update_id = value.get("update_id")
        if isinstance(update_id, bool) or not isinstance(update_id, int) or update_id < 0:
            return None
        message = value.get("message")
        if not isinstance(message, dict):
            return TelegramInboundUpdate(update_id, None, None, None, None)
        message_id = message.get("message_id")
        chat = message.get("chat")
        sender = message.get("from")
        raw_text = message.get("text")
        chat_id = TelegramAIClient._numeric_identifier(
            chat.get("id") if isinstance(chat, dict) else None
        )
        user_id = TelegramAIClient._numeric_identifier(
            sender.get("id") if isinstance(sender, dict) else None
        )
        return TelegramInboundUpdate(
            update_id=update_id,
            message_id=(
                str(message_id)
                if isinstance(message_id, int) and not isinstance(message_id, bool)
                else None
            ),
            chat_id=chat_id,
            user_id=user_id,
            text=raw_text if isinstance(raw_text, str) else None,
        )

    @staticmethod
    def _numeric_identifier(value: object) -> str | None:
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return str(value)
