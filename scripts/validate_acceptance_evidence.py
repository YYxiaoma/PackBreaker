from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "docs" / "v1-acceptance-evidence.json"
_ALLOWED_STATUSES = {"covered", "field_evidence", "pending_external"}


def validate_manifest(root: Path, manifest_path: Path) -> list[str]:
    errors: list[str] = []
    try:
        payload: Any = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"无法读取验收证据清单：{exc}"]

    if not isinstance(payload, dict) or payload.get("format_version") != 1:
        return ["验收证据清单 format_version 必须为 1"]
    requirements = payload.get("requirements")
    if not isinstance(requirements, list):
        return ["验收证据清单 requirements 必须为数组"]

    expected_ids = [f"V1-{index:03d}" for index in range(1, 26)]
    actual_ids = [item.get("id") for item in requirements if isinstance(item, dict)]
    if actual_ids != expected_ids:
        errors.append("验收证据必须按 V1-001..V1-025 完整且有序列出")

    testing_text = (root / "docs" / "testing.md").read_text(encoding="utf-8")
    for index, item in enumerate(requirements, start=1):
        if not isinstance(item, dict):
            errors.append(f"requirements[{index}] 必须为对象")
            continue
        requirement_id = item.get("id", f"requirements[{index}]")
        criterion = item.get("criterion")
        status = item.get("status")
        evidence = item.get("evidence")

        if not isinstance(criterion, str) or not criterion.strip():
            errors.append(f"{requirement_id}: criterion 不能为空")
        elif criterion not in testing_text:
            errors.append(f"{requirement_id}: criterion 未锚定到 docs/testing.md")
        if status not in _ALLOWED_STATUSES:
            errors.append(f"{requirement_id}: status 无效：{status!r}")
        if status == "pending_external" and not item.get("external_blocker"):
            errors.append(f"{requirement_id}: pending_external 必须说明 external_blocker")
        if not isinstance(evidence, list) or not evidence:
            errors.append(f"{requirement_id}: evidence 至少需要一项")
            continue

        for evidence_index, entry in enumerate(evidence, start=1):
            if not isinstance(entry, dict):
                errors.append(f"{requirement_id}: evidence[{evidence_index}] 必须为对象")
                continue
            relative = entry.get("path")
            anchor = entry.get("contains")
            if not isinstance(relative, str) or not relative:
                errors.append(f"{requirement_id}: evidence[{evidence_index}] path 无效")
                continue
            if not isinstance(anchor, str) or not anchor:
                errors.append(f"{requirement_id}: evidence[{evidence_index}] contains 无效")
                continue
            candidate = (root / relative).resolve()
            try:
                candidate.relative_to(root.resolve())
            except ValueError:
                errors.append(f"{requirement_id}: evidence 路径越界：{relative}")
                continue
            if not candidate.is_file():
                errors.append(f"{requirement_id}: evidence 文件不存在：{relative}")
                continue
            try:
                text = candidate.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                errors.append(
                    f"{requirement_id}: evidence 文件不可作为 UTF-8 读取：{relative}: {exc}"
                )
                continue
            if anchor not in text:
                errors.append(f"{requirement_id}: evidence 锚点失效：{relative} :: {anchor}")

    return errors


def main() -> int:
    manifest = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else DEFAULT_MANIFEST
    errors = validate_manifest(ROOT, manifest)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    payload = json.loads(manifest.read_text(encoding="utf-8"))
    counts = Counter(item["status"] for item in payload["requirements"])
    print(
        "v1.0 验收证据索引通过："
        f"covered={counts['covered']}，field_evidence={counts['field_evidence']}，"
        f"pending_external={counts['pending_external']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
