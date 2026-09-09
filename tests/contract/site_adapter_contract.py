from __future__ import annotations

from dataclasses import dataclass

from backend.app.domain.site_adapter import SiteAdapter
from backend.app.domain.site_search import SearchQuery


@dataclass(frozen=True, slots=True)
class SiteAdapterContractCase:
    adapter: SiteAdapter
    query: SearchQuery
    expected_site_id: str
    expected_torrent_id: str


async def assert_read_only_site_adapter_contract(case: SiteAdapterContractCase) -> None:
    capabilities = await case.adapter.capabilities()
    assert capabilities.supports_pagination is True

    connection = await case.adapter.test_connection()
    assert connection.connected is True
    assert connection.site_id == case.expected_site_id

    page = await case.adapter.search(case.query)
    assert page.site_id == case.expected_site_id
    assert page.items
    assert page.items[0].torrent_id == case.expected_torrent_id

    details = await case.adapter.fetch_details(case.expected_torrent_id)
    assert details.site_id == case.expected_site_id
    assert details.torrent_id == case.expected_torrent_id

    payload = await case.adapter.fetch_torrent(case.expected_torrent_id)
    assert payload.site_id == case.expected_site_id
    assert payload.torrent_id == case.expected_torrent_id
    assert payload.content
    assert len(payload.sha256_digest) == 64
    assert "content=" not in repr(payload)
