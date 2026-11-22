"""M-Team 404 必须映射为独立的 SITE_NOT_FOUND。"""

from __future__ import annotations

import asyncio

import httpx2
import pytest

from backend.app.infrastructure.adapters.site_errors import SiteAdapterError
from backend.app.infrastructure.adapters.sites import MTeamAdapter


def test_mteam_detail_404_is_site_not_found() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(404)

    adapter = MTeamAdapter("synthetic-key", transport=httpx2.MockTransport(handler))
    with pytest.raises(SiteAdapterError) as failure:
        asyncio.run(adapter.fetch_details("123456"))
    assert failure.value.code == "SITE_NOT_FOUND"
