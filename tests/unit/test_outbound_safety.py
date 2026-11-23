"""Tests for the shared outbound host guard and its two call sites."""

import pytest

from backend.app.domain.cookiecloud import normalize_cookiecloud_server_url
from backend.app.domain.downloader import normalize_base_url
from backend.app.domain.outbound_safety import is_blocked_outbound_host
from backend.app.domain.verification import DownloaderKind


@pytest.mark.parametrize(
    "host",
    [
        "169.254.169.254",
        "metadata.google.internal",
        "224.0.0.1",
        "240.0.0.1",
        "0.0.0.0",
        "fe80::1",
    ],
)
def test_metadata_and_special_addresses_are_blocked(host: str) -> None:
    assert is_blocked_outbound_host(host) is True


@pytest.mark.parametrize(
    "host",
    ["192.168.20.91", "127.0.0.1", "example.com", "10.0.0.5"],
)
def test_private_and_loopback_hosts_stay_allowed(host: str) -> None:
    assert is_blocked_outbound_host(host) is False


def test_cookiecloud_url_rejects_metadata_address() -> None:
    with pytest.raises(ValueError, match="主机地址不被允许"):
        normalize_cookiecloud_server_url("http://169.254.169.254/")


def test_downloader_url_rejects_metadata_address() -> None:
    with pytest.raises(ValueError, match="主机地址不被允许"):
        normalize_base_url(DownloaderKind.QBITTORRENT, "http://169.254.169.254:8080/")


def test_downloader_private_address_still_works() -> None:
    assert "192.168.20.91" in normalize_base_url(
        DownloaderKind.QBITTORRENT, "http://192.168.20.91:18080/"
    )
