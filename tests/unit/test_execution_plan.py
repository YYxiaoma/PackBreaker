import json
from datetime import UTC, datetime

import pytest

from backend.app.domain.execution_plan import (
    ExecutionPlanAction,
    ExecutionPlanActionKind,
    ExecutionPlanBlockReason,
    ExecutionPlanSnapshot,
    execution_plan_to_payload,
)
from backend.app.domain.verification import FileSnapshot, VerificationLevel


def _file_snapshot() -> FileSnapshot:
    return FileSnapshot(device=1, inode=2, size=16, mtime_ns=3, file_type="regular")


def _plan(**overrides: object) -> ExecutionPlanSnapshot:
    values: dict[str, object] = {
        "task_id": "task-1",
        "task_version": 7,
        "task_unit_id": "unit-1",
        "execution_gate_id": "gate-1",
        "execution_gate_digest": "a" * 64,
        "preflight_snapshot_id": "preflight-1",
        "review_revision_id": "review-1",
        "candidate_id": "candidate-1",
        "source_inventory_digest": "b" * 64,
        "metainfo_digest": "c" * 64,
        "verification_level": VerificationLevel.FULL_VERIFIED,
        "client_check_required": False,
        "source_root": "source/movie",
        "target_root": "seeding/movie",
        "target_device": 1,
        "target_downloader_id": "downloader-1",
        "target_downloader_version": 3,
        "target_downloader_binding_digest": "d" * 64,
        "target_remote_save_path": "/downloads/seeding/movie",
        "actions": (
            ExecutionPlanAction(
                torrent_path="Movie.2026.mkv",
                kind=ExecutionPlanActionKind.HARDLINK,
                length=16,
                source_relative_path="Movie.2026.mkv",
                source_snapshot=_file_snapshot(),
            ),
        ),
        "create_directories": (),
        "estimated_download_bytes_upper_bound": 0,
        "blocked_reasons": (),
        "created_at": datetime(2026, 9, 10, tzinfo=UTC),
    }
    values.update(overrides)
    return ExecutionPlanSnapshot(**values)  # type: ignore[arg-type]


def test_plan_digest_is_stable_across_created_at() -> None:
    first = _plan(created_at=datetime(2026, 9, 10, 1, tzinfo=UTC))
    second = _plan(created_at=datetime(2026, 9, 10, 2, tzinfo=UTC))

    assert first.plan_digest == second.plan_digest


@pytest.mark.parametrize("value", ["/absolute", "../escape", "C:/escape", "a\\b"])
def test_plan_rejects_unsafe_paths(value: str) -> None:
    with pytest.raises(ValueError, match="POSIX|不安全"):
        _plan(target_root=value)


def test_hardlink_action_requires_matching_source_snapshot() -> None:
    with pytest.raises(ValueError, match="源相对路径"):
        ExecutionPlanAction(
            torrent_path="Movie.mkv",
            kind=ExecutionPlanActionKind.HARDLINK,
            length=16,
        )
    with pytest.raises(ValueError, match="长度"):
        ExecutionPlanAction(
            torrent_path="Movie.mkv",
            kind=ExecutionPlanActionKind.HARDLINK,
            length=15,
            source_relative_path="Movie.mkv",
            source_snapshot=_file_snapshot(),
        )


def test_plan_payload_exposes_only_logical_source_path() -> None:
    payload = execution_plan_to_payload(_plan())
    encoded = json.dumps(payload, ensure_ascii=False)

    assert payload["execution_allowed"] is False
    assert payload["side_effects_started"] is False
    assert payload["ready"] is True
    assert "Movie.2026.mkv" in encoded
    assert "/workspace" not in encoded
    assert "/data/" not in encoded


def test_blocked_plan_is_not_ready() -> None:
    plan = _plan(blocked_reasons=(ExecutionPlanBlockReason.TARGET_EXISTS,))

    assert plan.ready is False
    assert execution_plan_to_payload(plan)["ready"] is False


def test_plan_rejects_inconsistent_verification_gate_semantics() -> None:
    with pytest.raises(ValueError, match="client_check_required"):
        _plan(
            verification_level=VerificationLevel.CLIENT_CHECK_REQUIRED,
            client_check_required=False,
        )

    with pytest.raises(ValueError, match="验证等级"):
        _plan(verification_level=VerificationLevel.BLOCKED)


def test_full_verified_plan_rejects_client_fetch_actions() -> None:
    with pytest.raises(ValueError, match="CLIENT_FETCH"):
        _plan(
            actions=(
                ExecutionPlanAction(
                    torrent_path="missing.nfo",
                    kind=ExecutionPlanActionKind.CLIENT_FETCH,
                    length=128,
                ),
            )
        )
