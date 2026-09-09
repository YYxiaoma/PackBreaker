from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
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


def _assert_same(expected: Path, actual: Path, label: str) -> None:
    if expected.read_bytes() != actual.read_bytes():
        raise RuntimeError(f"{label} 已漂移，请重新生成并提交")


def _check_generated_contract() -> None:
    corepack = _tool("corepack")
    with tempfile.TemporaryDirectory(prefix="packbreaker-openapi-") as directory:
        temporary = Path(directory)
        openapi = temporary / "openapi.json"
        schema = temporary / "schema.ts"
        _run([sys.executable, str(ROOT / "scripts" / "export_openapi.py"), str(openapi)])
        _assert_same(FRONTEND / "openapi.json", openapi, "OpenAPI snapshot")
        _run(
            [corepack, "pnpm", "exec", "openapi-typescript", str(openapi), "-o", str(schema)],
            cwd=FRONTEND,
        )
        _run(
            [
                corepack,
                "pnpm",
                "exec",
                "prettier",
                "--config",
                ".prettierrc.json",
                "--write",
                str(schema),
            ],
            cwd=FRONTEND,
        )
        _assert_same(
            FRONTEND / "src" / "api" / "generated" / "schema.ts", schema, "OpenAPI TypeScript 类型"
        )


def main() -> None:
    _run([_tool("ruff"), "format", "--check", "."])
    _run([_tool("ruff"), "check", "."])
    _run([_tool("mypy"), "backend", "tests", "scripts"])
    _run([_tool("corepack"), "pnpm", "lint"], cwd=FRONTEND)
    _run([_tool("corepack"), "pnpm", "typecheck"], cwd=FRONTEND)
    _check_generated_contract()


if __name__ == "__main__":
    main()
