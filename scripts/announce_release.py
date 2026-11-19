"""把新版本发布消息发送到 Telegram 频道。

发布流水线在 GitHub Release 与镜像都确认就绪后调用本脚本，把当前版本的摘要
和要点推到订阅频道，末尾带上 Release 链接与镜像地址。

内容来源是仓库里的 RELEASE_NOTES.md（与 Release body 同一份），因此频道消息
不会和发布说明漂移。凭据只从环境变量读取：TELEGRAM_BOT_TOKEN / TELEGRAM_CHANNEL_ID。

用法：

    uv run python scripts/announce_release.py \
        --tag v1.1.13 --notes-file RELEASE_NOTES.md \
        --release-url https://github.com/OWNER/REPO/releases/tag/v1.1.13 \
        --image ghcr.io/yyxiaoma/packbreaker --digest sha256:... --dry-run
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

TELEGRAM_API = "https://api.telegram.org"
# Telegram 单条消息上限 4096 字符；留出余量给尾部链接与省略提示。
MESSAGE_LIMIT = 3800
MAX_SECTIONS = 5
MAX_ITEMS_PER_SECTION = 5
# 单条要点只在极端长句时截断，正常情况下整句保留，靠总长度预算控制篇幅。
MAX_ITEM_LENGTH = 400
MAX_SUMMARY_LENGTH = 300

RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
RETRY_DELAYS = (2.0, 5.0)

_H1 = re.compile(r"^#\s+(.*)$")
_H2 = re.compile(r"^##\s+(.*)$")
_BULLET = re.compile(r"^[-*]\s+(.*)$")
_INLINE_MARKUP = re.compile(r"\*\*|`|~~")
# 这些分节在消息里由脚本自己渲染或属于噪音，不再重复搬运。
_SKIPPED_SECTION_KEYWORDS = ("镜像",)


@dataclass(frozen=True, slots=True)
class ReleaseNotes:
    title: str
    summary: str
    sections: tuple[tuple[str, tuple[str, ...]], ...]


@dataclass(frozen=True, slots=True)
class Announcement:
    text: str
    truncated: bool


def parse_release_notes(markdown: str) -> ReleaseNotes:
    """解析发布说明：标题、首段摘要、按 H2 分组的要点。"""

    title = ""
    summary = ""
    sections: list[tuple[str, list[str]]] = []
    in_code_block = False

    for raw in markdown.splitlines():
        line = raw.rstrip()
        stripped = line.strip()

        if stripped.startswith("```"):
            in_code_block = not in_code_block
            continue
        if in_code_block or not stripped:
            continue
        if stripped.startswith("|") or stripped.startswith(">"):
            continue

        heading_one = _H1.match(stripped)
        if heading_one is not None:
            title = _clean(heading_one.group(1))
            continue
        heading_two = _H2.match(stripped)
        if heading_two is not None:
            sections.append((_clean(heading_two.group(1)), []))
            continue

        bullet = _BULLET.match(stripped)
        if bullet is None:
            # 分节里的散文段落不进消息（摘要已经有叙述），只搬运要点条目，
            # 否则频道消息会被长段落撑爆。
            if not sections and not summary:
                summary = _clean(stripped)[:MAX_SUMMARY_LENGTH]
            continue

        text = _clean(bullet.group(1))
        if not text:
            continue
        if not sections:
            if not summary:
                summary = text[:MAX_SUMMARY_LENGTH]
            continue
        items = sections[-1][1]
        if len(items) < MAX_ITEMS_PER_SECTION:
            items.append(text)

    return ReleaseNotes(
        title=title,
        summary=summary,
        sections=tuple(
            (section_title, tuple(items))
            for section_title, items in sections
            if items and not any(word in section_title for word in _SKIPPED_SECTION_KEYWORDS)
        ),
    )


def build_announcement(
    notes: ReleaseNotes,
    *,
    version: str,
    release_url: str,
    image: str,
    limit: int = MESSAGE_LIMIT,
) -> Announcement:
    """拼出频道消息；超长时优先保留前面的要点，尾部固定保留链接。"""

    header = [f"🚀 <b>PackBreaker v{version} 已发布</b>", ""]
    if notes.summary:
        header.extend([escape(notes.summary), ""])

    footer_lines = [
        "<b>镜像</b>",
        f"<code>{escape(image)}:{escape(version)}</code>",
        f"<code>docker.io/yyxiaoma01/packbreaker:{escape(version)}</code>",
        "",
        f'<a href="{escape(release_url)}">查看完整发布说明</a>',
    ]
    tail = "\n".join(footer_lines)
    base = "\n".join(header)
    budget = limit - len(base) - len(tail)

    body: list[str] = []
    used = 0
    truncated = False
    for section_title, items in notes.sections[:MAX_SECTIONS]:
        block = [f"<b>{escape(section_title)}</b>"]
        for item in items:
            block.append(f"• {escape(_clip(item, MAX_ITEM_LENGTH))}")
        block_text = "\n".join(block)
        cost = len(block_text) + 2
        if used + cost > budget:
            truncated = True
            break
        body.extend(block)
        body.append("")
        used += cost

    if truncated:
        tail = "…（其余内容见发布说明）\n" + tail

    parts = [base.rstrip(), "\n".join(body).strip(), tail.strip()]
    text = "\n\n".join(part for part in parts if part)
    return Announcement(text=text, truncated=truncated)


def send_telegram(
    *,
    token: str,
    chat_id: str,
    text: str,
    opener: Callable[[urllib.request.Request, float], object] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """调用 Bot API 发送消息；限流与 5xx 退避重试，永久错误直接抛出。"""

    if not token or not chat_id:
        raise RuntimeError("缺少 TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHANNEL_ID")

    payload = json.dumps(
        {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
    ).encode()
    url = f"{TELEGRAM_API}/bot{token}/sendMessage"
    open_url = opener or (lambda request, timeout: urllib.request.urlopen(request, timeout=timeout))

    for attempt in range(len(RETRY_DELAYS) + 1):
        request = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            response = open_url(request, 20.0)
            body = json.loads(getattr(response, "read", lambda: b"{}")())
        except urllib.error.HTTPError as exc:
            if exc.code in RETRY_STATUS and attempt < len(RETRY_DELAYS):
                sleep(RETRY_DELAYS[attempt])
                continue
            # 不要把 URL（含 token）写进日志。
            raise RuntimeError(f"Telegram 发送失败：HTTP {exc.code}") from None
        except urllib.error.URLError as exc:
            if attempt < len(RETRY_DELAYS):
                sleep(RETRY_DELAYS[attempt])
                continue
            raise RuntimeError(f"Telegram 发送失败：{exc.reason}") from None

        if isinstance(body, dict) and body.get("ok") is True:
            return
        description = body.get("description") if isinstance(body, dict) else None
        raise RuntimeError(f"Telegram 拒绝发送：{description or '未知原因'}")

    raise RuntimeError("Telegram 发送失败：重试次数已用尽")


def escape(value: str) -> str:
    return html.escape(value, quote=False)


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", _INLINE_MARKUP.sub("", value)).strip()


def _clip(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 1] + "…"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把新版本发布消息发送到 Telegram 频道")
    parser.add_argument("--tag", required=True)
    parser.add_argument("--notes-file", type=Path, required=True)
    parser.add_argument("--release-url", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    version = args.tag[1:] if args.tag.startswith("v") else args.tag
    notes = parse_release_notes(args.notes_file.read_text(encoding="utf-8"))
    if not notes.sections and not notes.summary:
        print("发布说明为空，跳过 Telegram 通知。", file=sys.stderr)
        return 0

    announcement = build_announcement(
        notes,
        version=version,
        release_url=args.release_url,
        image=args.image,
    )
    if args.dry_run:
        print(announcement.text)
        print(
            f"\n[字符数 {len(announcement.text)}，截断={announcement.truncated}]", file=sys.stderr
        )
        return 0

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHANNEL_ID", "").strip()
    if not token or not chat_id:
        # 未配置凭据时不让发布流水线失败：发版本身已经完成。
        print("未配置 TELEGRAM_BOT_TOKEN / TELEGRAM_CHANNEL_ID，跳过通知。", file=sys.stderr)
        return 0

    send_telegram(token=token, chat_id=chat_id, text=announcement.text)
    print(f"已发送 v{version} 发布通知（{len(announcement.text)} 字符）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
