import json

import httpx2
import pytest

from backend.app.domain.downloader import DownloaderCredential
from backend.app.domain.verification import VerificationLevel
from backend.app.infrastructure.adapters.downloaders import (
    DownloaderAdapterError,
    QbittorrentAdapter,
    QbittorrentAddRequest,
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


@pytest.mark.asyncio
async def test_qbittorrent_5215_add_uses_paused_multipart_and_reads_actual_state() -> None:
    torrent_hash = "a" * 40
    seen_paths: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen_paths.append(request.url.path)
        if request.url.path.endswith("/auth/login"):
            return httpx2.Response(200, text="Ok.", headers={"Set-Cookie": "QBT_SID=fake"})
        if request.url.path.endswith("/app/webapiVersion"):
            return httpx2.Response(200, text="2.15.1")
        if request.url.path.endswith("/torrents/add"):
            body = request.content
            assert b'name="savepath"' in body and b"/downloads/seed" in body
            assert b'name="paused"' in body and b"true" in body
            assert b'name="skip_checking"' in body and b"false" in body
            assert b'name="tags"' in body and b"packbreaker-test" in body
            assert b"application/x-bittorrent" in body
            return httpx2.Response(
                200,
                json={
                    "success_count": 1,
                    "pending_count": 0,
                    "failure_count": 0,
                    "added_torrent_ids": [torrent_hash],
                },
            )
        if request.url.path.endswith("/torrents/info"):
            assert request.url.params["hashes"] == torrent_hash
            return httpx2.Response(
                200,
                json=[
                    {
                        "hash": torrent_hash,
                        "save_path": "/downloads/seed/",
                        "content_path": "/downloads/seed/movie.mkv",
                        "state": "stoppedUP",
                        "tags": "packbreaker-test,media",
                    }
                ],
            )
        return httpx2.Response(404)

    adapter = QbittorrentAdapter(
        "http://qb.invalid:8080",
        DownloaderCredential(username="admin", password="synthetic-password"),
        transport=httpx2.MockTransport(handler),
    )
    result = await adapter.add_torrent(
        QbittorrentAddRequest(
            torrent_content=b"synthetic-torrent-payload",
            save_path="/downloads/seed",
            verification_level=VerificationLevel.CLIENT_CHECK_REQUIRED,
            tags=("packbreaker-test",),
        )
    )
    states = await adapter.get_torrents((torrent_hash,))

    assert result.webapi_version == "2.15.1"
    assert result.added_torrent_ids == (torrent_hash,)
    assert states[0].save_path == "/downloads/seed"
    assert states[0].stopped is True
    assert states[0].tags == ("packbreaker-test", "media")
    assert seen_paths.count("/api/v2/torrents/add") == 1


@pytest.mark.asyncio
async def test_qbittorrent_skip_checking_is_blocked_on_webapi_216() -> None:
    calls: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/app/webapiVersion"):
            return httpx2.Response(200, text="2.16.0")
        return httpx2.Response(500)

    adapter = QbittorrentAdapter(
        "http://qb.invalid",
        DownloaderCredential(api_key="qbt_synthetic_key"),
        transport=httpx2.MockTransport(handler),
    )
    with pytest.raises(DownloaderAdapterError) as failure:
        await adapter.add_torrent(
            QbittorrentAddRequest(
                torrent_content=b"synthetic-torrent-payload",
                save_path="/downloads/seed",
                verification_level=VerificationLevel.FULL_VERIFIED,
                skip_checking=True,
            )
        )

    assert failure.value.code == "DOWNLOADER_API_UNSUPPORTED"
    assert calls == ["/api/v2/app/webapiVersion"]


def test_qbittorrent_request_rejects_skip_checking_without_full_verification() -> None:
    with pytest.raises(ValueError, match="FULL_VERIFIED"):
        QbittorrentAddRequest(
            torrent_content=b"synthetic-torrent-payload",
            save_path="/downloads/seed",
            verification_level=VerificationLevel.CLIENT_CHECK_REQUIRED,
            skip_checking=True,
        )


@pytest.mark.asyncio
async def test_qbittorrent_5x_uses_stop_start_and_recheck_endpoints() -> None:
    torrent_hash = "b" * 40
    actions: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        actions.append(request.url.path)
        assert request.content == f"hashes={torrent_hash}".encode()
        return httpx2.Response(200)

    adapter = QbittorrentAdapter(
        "http://qb.invalid",
        DownloaderCredential(api_key="qbt_synthetic_key"),
        transport=httpx2.MockTransport(handler),
    )
    await adapter.stop_torrent(torrent_hash)
    await adapter.start_torrent(torrent_hash)
    await adapter.recheck_torrent(torrent_hash)

    assert actions == [
        "/api/v2/torrents/stop",
        "/api/v2/torrents/start",
        "/api/v2/torrents/recheck",
    ]
