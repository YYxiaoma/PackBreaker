from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import httpx2
import pytest

from backend.app.domain.site_adapter import TorrentPayload
from backend.app.domain.site_config import SiteKind, site_profile
from backend.app.domain.site_search import (
    SearchMediaType,
    SearchPage,
    SearchQuery,
    normalize_candidate_meta,
)
from backend.app.infrastructure.adapters.sites import SiteAdapterFactory
from scripts import check_candidate_torrent_readonly as acceptance

_SYNTHETIC_TORRENT = (
    b"d4:infod6:lengthi1e4:name9:synthetic12:piece lengthi16384e6:pieces20:01234567890123456789ee"
)


def test_candidate_torrent_acceptance_refuses_to_contact_site_by_default(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(acceptance, "_SECRET_PATH", tmp_path / "not-present.secret")
    assert acceptance.main(["--site", "keepfrds"]) == 2
    assert "未发起站点请求" in capsys.readouterr().out
    assert acceptance.main(["--site", "keepfrds", "--live"]) == 2
    assert "未发起站点请求" in capsys.readouterr().out
    assert acceptance.main(["--site", "rousi"]) == 2
    assert "未发起站点请求" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_rousi_one_shot_acceptance_separates_api_key_and_cookie() -> None:
    requests: list[str] = []
    key = "synthetic-rousi-api-key"
    cookie = "synthetic-rousi-download-cookie"

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request.url.path)
        assert request.url.host == "rousi.pro"
        assert request.headers.get("authorization") is None
        if request.url.path == "/api/v1/torrents":
            assert request.headers.get("api-token") == key
            assert request.headers.get("cookie") is None
            return httpx2.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "page": 1,
                        "page_size": 100,
                        "total": 1,
                        "torrents": [
                            {
                                "id": 123,
                                "title": "Synthetic.Movie.2024",
                                "size": 1,
                                "seeders": 1,
                                "leechers": 0,
                            }
                        ],
                    },
                },
            )
        assert request.url.path == "/api/v1/torrents/123/download"
        assert request.headers.get("cookie") == cookie
        assert request.headers.get("api-token") is None
        return httpx2.Response(
            200,
            content=_SYNTHETIC_TORRENT,
            headers={"content-type": "application/x-bittorrent"},
        )

    seconds: list[float] = []

    async def fake_sleep(duration: float) -> None:
        seconds.append(duration)

    result = await acceptance.check_one_site(
        "rousi",
        config={
            "Rousi Pro": {
                "url": site_profile(SiteKind.ROUSI_PRO).base_url,
                "auth_type": "cookie",
                "api_key": key,
                "cookie": cookie,
            }
        },
        factory=SiteAdapterFactory(transport=httpx2.MockTransport(handler)),
        sleep=fake_sleep,
    )
    assert result["status"] == "VALID_METAINFO"
    assert requests == ["/api/v1/torrents", "/api/v1/torrents/123/download"]
    assert seconds == [2.0]
    summary = json.dumps(result)
    for private in (key, cookie, "Synthetic.Movie", "123"):
        assert private not in summary


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "entry",
    (
        {"url": "https://rousi.pro", "auth_type": "cookie", "cookie": "synthetic-cookie"},
        {"url": "https://rousi.pro", "auth_type": "cookie", "api_key": "synthetic-key"},
        {
            "url": "https://outside.invalid",
            "auth_type": "cookie",
            "api_key": "synthetic-key",
            "cookie": "synthetic-cookie",
        },
        {
            "url": "https://rousi.pro",
            "auth_type": "cookie",
            "api_key": "synthetic-key",
            "cookie": "  ",
        },
    ),
)
async def test_rousi_acceptance_requires_both_secrets_and_trusted_origin(
    entry: dict[str, str],
) -> None:
    class DeniedFactory:
        def create(self, **_kwargs: object) -> None:
            raise AssertionError("Invalid Rousi acceptance config must never reach the network")

    result = await acceptance.check_one_site(
        "rousi",
        config={"Rousi Pro": entry},
        factory=cast(SiteAdapterFactory, DeniedFactory()),
    )
    assert result == {"site": "rousi", "status": "CONFIG_BLOCKED"}


@pytest.mark.asyncio
async def test_candidate_torrent_acceptance_one_fetch_without_sensitive_output() -> None:
    class FakeAdapter:
        fetch_count = 0
        search_count = 0

        async def search(self, query: SearchQuery) -> SearchPage:
            self.search_count += 1
            assert query == SearchQuery(("2024",), SearchMediaType.MOVIE, page_size=20)
            candidate = normalize_candidate_meta(
                site_id="keepfrds",
                torrent_id="123",
                display_name="Synthetic.Movie.2026.1080p.WEB-DL",
                total_size=1,
                seeders=3,
            )
            return SearchPage("keepfrds", 1, (candidate,), False)

        async def fetch_torrent(self, torrent_id: str) -> TorrentPayload:
            assert torrent_id == "123"
            self.fetch_count += 1
            return TorrentPayload("keepfrds", torrent_id, _SYNTHETIC_TORRENT)

    class FakeFactory:
        def create(self, **kwargs: object) -> FakeAdapter:
            assert kwargs["kind"] is SiteKind.KEEPFRDS
            assert kwargs["base_url"] == site_profile(SiteKind.KEEPFRDS).base_url
            assert kwargs["credential"] == "synthetic-cookie"
            return adapter

    seconds: list[float] = []

    async def fake_sleep(duration: float) -> None:
        seconds.append(duration)

    adapter = FakeAdapter()
    result = await acceptance.check_one_site(
        "keepfrds",
        config={
            "KeepFrds": {
                "url": site_profile(SiteKind.KEEPFRDS).base_url,
                "auth_type": "cookie",
                "cookie": "synthetic-cookie",
            }
        },
        factory=cast(SiteAdapterFactory, FakeFactory()),
        sleep=fake_sleep,
    )
    assert result["status"] == "VALID_METAINFO"
    assert result["torrent_kind"] == "V1"
    assert result["file_count"] == 1
    assert adapter.search_count == adapter.fetch_count == 1
    assert len(seconds) == 1 and seconds[0] >= 2
    safe_summary = json.dumps(result)
    for secret in ("synthetic-cookie", "Synthetic.Movie", "123", "01234567890123456789"):
        assert secret not in safe_summary


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_entry",
    (
        {"url": "https://outside.invalid", "auth_type": "cookie", "cookie": "synthetic-cookie"},
        {"url": 123, "auth_type": "cookie", "cookie": "synthetic-cookie"},
        {"url": "https://pt.keepfrds.com", "auth_type": "api_key", "cookie": "synthetic-cookie"},
        {"url": "https://pt.keepfrds.com", "auth_type": "cookie", "cookie": None},
    ),
)
async def test_candidate_torrent_acceptance_rejects_bad_config_without_network(
    bad_entry: Mapping[str, object],
) -> None:
    class DeniedFactory:
        def create(self, **_kwargs: object) -> None:
            raise AssertionError("Rejected candidate must never contact a site")

    result = await acceptance.check_one_site(
        "keepfrds",
        config={"KeepFrds": dict(bad_entry)},
        factory=cast(SiteAdapterFactory, DeniedFactory()),
    )
    assert result == {"site": "keepfrds", "status": "CONFIG_BLOCKED"}


def test_candidate_torrent_acceptance_failure_returns_nonzero_and_no_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret_path = tmp_path / "site-acceptance.secret"
    secret_path.write_text('{"KeepFrds": {"cookie": "synthetic-secret"}}')
    secret_path.chmod(0o600)
    monkeypatch.setattr(acceptance, "_SECRET_PATH", secret_path)

    async def fake_check(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {
            "site": "keepfrds",
            "status": "FAILED_NO_RETRY",
            "stage": "FETCH_TORRENT",
            "error_code": "SITE_UNAVAILABLE",
        }

    monkeypatch.setattr(acceptance, "check_one_site", fake_check)
    assert acceptance.main(["--site", "keepfrds", "--live", "--acknowledge-download-record"]) == 1
    text = capsys.readouterr().out
    assert '"stage": "FETCH_TORRENT"' in text
    assert "synthetic-secret" not in text
