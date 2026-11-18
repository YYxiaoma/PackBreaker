"""把各版本 tag 里的 RELEASE_NOTES.md 汇总成随镜像发布的升级日志。

升级日志要给「关于」页展示，必须在离线部署里可用，所以生成一份静态 JSON 放进
镜像（`resources/release_notes.json`），前端只做渲染。

用法（发布前跑一次）：

    uv run python scripts/build_release_notes.py

数据源是 git tag 与 tag 里的 RELEASE_NOTES.md，不做任何网络请求；条目按版本
倒序排列，最新版本在最前。
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "resources" / "release_notes.json"
SCHEMA_VERSION = 1

MAX_ENTRIES = 50
MAX_SUMMARY_LENGTH = 240
MAX_ITEMS_PER_SECTION = 10
MAX_ITEM_LENGTH = 300

# 镜像地址是发布产物信息，不是变更说明，卡片里不展示。
SKIPPED_SECTION_KEYWORDS = ("镜像",)

_TAG_PATTERN = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
_BULLET_PATTERN = re.compile(r"^[-*]\s+(.*)$")
_INLINE_PATTERN = re.compile(r"\*\*|`")


def _clean_inline(text: str) -> str:
    """去掉仅用于排版的 markdown 标记，保留可读正文。"""

    cleaned = _INLINE_PATTERN.sub("", text)
    cleaned = cleaned.replace("~~", "")
    return re.sub(r"\s+", " ", cleaned).strip()


def parse_release_notes_markdown(markdown: str) -> tuple[str, list[dict[str, object]]]:
    """把一份 RELEASE_NOTES.md 解析成「一句话摘要 + 分节条目」。

    只提取叙述性内容：标题、列表项与段落；代码块、表格与引用块会被跳过，
    以免升级日志里出现无法在卡片中阅读的排版碎片。
    """

    summary = ""
    sections: list[dict[str, object]] = []
    current: dict[str, object] | None = None
    in_code_block = False

    for raw in markdown.splitlines():
        line = raw.rstrip()
        stripped = line.strip()

        if stripped.startswith("```"):
            in_code_block = not in_code_block
            continue
        if in_code_block or not stripped:
            continue
        if stripped.startswith("# "):
            continue
        if stripped.startswith("## "):
            title = _clean_inline(stripped[3:])
            current = {"title": title, "items": []}
            sections.append(current)
            continue
        if stripped.startswith("|") or stripped.startswith(">"):
            continue

        bullet = _BULLET_PATTERN.match(stripped)
        text = _clean_inline(bullet.group(1) if bullet else stripped)
        if not text:
            continue

        if current is None:
            if not summary:
                summary = text[:MAX_SUMMARY_LENGTH]
            continue

        items = current["items"]
        assert isinstance(items, list)
        if bullet is None and items:
            # 非列表行是上一条的续写（发布说明里常见的长句换行），并入而不是单列。
            items[-1] = f"{items[-1]} {text}"[:MAX_ITEM_LENGTH]
            continue
        if len(items) >= MAX_ITEMS_PER_SECTION:
            continue
        items.append(text[:MAX_ITEM_LENGTH])

    cleaned_sections = [
        section
        for section in sections
        if section["items"]
        and not any(keyword in str(section["title"]) for keyword in SKIPPED_SECTION_KEYWORDS)
    ]
    return summary, cleaned_sections


def _run_git(*args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _tags() -> list[tuple[tuple[int, int, int], str, str, str]]:
    """返回 (版本号元组, tag, 发布日期, tag 里的发布说明) 列表，按版本倒序。"""

    raw_tags = _run_git("tag", "--list", "v*.*.*").split()
    collected: list[tuple[tuple[int, int, int], str, str, str]] = []
    for tag in raw_tags:
        matched = _TAG_PATTERN.match(tag)
        if matched is None:
            continue
        version_tuple = (int(matched.group(1)), int(matched.group(2)), int(matched.group(3)))
        released_on = _run_git("log", "-1", "--format=%ad", "--date=short", tag).strip()
        try:
            notes = _run_git("show", f"{tag}:RELEASE_NOTES.md")
        except subprocess.CalledProcessError:
            notes = ""
        collected.append((version_tuple, tag, released_on, notes))

    collected.sort(key=lambda item: item[0], reverse=True)
    return collected


def _project_version() -> str:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = data.get("project")
    version = project.get("version") if isinstance(project, dict) else None
    return version if isinstance(version, str) else ""


def build_catalog() -> dict[str, object]:
    entries: list[dict[str, object]] = []
    for version_tuple, tag, released_on, notes in _tags()[:MAX_ENTRIES]:
        summary, sections = parse_release_notes_markdown(notes)
        if not sections:
            continue
        entries.append(
            {
                "version": ".".join(str(part) for part in version_tuple),
                "tag": tag,
                "released_on": released_on,
                "summary": summary,
                "sections": sections,
            }
        )

    # 发布准备阶段当前版本还没有 tag，用工作区的 RELEASE_NOTES.md 补一条，
    # 这样「关于」页在打 tag 之前就能显示这一版，门禁也能要求目录覆盖当前版本。
    current = _project_version()
    if current and all(entry["version"] != current for entry in entries):
        notes_path = ROOT / "RELEASE_NOTES.md"
        if notes_path.is_file():
            summary, sections = parse_release_notes_markdown(notes_path.read_text(encoding="utf-8"))
            if sections:
                entries.insert(
                    0,
                    {
                        "version": current,
                        "tag": f"v{current}",
                        "released_on": datetime.date.today().isoformat(),
                        "summary": summary,
                        "sections": sections,
                    },
                )

    return {"schema_version": SCHEMA_VERSION, "entries": entries}


def main() -> None:
    parser = argparse.ArgumentParser(description="生成升级日志 resources/release_notes.json")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    catalog = build_catalog()
    entries = catalog["entries"]
    assert isinstance(entries, list)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"已写入 {args.output}（{len(entries)} 个版本）")


if __name__ == "__main__":
    main()
