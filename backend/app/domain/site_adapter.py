from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from math import isfinite
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
class SiteUserProfile:
    site_id: str
    uid: str | None = None
    username: str | None = None
    user_level: str | None = None
    real_uploaded_bytes: int | None = None
    real_downloaded_bytes: int | None = None
    uploaded_bytes: int | None = None
    downloaded_bytes: int | None = None
    ratio: float | None = None
    torrents_posted: int | None = None
    seeding_count: int | None = None
    seeding_size_bytes: int | None = None
    bonus: float | None = None
    seeding_points: float | None = None
    bonus_per_hour: float | None = None
    fetched_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not self.site_id.strip():
            raise ValueError("站点用户详情必须包含 site_id")
        for field_name in (
            "real_uploaded_bytes",
            "real_downloaded_bytes",
            "uploaded_bytes",
            "downloaded_bytes",
            "torrents_posted",
            "seeding_count",
            "seeding_size_bytes",
        ):
            value = getattr(self, field_name)
            if value is not None and (type(value) is not int or value < 0):
                # An adapter must not pass a rounded float, boolean or NaN as
                # a count or byte size. Enforce this once for all site kinds.
                raise ValueError(f"{field_name} 必须是非负整数")
        for field_name in ("ratio", "bonus", "seeding_points", "bonus_per_hour"):
            value = getattr(self, field_name)
            if value is not None and (not isfinite(value) or value < 0):
                raise ValueError(f"{field_name} 必须是有限非负数")
        if self.fetched_at.tzinfo is None or self.fetched_at.utcoffset() is None:
            raise ValueError("站点用户详情获取时间必须带时区")
        object.__setattr__(self, "fetched_at", self.fetched_at.astimezone(UTC))


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

    async def fetch_user_profile(self) -> SiteUserProfile: ...

    async def search(self, query: SearchQuery) -> SearchPage: ...

    async def fetch_details(self, torrent_id: str) -> TorrentDetails: ...

    async def fetch_torrent(self, torrent_id: str) -> TorrentPayload: ...
