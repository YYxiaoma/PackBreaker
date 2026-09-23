from __future__ import annotations

import subprocess
import sys

import pytest

from scripts.check_hdtime_ajax_html import inspect_hdtime_ajax_html


@pytest.mark.parametrize(
    ("html", "result"),
    [
        (
            "<div><b>37</b> 条记录 | 总大小：4.500 TB</div>"
            "<table><tr><td>synthetic-private-torrent</td></tr></table>",
            "complete",
        ),
        (
            "<div><b>37</b> 条记录 | 总大小：--</div>"
            "<table><tr><td>5 条记录 | 总大小：7 TB</td></tr></table>",
            "unconfirmed",
        ),
        ("<p>1 - 10 | 11 - 20</p><table><tr><td>1 GB</td></tr></table>", "unconfirmed"),
    ],
)
def test_hdtime_offline_check_does_not_guess_from_page_rows(html: str, result: str) -> None:
    assert inspect_hdtime_ajax_html(html.encode("utf-8")) == result


def test_hdtime_offline_check_rejects_invalid_encoding_and_oversized_input() -> None:
    assert inspect_hdtime_ajax_html(b"") == "empty"
    assert inspect_hdtime_ajax_html(b" \t\r\n") == "empty"
    assert inspect_hdtime_ajax_html("\ufeff".encode("utf-8")) == "empty"
    assert inspect_hdtime_ajax_html(b"\xff\xfe") == "invalid_encoding"
    assert inspect_hdtime_ajax_html(b"a" * (5 * 1024 * 1024 + 1)) == "too_large"


@pytest.mark.parametrize(
    ("html", "expected_code"),
    [
        ("<div><b>37</b> 条记录 | 总大小：4.500 TB</div>", 0),
        ("<div><b>37</b> 条记录 | 总大小：--</div>", 2),
        ("", 2),
    ],
)
def test_hdtime_offline_cli_never_prints_source_or_personal_values(
    html: str, expected_code: int
) -> None:
    sensitive = "UID-secret IP-secret synthetic-private-torrent cookie-secret"
    result = subprocess.run(
        [sys.executable, "-m", "scripts.check_hdtime_ajax_html"],
        input=(html + sensitive if html else "").encode("utf-8"),
        capture_output=True,
        check=False,
    )
    assert result.returncode == expected_code
    assert b"HDTime" in result.stdout
    if not html:
        assert "输入文件为空".encode() in result.stdout
    assert not result.stderr
    for secret in (sensitive, "37", "4.500", "--"):
        assert secret.encode("utf-8") not in result.stdout
