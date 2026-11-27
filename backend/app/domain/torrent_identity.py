"""种子身份解析：从 .torrent 的 comment 反查所属站点与站点侧种子 ID。

该模块与 ``scripts/build_template_from_transmission.py`` 使用同一套域名词表，
但以严格 bencode 解析取代字节搜索，避免把文件内容里的同名字节误当 comment。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import urlparse

from backend.app.domain.seeding_health import SeedingIdentitySource
from backend.app.domain.site_config import SiteKind

# 各站点的域名片段：用于从 comment 判断种子归属。
SITE_DOMAIN_TOKENS: dict[SiteKind, tuple[str, ...]] = {
    SiteKind.MTEAM: ("m-team", "halomt", "groueta"),
    SiteKind.HDTIME: ("hdtime",),
    SiteKind.HHCLUB: ("hhanclub", "hhclub"),
    SiteKind.KEEPFRDS: ("keepfrds",),
    SiteKind.HDHOME: ("hdhome",),
    SiteKind.UBITS: ("ubits",),
    SiteKind.HDFANS: ("hdfans",),
    SiteKind.BTSCHOOL: ("btschool",),
    SiteKind.PTTIME: ("pttime",),
    SiteKind.ROUSI_PRO: ("rousi",),
    SiteKind.LINGYIN_CLUB: ("soulvoice",),
    SiteKind.PTERCLUB: ("pterclub", "pter.club"),
    SiteKind.AUDIENCES: ("audiences",),
    SiteKind.SPRING_SUNDAY: ("springsunday",),
    SiteKind.HDDOLBY: ("hddolby",),
    SiteKind.U2: ("dmhy",),
    SiteKind.TANGPT: ("tangpt",),
    SiteKind.CARPT: ("carpt",),
}

_URL_RE = re.compile(r"https?://[^\s]+")
_QUERY_ID_RE = re.compile(r"[?&]id=(\d+)")
_PATH_ID_RE = re.compile(r"/(?:detail|details)/(\d+)")
_MAX_COMMENT_BYTES = 8192


@dataclass(frozen=True, slots=True)
class TorrentIdentity:
    site_key: str
    torrent_id: str
    source: SeedingIdentitySource


class _BencodeError(ValueError):
    pass


def _skip_bencode_value(data: bytes, index: int) -> int:
    if index >= len(data):
        raise _BencodeError("bencode 提前结束")
    tag = data[index : index + 1]
    if tag == b"i":
        end = data.find(b"e", index)
        if end < 0:
            raise _BencodeError("整数缺少结束符")
        return end + 1
    if tag in (b"l", b"d"):
        index += 1
        while True:
            if index >= len(data):
                raise _BencodeError("容器缺少结束符")
            if data[index : index + 1] == b"e":
                return index + 1
            index = _skip_bencode_value(data, index)
    return _read_bencode_string(data, index)[1]


def _read_bencode_string(data: bytes, index: int) -> tuple[bytes, int]:
    colon = data.find(b":", index, min(len(data), index + 12))
    if colon < 0:
        raise _BencodeError("字符串长度缺失")
    raw_length = data[index:colon]
    if not raw_length.isdigit():
        raise _BencodeError("字符串长度无效")
    length = int(raw_length)
    start = colon + 1
    end = start + length
    if end > len(data):
        raise _BencodeError("字符串越界")
    return data[start:end], end


def extract_torrent_comment(payload: bytes) -> str | None:
    """只从顶层字典读取 comment；任何结构异常都返回 None。"""
    if not payload or payload[:1] != b"d":
        return None
    index = 1
    try:
        while index < len(payload) and payload[index : index + 1] != b"e":
            key, index = _read_bencode_string(payload, index)
            if key == b"comment":
                value, index = _read_bencode_string(payload, index)
                if not value or len(value) > _MAX_COMMENT_BYTES:
                    return None
                return value.decode("utf-8", errors="replace")
            index = _skip_bencode_value(payload, index)
    except (_BencodeError, ValueError):
        return None
    return None


def site_kind_of_url(url: str) -> SiteKind | None:
    """只认 URL 主机名里的站点域名，避免转载种子串站。"""
    host = urlparse(url).netloc.lower()
    if not host:
        return None
    for kind, tokens in SITE_DOMAIN_TOKENS.items():
        if any(token in host for token in tokens):
            return kind
    return None


def identity_from_tracker(tracker: str | None) -> TorrentIdentity | None:
    """从 tracker announce URL 反推站点；tracker 不提供站点侧种子 ID。"""
    if not tracker:
        return None
    kind = site_kind_of_url(tracker)
    if kind is None:
        return None
    return TorrentIdentity(
        site_key=kind.value.lower(),
        torrent_id="",
        source=SeedingIdentitySource.TRACKER,
    )


def identity_from_trackers(trackers: Iterable[str | None]) -> TorrentIdentity | None:
    """按顺序尝试全部 tracker，返回第一个能识别站点的身份。

    一个任务常同时配置多个 tracker（PT 站、备用站、公共 tracker），
    只看第一个会把「首个条目不是 PT 站」的种子误判为站点未知。
    """
    for tracker in trackers:
        identity = identity_from_tracker(tracker)
        if identity is not None:
            return identity
    return None


def torrent_id_of_url(url: str) -> str | None:
    """按 NexusPHP 的 id 查询参数与 M-Team 的路径段两种形态提取。"""
    match = _QUERY_ID_RE.search(url)
    if match is not None:
        return match.group(1)
    match = _PATH_ID_RE.search(url)
    return match.group(1) if match is not None else None


def identity_from_comment(comment: str | None) -> TorrentIdentity | None:
    if not comment:
        return None
    match = _URL_RE.search(comment)
    if match is None:
        return None
    url = match.group(0)
    kind = site_kind_of_url(url)
    torrent_id = torrent_id_of_url(url)
    if kind is None or torrent_id is None:
        return None
    return TorrentIdentity(
        # 站点配置里的 site_id 统一是小写形式（SiteService._expected_site_id），
        # 这里必须对齐，否则探测阶段匹配不到已启用站点。
        site_key=kind.value.lower(),
        torrent_id=torrent_id,
        source=SeedingIdentitySource.COMMENT,
    )


def identity_from_payload(payload: bytes) -> TorrentIdentity | None:
    return identity_from_comment(extract_torrent_comment(payload))
