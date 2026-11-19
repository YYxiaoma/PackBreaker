from __future__ import annotations

import io
import json
import urllib.error
from email.message import Message
from pathlib import Path

import pytest

from scripts.announce_release import (
    build_announcement,
    parse_release_notes,
    send_telegram,
)

NOTES = """# PackBreaker v1.1.13

两个现场问题的修复：Transmission 4.0.x 连不上。

## 修复：Transmission 连接

适配器此前只实现了 4.1 起的信封。

- 首次调用自动探测协议方言
- 旧信封的方法与参数做显式映射

## 升级说明

- 无数据库迁移

## 镜像

- `ghcr.io/yyxiaoma/packbreaker:1.1.13`
"""


def _notes_with_sections(count: int) -> str:
    blocks = []
    for index in range(count):
        items = "\n".join(f"- 第 {index}-{item} 条要点内容，用来撑长消息" for item in range(5))
        blocks.append(f"## 变更 {index}\n\n{items}")
    return "# PackBreaker v9.9.9\n\n摘要。\n\n" + "\n\n".join(blocks) + "\n"


def test_parse_extracts_title_summary_and_bullets_only() -> None:
    notes = parse_release_notes(NOTES)

    assert notes.title == "PackBreaker v1.1.13"
    assert notes.summary == "两个现场问题的修复：Transmission 4.0.x 连不上。"
    assert [title for title, _ in notes.sections] == ["修复：Transmission 连接", "升级说明"]
    assert notes.sections[0][1] == (
        "首次调用自动探测协议方言",
        "旧信封的方法与参数做显式映射",
    )
    # 分节里的散文段落不进消息，镜像分节整体跳过。
    assert all("适配器此前" not in item for _, items in notes.sections for item in items)


def test_parse_skips_code_blocks_and_tables() -> None:
    markdown = """# PackBreaker v1.1.13

摘要。

## 升级说明

```
docker compose pull
```

- 无数据库迁移

| 字段 | 说明 |
| --- | --- |
"""

    notes = parse_release_notes(markdown)

    assert notes.sections == (("升级说明", ("无数据库迁移",)),)


def test_build_includes_links_images_and_escapes_html() -> None:
    notes = parse_release_notes(
        "# PackBreaker v1.1.13\n\n对比 <1.1.12> & 修复。\n\n## 修复\n\n- 支持 a<b 与 c>d\n"
    )

    announcement = build_announcement(
        notes,
        version="1.1.13",
        release_url="https://example.invalid/releases/tag/v1.1.13",
        image="ghcr.io/yyxiaoma/packbreaker",
    )

    assert announcement.truncated is False
    assert "🚀 <b>PackBreaker v1.1.13 已发布</b>" in announcement.text
    assert "对比 &lt;1.1.12&gt; &amp; 修复。" in announcement.text
    assert "• 支持 a&lt;b 与 c&gt;d" in announcement.text
    assert "<code>ghcr.io/yyxiaoma/packbreaker:1.1.13</code>" in announcement.text
    assert "<code>docker.io/yyxiaoma01/packbreaker:1.1.13</code>" in announcement.text
    assert '<a href="https://example.invalid/releases/tag/v1.1.13">' in announcement.text
    assert "<b>镜像</b>" in announcement.text


def test_build_truncates_long_notes_and_keeps_the_link() -> None:
    notes = parse_release_notes(_notes_with_sections(20))

    announcement = build_announcement(
        notes,
        version="9.9.9",
        release_url="https://example.invalid/releases/tag/v9.9.9",
        image="ghcr.io/yyxiaoma/packbreaker",
        limit=500,
    )

    assert announcement.truncated is True
    assert len(announcement.text) <= 500
    assert "其余内容见发布说明" in announcement.text
    assert "releases/tag/v9.9.9" in announcement.text


class _Response:
    def __init__(self, payload: dict[str, object]) -> None:
        self._buffer = io.BytesIO(json.dumps(payload).encode())

    def read(self) -> bytes:
        return self._buffer.read()


def test_send_telegram_posts_expected_payload() -> None:
    captured: dict[str, object] = {}

    def opener(request: urllib.request.Request, timeout: float) -> _Response:
        captured["url"] = request.full_url
        raw = request.data
        assert isinstance(raw, bytes)
        captured["body"] = json.loads(raw)
        captured["timeout"] = timeout
        return _Response({"ok": True, "result": {"message_id": 1}})

    send_telegram(token="123:token", chat_id="@channel", text="hello", opener=opener)

    assert str(captured["url"]).endswith("/bot123:token/sendMessage")
    assert captured["body"] == {
        "chat_id": "@channel",
        "text": "hello",
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }


def test_send_telegram_retries_after_rate_limit() -> None:
    attempts: list[int] = []
    sleeps: list[float] = []

    def opener(request: urllib.request.Request, timeout: float) -> _Response:
        attempts.append(1)
        if len(attempts) == 1:
            raise urllib.error.HTTPError(
                request.full_url, 429, "Too Many Requests", Message(), None
            )
        return _Response({"ok": True})

    send_telegram(
        token="123:token",
        chat_id="@channel",
        text="hello",
        opener=opener,
        sleep=sleeps.append,
    )

    assert len(attempts) == 2
    assert sleeps


def test_send_telegram_does_not_leak_token_on_permanent_error() -> None:
    def opener(request: urllib.request.Request, timeout: float) -> _Response:
        raise urllib.error.HTTPError(request.full_url, 400, "Bad Request", Message(), None)

    with pytest.raises(RuntimeError) as caught:
        send_telegram(token="123:secret-token", chat_id="@channel", text="hello", opener=opener)

    assert "HTTP 400" in str(caught.value)
    assert "secret-token" not in str(caught.value)


def test_main_skips_without_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    notes_file = tmp_path / "notes.md"
    notes_file.write_text(NOTES, encoding="utf-8")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHANNEL_ID", raising=False)

    from scripts.announce_release import main

    code = main(
        [
            "--tag",
            "v1.1.13",
            "--notes-file",
            str(notes_file),
            "--release-url",
            "https://example.invalid/releases/tag/v1.1.13",
            "--image",
            "ghcr.io/yyxiaoma/packbreaker",
        ]
    )

    assert code == 0
