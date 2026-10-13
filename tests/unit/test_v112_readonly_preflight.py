from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from backend.app.application.unpack_acceptance import READ_ONLY_ACCEPTANCE_MARKER
from backend.app.domain.execution_plan import ExecutionPlanBlockReason
from backend.app.domain.task_definition import TaskConflictPolicy, TaskStorageMode
from backend.app.domain.unpack_execution_plan import (
    UnpackExecutionAction,
    UnpackExecutionActionKind,
    UnpackExecutionPlan,
    unpack_execution_plan_to_payload,
)
from backend.app.domain.verification import VerificationLevel
from backend.app.infrastructure.source_inventory import current_file_snapshot
from scripts.v112_readonly_acceptance_preflight import PreflightFailure, inspect


@dataclass(frozen=True)
class Fixture:
    database: Path
    parent: Path
    output: Path
    source: Path
    execution_id: str = "synthetic-execution"


def _fixture(tmp_path: Path) -> Fixture:
    parent = tmp_path / "mounted"
    parent.mkdir()
    source = parent / "source.mkv"
    source.write_bytes(b"synthetic-only-source")
    output = parent / "isolated-output"
    db = tmp_path / "state.sqlite"
    snap = current_file_snapshot(source)
    plan = UnpackExecutionPlan(
        item_id="item-1",
        item_version_before=1,
        candidate_id="candidate-1",
        candidate_generation=2,
        metainfo_digest="a" * 64,
        verification_level=VerificationLevel.FULL_VERIFIED,
        output_directory=output.as_posix(),
        storage_mode=TaskStorageMode.COPY,
        conflict_policy=TaskConflictPolicy.VERIFY_REUSE_OR_STOP,
        target_device=snap.device,
        target_downloader_id="synthetic-downloader",
        target_downloader_version=1,
        target_downloader_binding_digest="b" * 64,
        target_remote_save_path="/synthetic-client-output",
        client_check_required=True,
        actions=(
            UnpackExecutionAction(
                torrent_path="Pack/video.mkv",
                kind=UnpackExecutionActionKind.MATERIALIZE,
                length=snap.size,
                source_path=source.as_posix(),
                source_snapshot=snap,
            ),
        ),
        create_directories=("Pack",),
        blocked_reasons=(ExecutionPlanBlockReason.TARGET_PARENT_UNSAFE,),
        created_at=datetime.now(UTC),
    )
    with sqlite3.connect(db) as conn:
        conn.executescript(
            """
            CREATE TABLE unpack_execution (
              id TEXT PRIMARY KEY, config_snapshot TEXT NOT NULL
            );
            CREATE TABLE unpack_execution_item (
              id TEXT PRIMARY KEY, execution_id TEXT NOT NULL, status TEXT NOT NULL,
              selected_candidate_id TEXT, candidate_generation INTEGER NOT NULL,
              execution_plan TEXT, execution_plan_digest TEXT
            );
            CREATE TABLE unpack_external_operation_journal (
              id TEXT PRIMARY KEY, item_id TEXT NOT NULL
            );
            """
        )
        conn.execute(
            "INSERT INTO unpack_execution VALUES (?, ?)",
            ("synthetic-execution", json.dumps({"acceptance_mode": READ_ONLY_ACCEPTANCE_MARKER})),
        )
        conn.execute(
            "INSERT INTO unpack_execution_item VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "item-1",
                "synthetic-execution",
                "PLAN_PENDING",
                "candidate-1",
                2,
                json.dumps(unpack_execution_plan_to_payload(plan)),
                plan.plan_digest,
            ),
        )
    return Fixture(db, parent, output, source)


def _inspect(fixture: Fixture) -> dict[str, Any]:
    return inspect(
        db_path=fixture.database,
        execution_id=fixture.execution_id,
        approved_parent=fixture.parent,
        expected_output=fixture.output,
    )


def test_missing_isolated_root_reports_metadata_only_and_never_writes(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    db_bytes = fixture.database.read_bytes()
    source_bytes = fixture.source.read_bytes()
    report = _inspect(fixture)

    assert report["status"] == "BLOCKED_OUTPUT_ROOT_MISSING"
    assert report["frozen_plan_ready"] is False
    assert report["frozen_blockers"] == ["TARGET_PARENT_UNSAFE"]
    assert report["writes_authorized"] is False
    assert report["client_or_site_contacted"] is False
    assert report["media_content_read"] is False
    assert report["checks"]["frozen_read_only_execution"] is True
    assert report["checks"]["source_snapshots_current"] is True
    assert report["checks"]["output_root_exists"] is False
    assert report["checks"]["no_external_operation_journals"] is True
    assert report["source_file_count"] == 1
    assert fixture.database.read_bytes() == db_bytes
    assert fixture.source.read_bytes() == source_bytes
    assert not fixture.output.exists()
    serialized = json.dumps(report)
    assert fixture.source.as_posix() not in serialized
    assert fixture.output.as_posix() not in serialized
    assert "synthetic-client-output" not in serialized


def test_existing_isolated_root_does_not_unfreeze_historical_plan(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    fixture.output.mkdir()
    report = _inspect(fixture)
    assert report["status"] == "BLOCKED_OTHER_PRECONDITION"
    assert report["checks"]["output_root_exists"] is True
    assert report["checks"]["output_root_safe"] is True
    assert report["frozen_plan_ready"] is False
    assert report["writes_authorized"] is False


def test_source_change_never_falsely_certifies_original_snapshot(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    fixture.source.write_bytes(b"changed-source")
    report = _inspect(fixture)
    assert report["status"] == "BLOCKED_OTHER_PRECONDITION"
    assert report["checks"]["source_snapshots_current"] is False


def test_unconfirmed_operation_evidence_never_reports_clean(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    with sqlite3.connect(fixture.database) as conn:
        conn.execute(
            "INSERT INTO unpack_external_operation_journal VALUES (?, ?)", ("op-1", "item-1")
        )
    report = _inspect(fixture)
    assert report["journal_count"] == 1
    assert report["checks"]["no_external_operation_journals"] is False
    assert report["status"] == "BLOCKED_OTHER_PRECONDITION"


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("candidate", "different", "PLAN_PROOF_MISMATCH"),
        ("marker", "NORMAL", "NOT_FROZEN_READONLY_ACCEPTANCE"),
        ("status", "COMPLETED", "ITEM_NOT_FROZEN_PLAN_PENDING"),
    ],
)
def test_mismatched_historical_identity_fails_closed(
    tmp_path: Path, field: str, value: str, error: str
) -> None:
    fixture = _fixture(tmp_path)
    with sqlite3.connect(fixture.database) as conn:
        if field == "candidate":
            conn.execute("UPDATE unpack_execution_item SET selected_candidate_id=?", (value,))
        elif field == "marker":
            conn.execute(
                "UPDATE unpack_execution SET config_snapshot=?",
                (json.dumps({"acceptance_mode": value}),),
            )
        else:
            conn.execute("UPDATE unpack_execution_item SET status=?", (value,))
    with pytest.raises(PreflightFailure, match=error):
        _inspect(fixture)


def test_exact_output_identity_and_no_symlink_are_mandatory(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    with pytest.raises(PreflightFailure, match="OUTPUT_IDENTITY_MISMATCH"):
        inspect(
            db_path=fixture.database,
            execution_id=fixture.execution_id,
            approved_parent=fixture.parent,
            expected_output=fixture.parent / "wrong-output",
        )
    innocent = tmp_path / "elsewhere"
    innocent.mkdir()
    fixture.output.symlink_to(innocent, target_is_directory=True)
    report = _inspect(fixture)
    assert report["checks"]["output_root_safe"] is False
    assert report["status"] == "BLOCKED_OTHER_PRECONDITION"
    assert not any(innocent.iterdir())


def test_database_symlinks_and_out_of_scope_outputs_are_rejected(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    symlink = tmp_path / "db-link"
    symlink.symlink_to(fixture.database)
    with pytest.raises(PreflightFailure, match="UNSAFE_DB_PATH"):
        inspect(
            db_path=symlink,
            execution_id=fixture.execution_id,
            approved_parent=fixture.parent,
            expected_output=fixture.output,
        )
    with pytest.raises(PreflightFailure, match="OUTPUT_NOT_EXACT_ISOLATED_CHILD"):
        inspect(
            db_path=fixture.database,
            execution_id=fixture.execution_id,
            approved_parent=fixture.parent,
            expected_output=tmp_path / "unapproved",
        )
