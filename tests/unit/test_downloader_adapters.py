import json
from base64 import b64encode

import httpx2
import pytest

from backend.app.domain.downloader import DownloaderCredential
from backend.app.domain.verification import VerificationLevel
from backend.app.infrastructure.adapters.downloaders import (
    DownloaderAdapterError,
    QbittorrentAdapter,
    QbittorrentAddRequest,
    TransmissionAdapter,
    TransmissionAddRequest,
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
    assert result.capabilities.supports_force_recheck is True
    assert result.capabilities.supports_verify_progress is True
    assert result.capabilities.supports_selective_files is True


@pytest.mark.asyncio
async def test_qbittorrent_523_probe_accepts_204_session_cookie_login() -> None:
    paths: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/auth/login"):
            return httpx2.Response(
                204,
                headers={"Set-Cookie": "QBT_SID_synthetic=fake; path=/; HttpOnly"},
            )
        if request.url.path.endswith("/app/version"):
            assert "QBT_SID_synthetic=fake" in request.headers.get("Cookie", "")
            return httpx2.Response(200, text="v5.2.3")
        if request.url.path.endswith("/app/webapiVersion"):
            assert "QBT_SID_synthetic=fake" in request.headers.get("Cookie", "")
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
    assert result.capabilities.version == "v5.2.3"
    assert result.capabilities.api_version == "2.15.1"


@pytest.mark.asyncio
async def test_qbittorrent_probe_rejects_204_without_session_cookie() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path.endswith("/auth/login")
        return httpx2.Response(204)

    adapter = QbittorrentAdapter(
        "http://qb.invalid:8080",
        DownloaderCredential(username="admin", password="synthetic-password"),
        transport=httpx2.MockTransport(handler),
    )

    with pytest.raises(DownloaderAdapterError) as failure:
        await adapter.test_connection()

    assert failure.value.code == "DOWNLOADER_AUTH_FAILED"


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
async def test_qbittorrent_runtime_metrics_use_global_rates_and_all_torrent_sizes() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/auth/login"):
            return httpx2.Response(200, text="Ok.", headers={"Set-Cookie": "SID=fake; path=/"})
        if request.url.path.endswith("/transfer/info"):
            return httpx2.Response(200, json={"up_info_speed": 1024, "dl_info_speed": 2048})
        if request.url.path.endswith("/torrents/info"):
            return httpx2.Response(200, json=[{"size": 100}, {"size": 250}])
        if request.url.path.endswith("/sync/maindata"):
            assert request.url.params["rid"] == "0"
            return httpx2.Response(200, json={"server_state": {"free_space_on_disk": 4096}})
        return httpx2.Response(404)

    adapter = QbittorrentAdapter(
        "http://qb.invalid:8080",
        DownloaderCredential(username="admin", password="synthetic-password"),
        transport=httpx2.MockTransport(handler),
    )

    metrics = await adapter.runtime_metrics()

    assert metrics.upload_speed_bytes_per_second == 1024
    assert metrics.download_speed_bytes_per_second == 2048
    assert metrics.total_content_size_bytes == 350
    assert metrics.free_space_bytes == 4096
    assert metrics.active_torrent_count is None
    assert metrics.total_torrent_count == 2


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
        assert body == {
            "jsonrpc": "2.0",
            "method": "session_get",
            "params": {"fields": ["version", "rpc_version_semver"]},
            "id": 1,
        }
        return httpx2.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "result": {"version": "4.1.3", "rpc_version_semver": "6.0.0"},
                "id": 1,
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
    assert result.capabilities.version == "4.1.3"
    assert result.capabilities.api_version == "6.0.0"
    assert result.capabilities.supports_skip_checking is False
    assert result.capabilities.supports_force_recheck is True
    assert result.capabilities.supports_verify_progress is True
    assert result.capabilities.supports_selective_files is True


@pytest.mark.asyncio
async def test_transmission_runtime_metrics_use_41_json_rpc_fields() -> None:
    methods: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.headers.get("X-Transmission-Session-Id") != "synthetic-session":
            return httpx2.Response(409, headers={"X-Transmission-Session-Id": "synthetic-session"})
        body = json.loads(request.content.decode())
        method = body["method"]
        methods.append(method)
        result: dict[str, object]
        if method == "session_stats":
            result = {
                "upload_speed": 300,
                "download_speed": 400,
                "active_torrent_count": 2,
                "torrent_count": 3,
            }
        elif method == "session_get":
            assert body["params"] == {"fields": ["download_dir", "download_dir_free_space"]}
            result = {"download_dir": "/downloads", "download_dir_free_space": 4500}
        elif method == "torrent_get":
            assert body["params"] == {"fields": ["total_size"]}
            result = {"torrents": [{"total_size": 1000}, {"total_size": 2000}]}
        elif method == "free_space":
            assert body["params"] == {"path": "/downloads"}
            result = {"path": "/downloads", "size_bytes": 5000, "total_size": 9000}
        else:
            raise AssertionError(f"unexpected method {method}")
        return httpx2.Response(200, json={"jsonrpc": "2.0", "result": result, "id": 1})

    adapter = TransmissionAdapter(
        "http://tr.invalid:9091/transmission/rpc",
        DownloaderCredential(username="rpc", password="synthetic-password"),
        transport=httpx2.MockTransport(handler),
    )

    metrics = await adapter.runtime_metrics()

    assert methods == ["session_stats", "session_get", "torrent_get", "free_space"]
    assert metrics.upload_speed_bytes_per_second == 300
    assert metrics.download_speed_bytes_per_second == 400
    assert metrics.total_content_size_bytes == 3000
    assert metrics.free_space_bytes == 5000
    assert metrics.active_torrent_count == 2
    assert metrics.total_torrent_count == 3


@pytest.mark.asyncio
async def test_transmission_runtime_metrics_degrade_only_free_space_when_rpc_rejects_it() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.headers.get("X-Transmission-Session-Id") != "synthetic-session":
            return httpx2.Response(409, headers={"X-Transmission-Session-Id": "synthetic-session"})
        body = json.loads(request.content.decode())
        method = body["method"]
        result: dict[str, object]
        if method == "session_stats":
            result = {
                "upload_speed": 300,
                "download_speed": 400,
                "active_torrent_count": 2,
                "torrent_count": 3,
            }
        elif method == "session_get":
            result = {"download_dir": "/downloads", "download_dir_free_space": -1}
        elif method == "torrent_get":
            result = {"torrents": [{"total_size": 1000}, {"total_size": 2000}]}
        elif method == "free_space":
            return httpx2.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "error": {"code": -32000, "message": "free space unavailable"},
                    "id": 1,
                },
            )
        else:
            raise AssertionError(f"unexpected method {method}")
        return httpx2.Response(200, json={"jsonrpc": "2.0", "result": result, "id": 1})

    adapter = TransmissionAdapter(
        "http://tr.invalid:9091/transmission/rpc",
        DownloaderCredential(username="rpc", password="synthetic-password"),
        transport=httpx2.MockTransport(handler),
    )

    metrics = await adapter.runtime_metrics()

    assert metrics.upload_speed_bytes_per_second == 300
    assert metrics.download_speed_bytes_per_second == 400
    assert metrics.total_content_size_bytes == 3000
    assert metrics.free_space_bytes is None
    assert metrics.active_torrent_count == 2
    assert metrics.total_torrent_count == 3


@pytest.mark.asyncio
async def test_transmission_413_write_primitives_are_paused_and_keep_local_data() -> None:
    torrent_hash = "a" * 40
    payloads: list[dict[str, object]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.headers.get("X-Transmission-Session-Id") != "synthetic-session":
            return httpx2.Response(
                409,
                headers={
                    "X-Transmission-Session-Id": "synthetic-session",
                    "X-Transmission-Rpc-Version": "6.0.0",
                },
            )
        body = json.loads(request.content.decode())
        assert body["jsonrpc"] == "2.0"
        assert body["id"] == 1
        payloads.append(body)
        method = body["method"]
        if method == "torrent_add":
            params = body["params"]
            assert params == {
                "metainfo": b64encode(b"synthetic-torrent-payload").decode("ascii"),
                "download_dir": "/downloads/seed",
                "paused": True,
                "labels": ["packbreaker-test"],
            }
            result: dict[str, object] = {
                "torrent_added": {"id": 7, "name": "Synthetic", "hash_string": torrent_hash}
            }
        elif method == "torrent_get":
            assert body["params"] == {
                "ids": [torrent_hash],
                "fields": [
                    "hash_string",
                    "download_dir",
                    "status",
                    "labels",
                    "percent_done",
                    "recheck_progress",
                ],
            }
            result = {
                "torrents": [
                    {
                        "hash_string": torrent_hash,
                        "download_dir": "/downloads/seed/",
                        "status": 0,
                        "labels": ["packbreaker-test", "media"],
                        "percent_done": 1.0,
                        "recheck_progress": 0.0,
                    }
                ]
            }
        else:
            result = {}
        return httpx2.Response(
            200,
            json={"jsonrpc": "2.0", "result": result, "id": 1},
        )

    adapter = TransmissionAdapter(
        "http://tr.invalid:9091/transmission/rpc",
        DownloaderCredential(username="rpc", password="synthetic-password"),
        transport=httpx2.MockTransport(handler),
    )

    added = await adapter.add_torrent(
        TransmissionAddRequest(
            torrent_content=b"synthetic-torrent-payload",
            save_path="/downloads/seed",
            labels=("packbreaker-test",),
        )
    )
    states = await adapter.get_torrents((torrent_hash,))
    await adapter.verify_torrent(torrent_hash)
    await adapter.start_torrent(torrent_hash)
    await adapter.stop_torrent(torrent_hash)
    await adapter.remove_torrent_keep_files(torrent_hash)

    assert added.torrent_hash == torrent_hash
    assert added.duplicate is False
    assert states[0].download_dir == "/downloads/seed"
    assert states[0].stopped is True
    assert states[0].verification_complete is True
    assert states[0].labels == ("packbreaker-test", "media")
    assert [item["method"] for item in payloads] == [
        "torrent_add",
        "torrent_get",
        "torrent_verify",
        "torrent_start",
        "torrent_stop",
        "torrent_remove",
    ]
    remove = payloads[-1]
    assert remove["params"] == {"ids": [torrent_hash], "delete_local_data": False}


@pytest.mark.asyncio
async def test_transmission_duplicate_is_reported_without_implicit_ownership() -> None:
    torrent_hash = "b" * 40

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.headers.get("X-Transmission-Session-Id") != "synthetic-session":
            return httpx2.Response(
                409,
                headers={"X-Transmission-Session-Id": "synthetic-session"},
            )
        return httpx2.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "result": {
                    "torrent_duplicate": {
                        "id": 8,
                        "name": "Already there",
                        "hash_string": torrent_hash,
                    }
                },
                "id": 1,
            },
        )

    adapter = TransmissionAdapter(
        "http://tr.invalid:9091/transmission/rpc",
        None,
        transport=httpx2.MockTransport(handler),
    )

    result = await adapter.add_torrent(
        TransmissionAddRequest(
            torrent_content=b"synthetic-torrent-payload",
            save_path="/downloads/seed",
        )
    )

    assert result.torrent_hash == torrent_hash
    assert result.duplicate is True


def test_transmission_add_request_cannot_start_immediately() -> None:
    with pytest.raises(ValueError, match="暂停状态"):
        TransmissionAddRequest(
            torrent_content=b"synthetic-torrent-payload",
            save_path="/downloads/seed",
            paused=False,
        )


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
            assert b'name="stopped"' in body and b"true" in body
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
                        "progress": 1.0,
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
    assert states[0].progress == 1.0
    assert states[0].verification_complete is True
    assert states[0].tags == ("packbreaker-test", "media")
    assert seen_paths.count("/api/v2/torrents/add") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("progress", [None, -0.01, 1.01, True, "1.0"])
async def test_qbittorrent_info_rejects_invalid_progress(progress: object) -> None:
    torrent_hash = "c" * 40

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path.endswith("/torrents/info")
        return httpx2.Response(
            200,
            json=[
                {
                    "hash": torrent_hash,
                    "save_path": "/downloads/seed",
                    "content_path": None,
                    "state": "checkingUP",
                    "tags": "packbreaker-test",
                    "progress": progress,
                }
            ],
        )

    adapter = QbittorrentAdapter(
        "http://qb.invalid",
        DownloaderCredential(api_key="qbt_synthetic_key"),
        transport=httpx2.MockTransport(handler),
    )
    with pytest.raises(DownloaderAdapterError) as failure:
        await adapter.get_torrents((torrent_hash,))

    assert failure.value.code == "DOWNLOADER_INVALID_RESPONSE"


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


def test_qbittorrent_request_rejects_skip_checking_without_content_evidence() -> None:
    with pytest.raises(ValueError, match="内容证据等级"):
        QbittorrentAddRequest(
            torrent_content=b"synthetic-torrent-payload",
            save_path="/downloads/seed",
            verification_level=VerificationLevel.CLIENT_CHECK_REQUIRED,
            skip_checking=True,
        )


def test_qbittorrent_request_accepts_sampled_verification_for_skip_checking() -> None:
    """抽样证据与逐片全量同等放行；用户显式勾选即代表接受抽样边界。"""

    request = QbittorrentAddRequest(
        torrent_content=b"synthetic-torrent-payload",
        save_path="/downloads/seed",
        verification_level=VerificationLevel.SAMPLED_VERIFIED,
        skip_checking=True,
    )

    assert request.skip_checking is True


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


@pytest.mark.asyncio
async def test_qbittorrent_remove_always_keeps_data_files() -> None:
    torrent_hash = "d" * 40
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        assert request.url.path == "/api/v2/torrents/delete"
        assert request.content == f"hashes={torrent_hash}&deleteFiles=false".encode()
        return httpx2.Response(200)

    adapter = QbittorrentAdapter(
        "http://qb.invalid",
        DownloaderCredential(api_key="qbt_synthetic_key"),
        transport=httpx2.MockTransport(handler),
    )

    await adapter.remove_torrent_keep_files(torrent_hash)

    assert len(requests) == 1


@pytest.mark.asyncio
async def test_qbittorrent_file_selection_uses_file_prio_and_is_idempotent() -> None:
    torrent_hash = "e" * 40
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        assert request.url.path == "/api/v2/torrents/filePrio"
        return httpx2.Response(200)

    adapter = QbittorrentAdapter(
        "http://qb.invalid",
        DownloaderCredential(api_key="qbt_synthetic_key"),
        transport=httpx2.MockTransport(handler),
    )

    await adapter.set_file_selection(
        torrent_hash,
        wanted=(3, 1, 3),
        unwanted=(0, 2),
    )

    assert [request.content for request in requests] == [
        f"hash={torrent_hash}&id=0%7C2&priority=0".encode(),
        f"hash={torrent_hash}&id=1%7C3&priority=1".encode(),
    ]


@pytest.mark.asyncio
async def test_transmission_file_selection_uses_torrent_set() -> None:
    torrent_hash = "f" * 40
    bodies: list[dict[str, object]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.headers.get("X-Transmission-Session-Id") != "synthetic-session":
            return httpx2.Response(
                409,
                headers={"X-Transmission-Session-Id": "synthetic-session"},
            )
        body = json.loads(request.content.decode())
        bodies.append(body)
        return httpx2.Response(
            200,
            json={"jsonrpc": "2.0", "result": {}, "id": 1},
        )

    adapter = TransmissionAdapter(
        "http://tr.invalid:9091/transmission/rpc",
        None,
        transport=httpx2.MockTransport(handler),
    )

    await adapter.set_file_selection(
        torrent_hash,
        wanted=(4, 2, 4),
        unwanted=(0, 1),
    )

    assert bodies == [
        {
            "jsonrpc": "2.0",
            "method": "torrent_set",
            "params": {
                "ids": [torrent_hash],
                "files_wanted": [2, 4],
                "files_unwanted": [0, 1],
            },
            "id": 1,
        }
    ]


@pytest.mark.asyncio
async def test_file_selection_rejects_overlap() -> None:
    adapter = QbittorrentAdapter("http://qb.invalid", None)
    with pytest.raises(ValueError, match="同时设为 wanted 与 unwanted"):
        await adapter.set_file_selection(
            "a" * 40,
            wanted=(1, 2),
            unwanted=(2, 3),
        )


# --- Transmission 4.0.x 旧协议（kebab-case 方法名 + arguments/tag）-----------------
#
# 用户现场跑的 4.0.5 仍是旧信封：不返回 jsonrpc/id，方法名是 session-get，
# 字段名在 session-* 里是 download-dir、在 torrent-* 里是 hashString/percentDone。
# 之前只实现了 4.1 的 JSON-RPC 2.0，连接测试直接报「响应标识无效」。


def _legacy_handshake(request: httpx2.Request) -> httpx2.Response | None:
    if request.headers.get("X-Transmission-Session-Id") != "synthetic-session":
        return httpx2.Response(409, headers={"X-Transmission-Session-Id": "synthetic-session"})
    return None


@pytest.mark.asyncio
async def test_transmission_40_legacy_envelope_is_detected_and_reports_version() -> None:
    legacy_requests: list[dict[str, object]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        handshake = _legacy_handshake(request)
        if handshake is not None:
            return handshake
        body = json.loads(request.content.decode())
        if body.get("jsonrpc") == "2.0":
            # 4.0.x 认不出 JSON-RPC 2.0 信封，用旧信封回一个失败结果。
            return httpx2.Response(
                200, json={"arguments": {}, "result": "method name not recognized"}
            )
        legacy_requests.append(body)
        assert body == {
            "method": "session-get",
            "arguments": {"fields": ["version", "rpc-version-semver"]},
            "tag": 1,
        }
        return httpx2.Response(
            200,
            json={
                "arguments": {"version": "4.0.5", "rpc-version-semver": "17"},
                "result": "success",
            },
        )

    adapter = TransmissionAdapter(
        "http://tr.invalid:9091/transmission/rpc",
        DownloaderCredential(username="rpc", password="synthetic-password"),
        transport=httpx2.MockTransport(handler),
    )

    result = await adapter.test_connection()

    assert len(legacy_requests) == 1
    assert result.capabilities.client == "Transmission"
    assert result.capabilities.version == "4.0.5"
    assert result.capabilities.api_version == "17"
    assert result.capabilities.supports_verify_progress is True


@pytest.mark.asyncio
async def test_transmission_40_legacy_runtime_metrics_map_camel_case_fields() -> None:
    legacy_methods: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        handshake = _legacy_handshake(request)
        if handshake is not None:
            return handshake
        body = json.loads(request.content.decode())
        if body.get("jsonrpc") == "2.0":
            return httpx2.Response(
                200, json={"arguments": {}, "result": "method name not recognized"}
            )
        method = body["method"]
        legacy_methods.append(method)
        arguments: dict[str, object]
        if method == "session-stats":
            arguments = {
                "uploadSpeed": 300,
                "downloadSpeed": 400,
                "activeTorrentCount": 2,
                "torrentCount": 3,
            }
        elif method == "session-get":
            assert body["arguments"] == {"fields": ["download-dir", "download-dir-free-space"]}
            arguments = {"download-dir": "/downloads", "download-dir-free-space": 4500}
        elif method == "torrent-get":
            assert body["arguments"] == {"fields": ["totalSize"]}
            arguments = {"torrents": [{"totalSize": 1000}, {"totalSize": 2000}]}
        elif method == "free-space":
            assert body["arguments"] == {"path": "/downloads"}
            arguments = {"path": "/downloads", "size-bytes": 5000}
        else:
            raise AssertionError(f"unexpected method {method}")
        return httpx2.Response(200, json={"arguments": arguments, "result": "success"})

    adapter = TransmissionAdapter(
        "http://tr.invalid:9091/transmission/rpc",
        DownloaderCredential(username="rpc", password="synthetic-password"),
        transport=httpx2.MockTransport(handler),
    )

    metrics = await adapter.runtime_metrics()

    # 首次调用先用 JSON-RPC 2.0 探测（旧服务端回的是旧信封），判定后按旧方法名重发，
    # 之后每次调用都直接走旧信封。
    assert legacy_methods == ["session-stats", "session-get", "torrent-get", "free-space"]
    assert metrics.upload_speed_bytes_per_second == 300
    assert metrics.download_speed_bytes_per_second == 400
    assert metrics.total_content_size_bytes == 3000
    assert metrics.free_space_bytes == 5000
    assert metrics.active_torrent_count == 2
    assert metrics.total_torrent_count == 3


@pytest.mark.asyncio
async def test_transmission_40_legacy_write_primitives_use_legacy_argument_names() -> None:
    torrent_hash = "a" * 40
    seen: dict[str, dict[str, object]] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        handshake = _legacy_handshake(request)
        if handshake is not None:
            return handshake
        body = json.loads(request.content.decode())
        if body.get("jsonrpc") == "2.0":
            return httpx2.Response(
                200, json={"arguments": {}, "result": "method name not recognized"}
            )
        method = body["method"]
        seen[method] = body["arguments"]
        if method == "torrent-add":
            arguments: dict[str, object] = {
                "torrent-added": {"id": 7, "name": "Synthetic", "hashString": torrent_hash}
            }
        elif method == "torrent-set" or method == "torrent-remove":
            arguments = {}
        else:
            raise AssertionError(f"unexpected method {method}")
        return httpx2.Response(200, json={"arguments": arguments, "result": "success"})

    adapter = TransmissionAdapter(
        "http://tr.invalid:9091/transmission/rpc",
        DownloaderCredential(username="rpc", password="synthetic-password"),
        transport=httpx2.MockTransport(handler),
    )

    added = await adapter.add_torrent(
        TransmissionAddRequest(
            torrent_content=b"synthetic-torrent-payload",
            save_path="/downloads/seed",
            labels=("packbreaker-test",),
        )
    )
    await adapter.set_file_selection(torrent_hash, wanted=(2, 4), unwanted=(0, 1))
    await adapter.remove_torrent_keep_files(torrent_hash)

    assert added.torrent_hash == torrent_hash
    assert added.duplicate is False
    assert seen["torrent-add"] == {
        "metainfo": b64encode(b"synthetic-torrent-payload").decode("ascii"),
        "download-dir": "/downloads/seed",
        "paused": True,
        "labels": ["packbreaker-test"],
    }
    assert seen["torrent-set"] == {
        "ids": [torrent_hash],
        "files-wanted": [2, 4],
        "files-unwanted": [0, 1],
    }
    assert seen["torrent-remove"] == {"ids": [torrent_hash], "delete-local-data": False}


@pytest.mark.asyncio
async def test_qbittorrent_list_torrents_reads_real_uploaded_bytes() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/auth/login"):
            return httpx2.Response(200, text="Ok.", headers={"Set-Cookie": "SID=fake; path=/"})
        if request.url.path.endswith("/torrents/info"):
            return httpx2.Response(
                200,
                json=[
                    {
                        "hash": "a" * 40,
                        "name": "movie.mkv",
                        "state": "seeding",
                        "progress": 1.0,
                        "size": 1024,
                        "category": "",
                        "tags": "",
                        "tracker": "",
                        "save_path": "/downloads",
                        "content_path": "/downloads/movie.mkv",
                        "seeding_time": 3600,
                        "uploaded": 4096,
                    }
                ],
            )
        return httpx2.Response(404)

    adapter = QbittorrentAdapter(
        "http://qb.invalid:8080",
        DownloaderCredential(username="admin", password="synthetic-password"),
        transport=httpx2.MockTransport(handler),
    )

    torrents = await adapter.list_torrents()

    assert torrents[0].uploaded_bytes == 4096


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["uploaded_ever", "uploadedEver"])
async def test_transmission_list_torrents_reads_uploaded_bytes(field: str) -> None:
    seen_fields: list[list[str]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.headers.get("X-Transmission-Session-Id") != "synthetic-session":
            return httpx2.Response(409, headers={"X-Transmission-Session-Id": "synthetic-session"})
        body = json.loads(request.content.decode())
        assert body["method"] == "torrent_get"
        seen_fields.append(body["params"]["fields"])
        return httpx2.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "torrents": [
                        {
                            "hash_string": "a" * 40,
                            "name": "movie.mkv",
                            "download_dir": "/downloads",
                            "status": 4,
                            "labels": [],
                            "percent_done": 1.0,
                            "total_size": 1024,
                            "trackers": [
                                {"announce": "https://public.example.invalid/announce"},
                                {"announce": "https://tracker.hdtime.org/announce"},
                                {"announce": "https://tracker.hdtime.org/announce"},
                            ],
                            field: 8192,
                        }
                    ]
                },
            },
        )

    adapter = TransmissionAdapter(
        "http://tr.invalid:9091/transmission/rpc",
        DownloaderCredential(username="rpc", password="synthetic-password"),
        transport=httpx2.MockTransport(handler),
    )

    torrents = await adapter.list_torrents()

    assert "uploaded_ever" in seen_fields[0]
    assert "uploadedEver" in seen_fields[0]
    assert "trackers" in seen_fields[0]
    assert torrents[0].uploaded_bytes == 8192
    assert torrents[0].tracker == "https://public.example.invalid/announce"
    assert torrents[0].tracker_urls == (
        "https://public.example.invalid/announce",
        "https://tracker.hdtime.org/announce",
    )
