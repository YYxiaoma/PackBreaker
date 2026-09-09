from __future__ import annotations

from typing import Protocol, cast
from urllib.parse import urlsplit

import httpx2

from backend.app.domain.downloader import (
    ConnectionTestResult,
    DownloaderCapabilities,
    DownloaderCredential,
)
from backend.app.domain.verification import DownloaderKind


class DownloaderAdapterError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class DownloaderProbeAdapter(Protocol):
    async def test_connection(self) -> ConnectionTestResult: ...


class DownloaderAdapterFactory:
    def __init__(self, *, transport: httpx2.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    def create(
        self,
        *,
        kind: DownloaderKind,
        base_url: str,
        credential: DownloaderCredential | None,
    ) -> DownloaderProbeAdapter:
        if kind is DownloaderKind.QBITTORRENT:
            return QbittorrentAdapter(base_url, credential, transport=self._transport)
        return TransmissionAdapter(base_url, credential, transport=self._transport)


class QbittorrentAdapter:
    def __init__(
        self,
        base_url: str,
        credential: DownloaderCredential | None,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._credential = credential
        self._transport = transport

    async def test_connection(self) -> ConnectionTestResult:
        headers: dict[str, str] = {}
        if self._credential is not None and self._credential.api_key:
            headers["Authorization"] = f"Bearer {self._credential.api_key}"
        try:
            async with httpx2.AsyncClient(
                headers=headers,
                timeout=10.0,
                follow_redirects=False,
                transport=self._transport,
            ) as client:
                if self._credential is not None and self._credential.username:
                    parsed = urlsplit(self._base_url)
                    origin = f"{parsed.scheme}://{parsed.netloc}"
                    login = await client.post(
                        f"{self._base_url}/api/v2/auth/login",
                        data={
                            "username": self._credential.username,
                            "password": self._credential.password or "",
                        },
                        headers={"Origin": origin, "Referer": f"{origin}/"},
                    )
                    if login.status_code in {401, 403} or not login.text.strip().startswith("Ok."):
                        raise DownloaderAdapterError(
                            "DOWNLOADER_AUTH_FAILED", "qBittorrent 认证失败"
                        )
                version_response = await client.get(f"{self._base_url}/api/v2/app/version")
                webapi_response = await client.get(f"{self._base_url}/api/v2/app/webapiVersion")
                self._raise_for_probe_status(version_response)
                self._raise_for_probe_status(webapi_response)
                version = self._validated_text(version_response.text, "qBittorrent 版本")
                webapi_version = self._validated_text(webapi_response.text, "WebAPI 版本")
        except DownloaderAdapterError:
            raise
        except (httpx2.TimeoutException, httpx2.NetworkError) as exc:
            raise DownloaderAdapterError(
                "DOWNLOADER_UNAVAILABLE", "qBittorrent 连接失败或超时"
            ) from exc
        except httpx2.HTTPError as exc:
            raise DownloaderAdapterError(
                "DOWNLOADER_CONNECTION_FAILED", "qBittorrent HTTP 探测失败"
            ) from exc
        return ConnectionTestResult(
            DownloaderCapabilities(
                client="qBittorrent",
                version=version,
                api_version=webapi_version,
                supports_skip_checking=True,
            )
        )

    @staticmethod
    def _raise_for_probe_status(response: httpx2.Response) -> None:
        if response.status_code in {401, 403}:
            raise DownloaderAdapterError("DOWNLOADER_AUTH_FAILED", "qBittorrent 认证失败")
        if response.status_code != 200:
            raise DownloaderAdapterError(
                "DOWNLOADER_CONNECTION_FAILED", "qBittorrent API 返回异常状态"
            )

    @staticmethod
    def _validated_text(value: str, label: str) -> str:
        normalized = value.strip()
        if not normalized or len(normalized) > 128:
            raise DownloaderAdapterError("DOWNLOADER_INVALID_RESPONSE", f"{label}响应格式无效")
        return normalized


class TransmissionAdapter:
    def __init__(
        self,
        base_url: str,
        credential: DownloaderCredential | None,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url
        self._credential = credential
        self._transport = transport

    async def test_connection(self) -> ConnectionTestResult:
        auth: httpx2.Auth | None = None
        if self._credential is not None and self._credential.username:
            auth = httpx2.BasicAuth(
                self._credential.username,
                self._credential.password or "",
            )
        payload = {"method": "session-get", "tag": 1}
        try:
            async with httpx2.AsyncClient(
                auth=auth,
                timeout=10.0,
                follow_redirects=False,
                transport=self._transport,
            ) as client:
                response = await client.post(self._base_url, json=payload)
                rpc_header = response.headers.get("X-Transmission-Rpc-Version")
                if response.status_code == 409:
                    session_id = response.headers.get("X-Transmission-Session-Id")
                    if not session_id:
                        raise DownloaderAdapterError(
                            "DOWNLOADER_INVALID_RESPONSE",
                            "Transmission 缺少 RPC session id",
                        )
                    response = await client.post(
                        self._base_url,
                        json=payload,
                        headers={"X-Transmission-Session-Id": session_id},
                    )
                if response.status_code in {401, 403}:
                    raise DownloaderAdapterError("DOWNLOADER_AUTH_FAILED", "Transmission 认证失败")
                if response.status_code != 200:
                    raise DownloaderAdapterError(
                        "DOWNLOADER_CONNECTION_FAILED", "Transmission RPC 返回异常状态"
                    )
                body = cast(dict[str, object], response.json())
                if body.get("result") != "success":
                    raise DownloaderAdapterError(
                        "DOWNLOADER_INVALID_RESPONSE", "Transmission RPC 返回失败结果"
                    )
                arguments = body.get("arguments")
                if not isinstance(arguments, dict):
                    raise DownloaderAdapterError(
                        "DOWNLOADER_INVALID_RESPONSE", "Transmission RPC 响应格式无效"
                    )
                version = arguments.get("version")
                if not isinstance(version, str) or not version or len(version) > 128:
                    raise DownloaderAdapterError(
                        "DOWNLOADER_INVALID_RESPONSE", "Transmission 版本响应无效"
                    )
                rpc_version = arguments.get("rpc-version-semver")
                if rpc_version is None:
                    rpc_version = rpc_header
                if rpc_version is not None and not isinstance(rpc_version, str):
                    rpc_version = str(rpc_version)
        except DownloaderAdapterError:
            raise
        except (httpx2.TimeoutException, httpx2.NetworkError) as exc:
            raise DownloaderAdapterError(
                "DOWNLOADER_UNAVAILABLE", "Transmission 连接失败或超时"
            ) from exc
        except (httpx2.HTTPError, ValueError, TypeError) as exc:
            raise DownloaderAdapterError(
                "DOWNLOADER_INVALID_RESPONSE", "Transmission RPC 响应无法解析"
            ) from exc
        return ConnectionTestResult(
            DownloaderCapabilities(
                client="Transmission",
                version=version,
                api_version=rpc_version,
                supports_skip_checking=False,
            )
        )
