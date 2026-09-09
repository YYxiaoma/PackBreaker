import json

import httpx2
import pytest

from backend.app.domain.downloader import DownloaderCredential
from backend.app.infrastructure.adapters.downloaders import (
    DownloaderAdapterError,
    QbittorrentAdapter,
    TransmissionAdapter,
)


@pytest.mark.asyncio
async def test_qbittorrent_probe_logs_in_and_reads_versions() -> None:
    paths: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/auth/login"):
            return httpx2.Response(200, text="Ok.", headers={"Set-Cookie": "SID=fake; path=/"})
        if request.url.path.endswith("/app/version"):
            return httpx2.Response(200, text="v5.2.1")
        if request.url.path.endswith("/app/webapiVersion"):
            return httpx2.Response(200, text="2.15.1")
        return httpx2.Response(404)

    adapter = QbittorrentAdapter(
        "http://qb.invalid:8080",
        DownloaderCredential(username="admin", password="synthetic-password"),
        transport=httpx2.MockTransport(handler),
    )

    result = await adapter.test_connection()

    assert paths == [
        "/api/v2/auth/login",
        "/api/v2/app/version",
        "/api/v2/app/webapiVersion",
    ]
    assert result.capabilities.client == "qBittorrent"
    assert result.capabilities.version == "v5.2.1"
    assert result.capabilities.api_version == "2.15.1"
    assert result.capabilities.supports_skip_checking is True


@pytest.mark.asyncio
async def test_qbittorrent_api_key_probe_never_calls_auth_endpoint() -> None:
    seen_authorization: list[str | None] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen_authorization.append(request.headers.get("Authorization"))
        if request.url.path.endswith("/app/version"):
            return httpx2.Response(200, text="v5.2.0")
        return httpx2.Response(200, text="2.14.1")

    adapter = QbittorrentAdapter(
        "https://qb.invalid",
        DownloaderCredential(api_key="qbt_synthetic_key"),
        transport=httpx2.MockTransport(handler),
    )

    await adapter.test_connection()

    assert seen_authorization == ["Bearer qbt_synthetic_key", "Bearer qbt_synthetic_key"]


@pytest.mark.asyncio
async def test_transmission_probe_performs_session_id_handshake() -> None:
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx2.Response(
                409,
                headers={
                    "X-Transmission-Session-Id": "synthetic-session",
                    "X-Transmission-Rpc-Version": "6.0.0",
                },
            )
        assert request.headers["X-Transmission-Session-Id"] == "synthetic-session"
        body = json.loads(request.content.decode())
        assert body["method"] == "session-get"
        return httpx2.Response(
            200,
            json={
                "result": "success",
                "arguments": {"version": "4.1.0", "rpc-version-semver": "6.0.0"},
                "tag": 1,
            },
        )

    adapter = TransmissionAdapter(
        "http://tr.invalid:9091/transmission/rpc",
        DownloaderCredential(username="rpc", password="synthetic-password"),
        transport=httpx2.MockTransport(handler),
    )

    result = await adapter.test_connection()

    assert len(requests) == 2
    assert result.capabilities.client == "Transmission"
    assert result.capabilities.version == "4.1.0"
    assert result.capabilities.api_version == "6.0.0"
    assert result.capabilities.supports_skip_checking is False


@pytest.mark.asyncio
async def test_adapter_errors_do_not_echo_credentials() -> None:
    canary = "PACKBREAKER-DOWNLOADER-CANARY-91f3"

    def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, text="Fails.")

    adapter = QbittorrentAdapter(
        "http://qb.invalid",
        DownloaderCredential(username="admin", password=canary),
        transport=httpx2.MockTransport(handler),
    )

    with pytest.raises(DownloaderAdapterError) as failure:
        await adapter.test_connection()

    assert failure.value.code == "DOWNLOADER_AUTH_FAILED"
    assert canary not in str(failure.value)
