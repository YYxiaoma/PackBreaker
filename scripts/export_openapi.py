from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def export_openapi(output: Path) -> None:
    from backend.app.main import create_app

    schema = create_app().openapi()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="导出 PackBreaker OpenAPI schema")
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    export_openapi(args.output)


if __name__ == "__main__":
    main()
