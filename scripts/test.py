from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"


def _tool(name: str) -> str:
    candidates = [
        ROOT / ".venv" / "bin" / name,
        ROOT / ".venv" / "Scripts" / f"{name}.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    resolved = shutil.which(name)
    if resolved is None:
        raise RuntimeError(f"缺少开发工具：{name}")
    return resolved


def _run(args: list[str], *, cwd: Path = ROOT) -> None:
    print("+", " ".join(args), flush=True)
    subprocess.run(args, cwd=cwd, check=True)


def main() -> None:
    _run([sys.executable, "-m", "pytest"])
    _run([_tool("corepack"), "pnpm", "test"], cwd=FRONTEND)
    _run([_tool("corepack"), "pnpm", "build"], cwd=FRONTEND)


if __name__ == "__main__":
    main()
