from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "packbreaker-candidate-calibration-v1"


def _decode_json(value: object, *, default: object) -> object:
    if value is None:
        return default
    if isinstance(value, str):
        return json.loads(value)
    return value


def _sample_id(unit_key: str, site_id: str, torrent_id: str) -> str:
    payload = f"{unit_key}\n{site_id}\n{torrent_id}".encode()
    return hashlib.sha256(payload).hexdigest()[:16]


def _manual_decisions(connection: sqlite3.Connection) -> dict[str, str]:
    decisions: dict[str, str] = {}
    rows = connection.execute(
        """
        SELECT approved_candidate_id, rejected_candidate_ids
        FROM task_review_revision
        ORDER BY created_at, id
        """
    )
    for row in rows:
        approved = row["approved_candidate_id"]
        if isinstance(approved, str) and approved:
            decisions[approved] = "APPROVED"
        rejected = _decode_json(row["rejected_candidate_ids"], default=[])
        if isinstance(rejected, list):
            for candidate_id in rejected:
                if isinstance(candidate_id, str) and candidate_id:
                    decisions[candidate_id] = "REJECTED"
    return decisions


def build_report(database: Path) -> dict[str, Any]:
    uri = f"file:{database.resolve()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        decisions = _manual_decisions(connection)
        rows = list(
            connection.execute(
                """
                SELECT id, normalized_unit_key, site_id, torrent_id, score,
                       rejected, verification_level, error_code, evidence, created_at
                FROM task_candidate
                ORDER BY created_at, id
                """
            )
        )
    finally:
        connection.close()

    latest: dict[tuple[str, str, str], sqlite3.Row] = {}
    for row in rows:
        key = (row["normalized_unit_key"], row["site_id"], row["torrent_id"])
        latest[key] = row

    samples: list[dict[str, Any]] = []
    for (unit_key, site_id, torrent_id), row in sorted(latest.items()):
        evidence = _decode_json(row["evidence"], default={})
        score_evidence = evidence.get("score", {}) if isinstance(evidence, dict) else {}
        hard_conflicts = (
            score_evidence.get("hard_conflicts", []) if isinstance(score_evidence, dict) else []
        )
        dimensions = (
            score_evidence.get("dimensions", []) if isinstance(score_evidence, dict) else []
        )
        verification_level = row["verification_level"]
        if verification_level == "FULL_VERIFIED":
            evidence_label = "VERIFIED_CORRECT"
        elif verification_level == "CLIENT_CHECK_REQUIRED":
            evidence_label = "NEEDS_CLIENT_CHECK"
        elif verification_level == "BLOCKED":
            evidence_label = "BLOCKED_UNSAFE"
        else:
            evidence_label = "UNKNOWN"
        samples.append(
            {
                "sample_id": _sample_id(unit_key, site_id, torrent_id),
                "site_id": site_id,
                "score": row["score"],
                "algorithm_rejected": bool(row["rejected"]),
                "verification_level": verification_level,
                "evidence_label": evidence_label,
                "manual_decision": decisions.get(row["id"], "UNLABELED"),
                "error_code": row["error_code"],
                "hard_conflicts": hard_conflicts,
                "dimensions": dimensions,
            }
        )

    verified = [sample for sample in samples if sample["evidence_label"] == "VERIFIED_CORRECT"]
    incorrect: list[dict[str, Any]] = []
    client_check = [
        sample for sample in samples if sample["evidence_label"] == "NEEDS_CLIENT_CHECK"
    ]
    blocked = [sample for sample in samples if sample["evidence_label"] == "BLOCKED_UNSAFE"]
    unknown = [sample for sample in samples if sample["evidence_label"] == "UNKNOWN"]
    positive_scores = [float(sample["score"]) for sample in verified]

    return {
        "schema_version": SCHEMA_VERSION,
        "label_policy": {
            "verified_correct": "仅 FULL_VERIFIED 作为高置信内容正确标签",
            "incorrect": "当前数据库没有独立的人工内容错误标签；review REJECTED 不等同内容错误",
            "client_check": "CLIENT_CHECK_REQUIRED 单独统计，不当作错误标签",
            "blocked": "BLOCKED 表示不可安全自动使用，不当作内容错误标签",
        },
        "counts": {
            "raw_candidate_rows": len(rows),
            "unique_candidates": len(samples),
            "verified_correct": len(verified),
            "incorrect_labels": len(incorrect),
            "client_check_required": len(client_check),
            "blocked_unsafe": len(blocked),
            "unknown": len(unknown),
        },
        "score_summary": {
            "verified_correct_min": min(positive_scores) if positive_scores else None,
            "verified_correct_max": max(positive_scores) if positive_scores else None,
        },
        "threshold_recommendation": {
            "status": "INSUFFICIENT_NEGATIVE_LABELS",
            "recommended_threshold": None,
            "automatic_action_allowed": False,
            "reason": "没有内容错误负样本，无法估计自动批准的误报率；继续人工确认。",
        },
        "samples": samples,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="只读导出 PackBreaker 真实候选阈值标定证据")
    parser.add_argument("database", type=Path, help="PackBreaker SQLite 数据库路径；以只读模式打开")
    args = parser.parse_args()
    print(json.dumps(build_report(args.database), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
