from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx2

from backend.app.domain.ai_agent import AIProviderKind


@dataclass(frozen=True, slots=True)
class AIProviderProbeResult:
    provider_kind: AIProviderKind
    model: str


@dataclass(frozen=True, slots=True)
class AIProviderToolCall:
    id: str
    name: str
    arguments_json: str


@dataclass(frozen=True, slots=True)
class AIProviderChatTurn:
    content: str | None
    tool_calls: tuple[AIProviderToolCall, ...]


class AIProviderError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class AIProviderFactory:
    def __init__(self, *, transport: httpx2.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    def create(
        self,
        *,
        provider_kind: AIProviderKind,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float,
    ) -> OpenAICompatibleProvider:
        return OpenAICompatibleProvider(
            provider_kind=provider_kind,
            base_url=base_url,
            api_key=api_key,
            model=model,
            timeout_seconds=timeout_seconds,
            transport=self._transport,
        )


class OpenAICompatibleProvider:
    """OpenAI REST 兼容边界；API Key 不自动跨重定向发送。"""

    def __init__(
        self,
        *,
        provider_kind: AIProviderKind,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("AI Provider API Key 不能为空")
        if timeout_seconds <= 0:
            raise ValueError("AI Provider timeout 必须为正数")
        self._provider_kind = provider_kind
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    async def probe(self) -> AIProviderProbeResult:
        encoded_model = quote(self._model, safe="")
        response = await self._get(f"/models/{encoded_model}")
        if response.status_code in {404, 405}:
            response = await self._get("/models")
            self._require_success(response)
            payload = self._json_object(response)
            data = payload.get("data")
            if not isinstance(data, list) or not any(
                isinstance(item, dict) and item.get("id") == self._model for item in data
            ):
                raise AIProviderError("AI_PROVIDER_MODEL_NOT_FOUND", retryable=False)
        else:
            self._require_success(response)
            payload = self._json_object(response)
            response_model = payload.get("id")
            if isinstance(response_model, str) and response_model != self._model:
                raise AIProviderError("AI_PROVIDER_MODEL_MISMATCH", retryable=False)
        return AIProviderProbeResult(self._provider_kind, self._model)

    async def chat(
        self,
        *,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]],
    ) -> AIProviderChatTurn:
        body: dict[str, object] = {"model": self._model, "messages": messages}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        response = await self._post("/chat/completions", json=body)
        self._require_success(response)
        payload = self._json_object(response)
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise AIProviderError("AI_PROVIDER_INVALID_RESPONSE", retryable=True)
        message = choices[0].get("message")
        if not isinstance(message, dict):
            raise AIProviderError("AI_PROVIDER_INVALID_RESPONSE", retryable=True)
        raw_content = message.get("content")
        content = raw_content if isinstance(raw_content, str) else None
        raw_calls = message.get("tool_calls", [])
        if not isinstance(raw_calls, list):
            raise AIProviderError("AI_PROVIDER_INVALID_RESPONSE", retryable=True)
        calls: list[AIProviderToolCall] = []
        for raw_call in raw_calls:
            if not isinstance(raw_call, dict):
                raise AIProviderError("AI_PROVIDER_INVALID_RESPONSE", retryable=True)
            call_id = raw_call.get("id")
            function = raw_call.get("function")
            if not isinstance(call_id, str) or not isinstance(function, dict):
                raise AIProviderError("AI_PROVIDER_INVALID_RESPONSE", retryable=True)
            name = function.get("name")
            arguments = function.get("arguments")
            if not isinstance(name, str) or not isinstance(arguments, str):
                raise AIProviderError("AI_PROVIDER_INVALID_RESPONSE", retryable=True)
            if not call_id or len(call_id) > 256 or not name or len(name) > 128:
                raise AIProviderError("AI_PROVIDER_INVALID_RESPONSE", retryable=True)
            if len(arguments) > 16_384:
                raise AIProviderError("AI_PROVIDER_TOOL_ARGUMENTS_TOO_LARGE", retryable=False)
            calls.append(AIProviderToolCall(call_id, name, arguments))
        if content is None and not calls:
            raise AIProviderError("AI_PROVIDER_INVALID_RESPONSE", retryable=True)
        return AIProviderChatTurn(content=content, tool_calls=tuple(calls))

    async def _get(self, path: str) -> httpx2.Response:
        return await self._request("GET", path)

    async def _post(self, path: str, *, json: dict[str, object]) -> httpx2.Response:
        return await self._request("POST", path, json=json)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, object] | None = None,
    ) -> httpx2.Response:
        try:
            async with httpx2.AsyncClient(
                timeout=self._timeout_seconds,
                follow_redirects=False,
                transport=self._transport,
            ) as client:
                response = await client.request(
                    method,
                    f"{self._base_url}{path}",
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                    },
                    json=json,
                )
                if len(response.content) > 2 * 1024 * 1024:
                    raise AIProviderError("AI_PROVIDER_RESPONSE_TOO_LARGE", retryable=False)
                return response
        except (httpx2.TimeoutException, httpx2.NetworkError) as exc:
            raise AIProviderError("AI_PROVIDER_UNAVAILABLE", retryable=True) from exc
        except httpx2.HTTPError as exc:
            raise AIProviderError("AI_PROVIDER_HTTP_ERROR", retryable=True) from exc

    @staticmethod
    def _require_success(response: httpx2.Response) -> None:
        if 300 <= response.status_code < 400:
            raise AIProviderError("AI_PROVIDER_REDIRECT_REJECTED", retryable=False)
        if response.status_code in {401, 403}:
            raise AIProviderError("AI_PROVIDER_AUTH_FAILED", retryable=False)
        if response.status_code == 404:
            raise AIProviderError("AI_PROVIDER_MODEL_NOT_FOUND", retryable=False)
        if response.status_code == 429 or response.status_code >= 500:
            raise AIProviderError("AI_PROVIDER_TEMPORARY_FAILURE", retryable=True)
        if not 200 <= response.status_code < 300:
            raise AIProviderError("AI_PROVIDER_REJECTED", retryable=False)

    @staticmethod
    def _json_object(response: httpx2.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise AIProviderError("AI_PROVIDER_INVALID_RESPONSE", retryable=True) from exc
        if not isinstance(payload, dict):
            raise AIProviderError("AI_PROVIDER_INVALID_RESPONSE", retryable=True)
        return payload
