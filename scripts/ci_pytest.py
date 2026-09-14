from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SUMMARY_PREFIXES = ("FAILED ", "ERROR ")
_MAX_ANNOTATION_CHARS = 8000


def _github_command_value(value: str) -> str:
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def main() -> int:
    process = subprocess.Popen(
        [sys.executable, "-m", "pytest"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert process.stdout is not None
    summaries: list[str] = []
    for line in process.stdout:
        print(line, end="", flush=True)
        stripped = line.rstrip("\r\n")
        if stripped.startswith(_SUMMARY_PREFIXES):
            summaries.append(stripped)

    exit_code = process.wait()
    if exit_code != 0 and os.environ.get("GITHUB_ACTIONS") == "true":
        summary = "\n".join(summaries[-20:])
        if not summary:
            summary = f"pytest exited with status {exit_code}; inspect authenticated job logs"
        print(
            "::error title=Backend pytest failed::"
            + _github_command_value(summary[:_MAX_ANNOTATION_CHARS]),
            flush=True,
        )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
