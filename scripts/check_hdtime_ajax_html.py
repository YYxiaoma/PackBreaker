"""Offline HDTime seeding-header check; never connects to a site or prints values.

Usage from repository root (with the candidate runtime dependencies installed):
    python -m scripts.check_hdtime_ajax_html < /private/path/result.txt

The response is read from standard input only, never saved or logged. This checks
the candidate parser's ability to recognize the *structure* of one HTML fragment;
it is not an authenticated online/site-account acceptance test.
"""

from __future__ import annotations

import sys
from typing import Literal

from backend.app.infrastructure.adapters.nexusphp import (
    _DEFAULT_HTML_LIMIT_BYTES,
    _parse_hdtime_ajax_seeding_summary,
    _parse_html,
)
from backend.app.infrastructure.adapters.site_errors import SiteAdapterError

ProbeResult = Literal["complete", "empty", "unconfirmed", "invalid_encoding", "too_large"]


def inspect_hdtime_ajax_html(raw: bytes) -> ProbeResult:
    """Classify one response without exposing any user, torrent, or metric values."""
    if len(raw) > _DEFAULT_HTML_LIMIT_BYTES:
        return "too_large"
    if not raw.strip():
        return "empty"
    try:
        html = raw.decode("utf-8", errors="strict")
    except UnicodeError:
        return "invalid_encoding"
    if not html.strip("\ufeff\u200b \t\r\n"):
        return "empty"
    try:
        return (
            "complete"
            if _parse_hdtime_ajax_seeding_summary(_parse_html(html)) is not None
            else "unconfirmed"
        )
    except SiteAdapterError:
        return "unconfirmed"


def main() -> int:
    if sys.stdin.isatty():
        print("请通过标准输入提供本地 HTML 响应；不会执行网络请求。")
        return 2
    result = inspect_hdtime_ajax_html(sys.stdin.buffer.read(_DEFAULT_HTML_LIMIT_BYTES + 1))
    messages: dict[ProbeResult, str] = {
        "complete": "HDTime 汇总结构：识别到唯一完整配对（仅离线解析）。",
        "empty": "HDTime 汇总结构：输入文件为空，未读到 HTML 响应；请核对文件是否保存成功。",
        "unconfirmed": "HDTime 汇总结构：缺失或存在歧义；不能确认证实的统计配对。",
        "invalid_encoding": "HDTime 汇总结构：输入不是 UTF-8，请先在本地转码。",
        "too_large": "HDTime 汇总结构：响应超过离线检查的大小上限。",
    }
    print(messages[result])
    return 0 if result == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())
