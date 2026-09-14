from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from scripts.export_candidate_calibration import build_report


def test_candidate_calibration_requires_real_negative_labels_and_redacts_remote_id(
    tmp_path: Path,
) -> None:
    database = tmp_path / "calibration.db"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE task_candidate (
            id TEXT PRIMARY KEY,
            normalized_unit_key TEXT NOT NULL,
            site_id TEXT NOT NULL,
            torrent_id TEXT NOT NULL,
            score REAL NOT NULL,
            rejected INTEGER NOT NULL,
            verification_level TEXT,
            error_code TEXT,
            evidence TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE task_review_revision (
            id TEXT PRIMARY KEY,
            approved_candidate_id TEXT,
            rejected_candidate_ids TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """
    )
    evidence = json.dumps(
        {
            "score": {
                "hard_conflicts": [],
                "dimensions": [
                    {
                        "dimension": "TITLE",
                        "earned": 20.0,
                        "maximum": 20,
                        "evidence": "token Dice=1.0000",
                    }
                ],
            }
        }
    )
    connection.execute(
        "INSERT INTO task_candidate VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "candidate-positive",
            "unit-positive",
            "hhclub",
            "remote-positive-secret",
            53.0,
            0,
            "FULL_VERIFIED",
            None,
            evidence,
            "2026-09-14T00:00:00Z",
        ),
    )
    connection.execute(
        "INSERT INTO task_candidate VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "candidate-operational-reject",
            "unit-unknown",
            "mteam",
            "remote-rejected-secret",
            65.0,
            0,
            None,
            "SITE_TORRENT_FETCH_FAILED",
            evidence,
            "2026-09-14T00:01:00Z",
        ),
    )
    connection.execute(
        "INSERT INTO task_review_revision VALUES (?, ?, ?, ?)",
        (
            "review-1",
            "candidate-positive",
            json.dumps(["candidate-operational-reject"]),
            "2026-09-14T00:02:00Z",
        ),
    )
    connection.commit()
    connection.close()

    report = build_report(database)

    assert report["counts"] == {
        "raw_candidate_rows": 2,
        "unique_candidates": 2,
        "verified_correct": 1,
        "incorrect_labels": 0,
        "client_check_required": 0,
        "blocked_unsafe": 0,
        "unknown": 1,
    }
    assert report["threshold_recommendation"]["status"] == "INSUFFICIENT_NEGATIVE_LABELS"
    assert report["threshold_recommendation"]["recommended_threshold"] is None
    samples = report["samples"]
    assert any(sample["manual_decision"] == "REJECTED" for sample in samples)
    assert any(sample["evidence_label"] == "UNKNOWN" for sample in samples)
    serialized = json.dumps(report)
    assert "remote-positive-secret" not in serialized
    assert "remote-rejected-secret" not in serialized
