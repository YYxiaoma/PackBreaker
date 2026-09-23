"""Classify CI checkout versus the immutable published release baseline.

Same-version post-release CI still exercises runtime, health and backup/restore;
cross-version upgrade and updater gates apply when the checkout is newer.
An older checkout is always rejected. The release-tag workflow has its own
strict baseline-to-latest-release requirement and never uses this skip.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from scripts.validate_release_baseline import load_release_baseline, project_version


def is_newer_release(candidate: str, baseline: str) -> bool:
    candidate_parts = tuple(int(part) for part in candidate.split("."))
    baseline_parts = tuple(int(part) for part in baseline.split("."))
    if len(candidate_parts) != 3 or len(baseline_parts) != 3:
        raise ValueError("CI 版本必须是三段数字 SemVer")
    if candidate_parts < baseline_parts:
        raise ValueError("CI 候选版本不能早于正式发布基线")
    return candidate_parts > baseline_parts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="判定是否需要跨版本升级和 updater CI")
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args(argv)
    try:
        candidate = project_version()
        baseline = load_release_baseline().version
        newer = is_newer_release(candidate, baseline)
    except (OSError, ValueError) as exc:
        parser.exit(2, f"CI release relation error: {exc}\n")
    if args.github_output is not None:
        with args.github_output.open("a", encoding="utf-8") as output:
            output.write(f"candidate_newer={'true' if newer else 'false'}\n")
    print("newer" if newer else "same")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
