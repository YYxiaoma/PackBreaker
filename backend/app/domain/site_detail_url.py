"""站点种子详情链接的唯一真源。

各站详情页路径并不统一：NexusPHP 系用 ``details.php?id=``，M-Team 用
``/detail/{id}``，Rousi Pro 用 ``/torrents/{id}``。手工添加候选时既要能
从用户粘贴的链接里取回种子 ID，也要能反推出可点击的详情页地址，两边
必须用同一份规则，否则就会出现「页面生成的链接自己校验不过」的情况。
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from urllib.parse import parse_qs, urlsplit

from backend.app.domain.site_config import SiteKind

_ID_RE = r"[0-9]{1,20}"
_PATH_STYLE_RE = re.compile(rf"^/(?:detail|details|torrent|torrents)/({_ID_RE})/?$")
_PATH_PREFIX_BY_KIND: dict[SiteKind, str] = {
    SiteKind.MTEAM: "/detail",
    SiteKind.ROUSI_PRO: "/torrents",
}
_DEFAULT_DETAIL_PATH = "/details.php"


def build_torrent_detail_url(kind: SiteKind, base_url: str, torrent_id: str) -> str:
    """按站点类型拼出详情页地址。"""
    base = urlsplit(base_url)
    origin = f"{base.scheme}://{base.netloc}"
    prefix = _PATH_PREFIX_BY_KIND.get(kind)
    if prefix is not None:
        return f"{origin}{prefix}/{torrent_id}"
    return f"{origin}{_DEFAULT_DETAIL_PATH}?id={torrent_id}"


def parse_torrent_detail_id(url: str, base_url: str) -> str | None:
    """从详情页链接取回种子 ID；主机必须与所选站点一致。

    同时接受 ``details.php?id=`` 与 ``/detail|details|torrent|torrents/{id}``
    两种形态，避免用户从 M-Team / Rousi Pro 复制链接时被误拒。
    """
    try:
        parsed = urlsplit(url.strip())
        base = urlsplit(base_url)
    except TypeError:
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.scheme.casefold() != base.scheme.casefold()
        or (parsed.hostname or "").casefold() != (base.hostname or "").casefold()
        or parsed.port != base.port
    ):
        return None

    path_name = PurePosixPath(parsed.path.rstrip("/") or "/").name.casefold()
    if path_name == "details.php":
        values = parse_qs(parsed.query, keep_blank_values=True).get("id", [])
        if len(values) == 1 and re.fullmatch(_ID_RE, values[0]):
            return values[0]
        return None

    match = _PATH_STYLE_RE.match(parsed.path)
    return match.group(1) if match is not None else None
