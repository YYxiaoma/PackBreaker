from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    from backend.app.application.m2_corpus_acceptance import run_m2_corpus_acceptance
    from backend.app.domain.errors import DomainViolation

    parser = argparse.ArgumentParser(
        description="只读运行 PackBreaker M2 真实语料结构/映射稳定性验收"
    )
    parser.add_argument("torrent", type=Path, help="真实 torrent 文件路径；不会复制或写入仓库")
    parser.add_argument("source_root", type=Path, help="已挂载到容器中的真实源目录")
    parser.add_argument("--repeat", type=int, default=3, help="重复扫描/映射次数，至少 2 次")
    parser.add_argument("--max-files", type=int, default=100_000, help="源目录最大普通文件数量")
    parser.add_argument(
        "--verify-content",
        action="store_true",
        help="显式执行完整 piece/Merkle 内容验证；大包可能读取全部已映射媒体字节",
    )
    args = parser.parse_args()

    try:
        report = run_m2_corpus_acceptance(
            torrent_path=args.torrent,
            source_root=args.source_root,
            repeated_runs=args.repeat,
            max_files=args.max_files,
            verify_content=args.verify_content,
        )
    except (DomainViolation, ValueError) as exc:
        error_code = exc.code.value if isinstance(exc, DomainViolation) else "INVALID_ARGUMENT"
        payload = {
            "schema_version": "packbreaker-m2-corpus-acceptance-v1",
            "status": "ERROR",
            "error_code": error_code,
            "execution_allowed": False,
            "side_effects_started": False,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 2

    print(json.dumps(report.to_payload(), ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.status == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
