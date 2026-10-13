"""Read-only preflight for one frozen v1.1.2 site-acceptance execution.

Never calls site/download clients, opens media contents, creates directories or
updates SQLite. A favorable result does NOT authorize a write or resume a
READ_ONLY_SITE_ACCEPTANCE execution.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import stat
from pathlib import Path
from typing import Any
from urllib.parse import quote

from backend.app.application.unpack_acceptance import READ_ONLY_ACCEPTANCE_MARKER
from backend.app.domain.unpack_execution_plan import (
    UnpackExecutionActionKind,
    unpack_execution_plan_from_payload,
)
from backend.app.infrastructure.source_inventory import current_file_snapshot

_SCHEMA = "packbreaker-v112-readonly-preflight-v1"


class PreflightFailure(ValueError):
    """Fail closed without echoing private paths or raw DB/plan contents."""


def _safe_absolute(path: Path) -> Path:
    if not path.is_absolute() or ".." in path.parts or "\x00" in str(path):
        raise PreflightFailure("INVALID_ABSOLUTE_PATH")
    return path


def _components_safe(path: Path, *, require_exists: bool) -> bool:
    """Reject symlink components and non-directory ancestors, including mounts."""
    _safe_absolute(path)
    parts = tuple(reversed(path.parents)) + (path,)
    for current in parts:
        try:
            info = current.lstat()
        except FileNotFoundError:
            return not require_exists and current == path
        except OSError:
            return False
        if stat.S_ISLNK(info.st_mode):
            return False
        if current != path and not stat.S_ISDIR(info.st_mode):
            return False
    return True


def inspect(
    *,
    db_path: Path,
    execution_id: str,
    approved_parent: Path,
    expected_output: Path,
) -> dict[str, Any]:
    """Inspect historical proof and current metadata. No writes or media reads."""
    db_path = _safe_absolute(db_path)
    approved_parent = _safe_absolute(approved_parent)
    expected_output = _safe_absolute(expected_output)
    if not execution_id or len(execution_id) > 128:
        raise PreflightFailure("INVALID_EXECUTION_ID")
    if expected_output.parent != approved_parent or expected_output == approved_parent:
        raise PreflightFailure("OUTPUT_NOT_EXACT_ISOLATED_CHILD")
    if not _components_safe(db_path, require_exists=True):
        raise PreflightFailure("UNSAFE_DB_PATH")
    if not stat.S_ISREG(db_path.lstat().st_mode):
        raise PreflightFailure("DB_NOT_REGULAR")
    if not _components_safe(approved_parent, require_exists=True):
        raise PreflightFailure("UNSAFE_APPROVED_PARENT")

    # URL-escape literal ?/# in absolute paths so SQLite cannot reinterpret
    # the file path as URI query parameters. Both guards reject DB writes.
    db_uri = f"file:{quote(db_path.as_posix(), safe='/')}?mode=ro"
    try:
        connection = sqlite3.connect(db_uri, uri=True, timeout=3)
    except sqlite3.Error as exc:
        raise PreflightFailure("DB_READ_FAILED") from exc

    try:
        connection.execute("PRAGMA query_only=ON")
        execution = connection.execute(
            "SELECT config_snapshot FROM unpack_execution WHERE id = ?", (execution_id,)
        ).fetchone()
        if execution is None:
            raise PreflightFailure("EXECUTION_NOT_FOUND")
        config = json.loads(execution[0])
        if (
            not isinstance(config, dict)
            or config.get("acceptance_mode") != READ_ONLY_ACCEPTANCE_MARKER
        ):
            raise PreflightFailure("NOT_FROZEN_READONLY_ACCEPTANCE")

        items = connection.execute(
            "SELECT id, status, selected_candidate_id, candidate_generation, "
            "execution_plan, execution_plan_digest "
            "FROM unpack_execution_item WHERE execution_id = ? LIMIT 2",
            (execution_id,),
        ).fetchall()
        if len(items) != 1:
            raise PreflightFailure("EXPECTED_EXACTLY_ONE_ITEM")
        item_id, item_status, candidate_id, generation, raw_plan, db_digest = items[0]
        if item_status != "PLAN_PENDING" or not raw_plan:
            raise PreflightFailure("ITEM_NOT_FROZEN_PLAN_PENDING")
        try:
            plan = unpack_execution_plan_from_payload(json.loads(raw_plan))
        except (ValueError, TypeError) as exc:
            raise PreflightFailure("PLAN_INVALID") from exc
        if (
            plan.plan_digest != db_digest
            or plan.item_id != item_id
            or plan.candidate_id != candidate_id
            or plan.candidate_generation != generation
        ):
            raise PreflightFailure("PLAN_PROOF_MISMATCH")
        if Path(plan.output_directory) != expected_output:
            raise PreflightFailure("OUTPUT_IDENTITY_MISMATCH")
        operation_count = connection.execute(
            "SELECT count(*) FROM unpack_external_operation_journal WHERE item_id = ?",
            (item_id,),
        ).fetchone()[0]
    except (sqlite3.Error, json.JSONDecodeError) as exc:
        raise PreflightFailure("DB_OR_METADATA_INVALID") from exc
    finally:
        connection.close()

    # Exact root may be absent; the preflight cannot create it.
    output_exists = expected_output.exists()
    output_safe = _components_safe(expected_output, require_exists=output_exists)
    # lstat (not exists()) catches broken symlinks, which must be unsafe.
    try:
        info = expected_output.lstat()
    except FileNotFoundError:
        output_exists = False
    else:
        output_exists = True
        output_safe = output_safe and stat.S_ISDIR(info.st_mode)

    snapshots_current = True
    source_count = 0
    copy_bytes = 0
    for action in plan.actions:
        if action.kind is not UnpackExecutionActionKind.MATERIALIZE:
            continue
        source_count += 1
        copy_bytes += action.length
        if not action.source_path or not _components_safe(
            Path(action.source_path), require_exists=True
        ):
            snapshots_current = False
            continue
        try:
            actual = current_file_snapshot(Path(action.source_path))
        except (OSError, ValueError):
            snapshots_current = False
            continue
        if actual != action.source_snapshot:
            snapshots_current = False
    free_bytes = shutil.disk_usage(approved_parent).free
    checks = {
        "frozen_read_only_execution": True,
        "plan_digest_and_candidate_valid": True,
        "approved_parent_safe": True,
        "output_root_exists": output_exists,
        "output_root_safe": output_safe,
        "source_snapshots_current": snapshots_current,
        "disk_space_above_copy_bytes": free_bytes > copy_bytes,
        "no_external_operation_journals": operation_count == 0,
    }
    return {
        "schema": _SCHEMA,
        "execution_id": execution_id,
        "status": (
            "BLOCKED_OUTPUT_ROOT_MISSING"
            if (
                not output_exists
                and output_safe
                and snapshots_current
                and operation_count == 0
                and free_bytes > copy_bytes
                and [reason.value for reason in plan.blocked_reasons] == ["TARGET_PARENT_UNSAFE"]
            )
            else "BLOCKED_OTHER_PRECONDITION"
        ),
        "frozen_plan_ready": plan.ready,
        "frozen_blockers": [item.value for item in plan.blocked_reasons],
        "source_file_count": source_count,
        "copy_bytes": copy_bytes,
        "disk_free_bytes_at_check": free_bytes,
        "journal_count": operation_count,
        "checks": checks,
        "writes_authorized": False,
        "client_or_site_contacted": False,
        "media_content_read": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="v1.1.2 单次只读验收环境预检（绝不授权写入）")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--execution-id", required=True)
    parser.add_argument("--approved-parent", type=Path, required=True)
    parser.add_argument("--expected-output", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        report = inspect(
            db_path=arguments.db,
            execution_id=arguments.execution_id,
            approved_parent=arguments.approved_parent,
            expected_output=arguments.expected_output,
        )
    except (PreflightFailure, OSError, sqlite3.Error) as exc:
        code = str(exc) if isinstance(exc, PreflightFailure) else "PREFLIGHT_CHECK_FAILED"
        print(
            json.dumps(
                {
                    "schema": _SCHEMA,
                    "status": "INVALID",
                    "error_code": code,
                    "writes_authorized": False,
                }
            )
        )
        return 2
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
