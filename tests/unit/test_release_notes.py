from __future__ import annotations

import json
from pathlib import Path

from backend.app.application.release_notes import load_release_notes
from backend.app.versioning import app_version
from scripts.build_release_notes import parse_release_notes_markdown

ROOT = Path(__file__).resolve().parents[2]


def _write(tmp_path: Path, payload: dict[str, object]) -> Path:
    path = tmp_path / "release_notes.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _entry(version: str, *, sections: list[dict[str, object]] | None = None) -> dict[str, object]:
    return {
        "version": version,
        "tag": f"v{version}",
        "released_on": "2026-11-18",
        "summary": f"{version} 的摘要",
        "sections": sections
        if sections is not None
        else [{"title": "新增", "items": [f"{version} 的新功能"]}],
    }


def test_entries_are_sorted_and_current_version_is_flagged(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        {"schema_version": 1, "entries": [_entry("1.1.9"), _entry("1.1.10"), _entry("1.0.15")]},
    )

    catalog = load_release_notes(path, current_version="v1.1.10")

    assert [entry.version for entry in catalog.entries] == ["1.1.10", "1.1.9", "1.0.15"]
    assert [entry.is_current for entry in catalog.entries] == [True, False, False]
    assert catalog.covers("1.1.10") is True
    assert catalog.covers("9.9.9") is False


def test_missing_file_yields_empty_catalog(tmp_path: Path) -> None:
    catalog = load_release_notes(tmp_path / "absent.json", current_version="1.1.10")

    assert catalog.entries == ()


def test_invalid_json_yields_empty_catalog(tmp_path: Path) -> None:
    path = tmp_path / "release_notes.json"
    path.write_text("{ not json", encoding="utf-8")

    assert load_release_notes(path).entries == ()


def test_entry_without_sections_is_skipped(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        {
            "schema_version": 1,
            "entries": [_entry("1.1.10", sections=[]), _entry("1.1.9")],
        },
    )

    catalog = load_release_notes(path)

    assert [entry.version for entry in catalog.entries] == ["1.1.9"]


def test_duplicate_versions_keep_the_first_entry(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        {
            "schema_version": 1,
            "entries": [
                _entry("1.1.10", sections=[{"title": "新增", "items": ["第一条"]}]),
                _entry("1.1.10", sections=[{"title": "新增", "items": ["第二条"]}]),
            ],
        },
    )

    catalog = load_release_notes(path)

    assert len(catalog.entries) == 1
    assert catalog.entries[0].sections[0].items == ("第一条",)


def test_markdown_parser_merges_continuation_lines_and_keeps_summary() -> None:
    markdown = """# PackBreaker v1.1.10

一句话摘要，说明这一版做了什么。

## 修复

- **按钮置灰**：升级一次后按钮永久置灰。
  表现为「上一次升级现场需要人工核对」。
- 拉取失败自动重试。

## 镜像

- ghcr.io/yyxiaoma/packbreaker:1.1.10
"""

    summary, sections = parse_release_notes_markdown(markdown)

    assert summary == "一句话摘要，说明这一版做了什么。"
    assert [section["title"] for section in sections] == ["修复"]
    assert sections[0]["items"] == [
        "按钮置灰：升级一次后按钮永久置灰。 表现为「上一次升级现场需要人工核对」。",
        "拉取失败自动重试。",
    ]


def test_markdown_parser_skips_code_blocks() -> None:
    markdown = """# PackBreaker v1.1.10

摘要。

## 升级说明

```
docker compose pull && docker compose up -d
```

- 无数据库迁移。
"""

    _, sections = parse_release_notes_markdown(markdown)

    assert sections[0]["items"] == ["无数据库迁移。"]


def test_repository_catalog_covers_current_version() -> None:
    """发布时必须重新生成 resources/release_notes.json，否则关于页缺少当前版本。"""

    catalog = load_release_notes(
        ROOT / "resources" / "release_notes.json",
        current_version=app_version(),
    )

    assert catalog.entries, "升级日志目录为空，请运行 scripts/build_release_notes.py"
    assert catalog.covers(app_version()) is True
    assert catalog.entries[0].is_current is True
