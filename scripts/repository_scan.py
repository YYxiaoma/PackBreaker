from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAX_TRACKED_FILE_BYTES = 5 * 1024 * 1024

_BLOCKED_FILENAMES = {".env", "secret.key"}
_BLOCKED_SUFFIXES = {
    ".avi",
    ".db",
    ".flac",
    ".key",
    ".log",
    ".m2ts",
    ".m4a",
    ".mkv",
    ".mov",
    ".mp3",
    ".mp4",
    ".p12",
    ".pem",
    ".pfx",
    ".sqlite",
    ".sqlite3",
    ".torrent",
    ".wav",
}
_CONTENT_RULES = (
    ("private-key", re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
    ("aws-access-key", re.compile(rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("github-token", re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{30,255}\b")),
    ("slack-token", re.compile(rb"\bxox[baprs]-[A-Za-z0-9-]{20,}\b")),
)


@dataclass(frozen=True, slots=True)
class Finding:
    path: str
    rule: str


def tracked_paths(root: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return [root / Path(item.decode()) for item in result.stdout.split(b"\0") if item]


def scan_paths(
    root: Path,
    paths: list[Path],
    *,
    max_file_bytes: int = MAX_TRACKED_FILE_BYTES,
) -> list[Finding]:
    findings: list[Finding] = []
    for path in paths:
        relative = path.relative_to(root).as_posix()
        if path.is_symlink() or not path.is_file():
            continue
        lowered_name = path.name.casefold()
        if lowered_name in _BLOCKED_FILENAMES or path.suffix.casefold() in _BLOCKED_SUFFIXES:
            findings.append(Finding(relative, "blocked-artifact"))
            continue
        size = path.stat().st_size
        if size > max_file_bytes:
            findings.append(Finding(relative, f"large-file>{max_file_bytes}"))
            continue
        content = path.read_bytes()
        for rule_name, pattern in _CONTENT_RULES:
            if pattern.search(content):
                findings.append(Finding(relative, rule_name))
    return findings


def main() -> None:
    findings = scan_paths(ROOT, tracked_paths(ROOT))
    if findings:
        print("仓库安全扫描失败：")
        for finding in findings:
            print(f"- {finding.path}: {finding.rule}")
        raise SystemExit(1)
    print("仓库安全扫描通过：未发现受禁制品、明显凭证或超限跟踪文件")


if __name__ == "__main__":
    main()
