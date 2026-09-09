from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from typing import Protocol

from backend.app.domain.site_search import (
    CandidateMeta,
    SearchPage,
    SearchQuery,
    SiteSearchCapabilities,
)


@dataclass(frozen=True, slots=True)
class SiteConnectionResult:
    site_id: str
    connected: bool = True

    def __post_init__(self) -> None:
        if not self.site_id.strip():
            raise ValueError("站点连接结果必须包含 site_id")


@dataclass(frozen=True, slots=True)
class TorrentDetails:
    candidate: CandidateMeta

    @property
    def site_id(self) -> str:
        return self.candidate.site_id

    @property
    def torrent_id(self) -> str:
        return self.candidate.torrent_id


@dataclass(frozen=True, slots=True)
class TorrentPayload:
    site_id: str
    torrent_id: str
    content: bytes = field(repr=False)
    fetched_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    sha256_digest: str = ""

    def __post_init__(self) -> None:
        if not self.site_id.strip() or not self.torrent_id.strip():
            raise ValueError("torrent payload 必须包含站点与远程 ID")
        if not self.content:
            raise ValueError("torrent payload 不能为空")
        if self.fetched_at.tzinfo is None or self.fetched_at.utcoffset() is None:
            raise ValueError("torrent payload 获取时间必须带时区")
        object.__setattr__(self, "fetched_at", self.fetched_at.astimezone(UTC))
        digest = sha256(self.content).hexdigest()
        if self.sha256_digest and self.sha256_digest != digest:
            raise ValueError("torrent payload digest 与内容不一致")
        object.__setattr__(self, "sha256_digest", digest)


class SiteAdapter(Protocol):
    async def capabilities(self) -> SiteSearchCapabilities: ...

    async def test_connection(self) -> SiteConnectionResult: ...

    async def search(self, query: SearchQuery) -> SearchPage: ...

    async def fetch_details(self, torrent_id: str) -> TorrentDetails: ...

    async def fetch_torrent(self, torrent_id: str) -> TorrentPayload: ...
