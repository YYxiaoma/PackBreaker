from dataclasses import replace
from datetime import UTC, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app.application.overview_statistics import overview_statistics
from backend.app.domain.task_definition import TaskConflictPolicy, TaskStorageMode
from backend.app.domain.unpack_execution_plan import (
    UnpackExecutionAction,
    UnpackExecutionActionKind,
    UnpackExecutionPlan,
    unpack_execution_plan_to_payload,
)
from backend.app.domain.verification import FileSnapshot, VerificationLevel
from backend.app.infrastructure.persistence.base import Base
from backend.app.infrastructure.persistence.models import (
    MovieDedupFileInventory,
    MovieDedupJob,
    MovieDedupPair,
    UnpackExecutionItem,
    new_uuid,
)


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def _plan(item_id: str, *, downloader: str = "client") -> UnpackExecutionPlan:
    return UnpackExecutionPlan(
        item_id=item_id,
        item_version_before=1,
        candidate_id="candidate",
        candidate_generation=0,
        metainfo_digest="a" * 64,
        verification_level=VerificationLevel.FULL_VERIFIED,
        output_directory="output",
        storage_mode=TaskStorageMode.HARDLINK,
        conflict_policy=TaskConflictPolicy.VERIFY_REUSE_OR_STOP,
        target_device=1,
        target_downloader_id=downloader,
        target_downloader_version=1,
        target_downloader_binding_digest="b" * 64,
        target_remote_save_path="/synthetic/output",
        client_check_required=False,
        actions=(
            UnpackExecutionAction(
                torrent_path="movie.mkv",
                kind=UnpackExecutionActionKind.MATERIALIZE,
                length=1024,
                source_path="movie.mkv",
                source_snapshot=FileSnapshot(
                    device=1, inode=1, size=1024, mtime_ns=1, file_type="regular"
                ),
            ),
        ),
        create_directories=(),
        blocked_reasons=(),
        created_at=datetime.now(UTC),
    )


def test_empty_overview_does_not_invent_space_or_success() -> None:
    with _session() as session:
        result = overview_statistics(session)
    assert result["unpack_total"] == result["unpack_success"] == 0
    assert result["dedup_reclaimed_estimate_bytes"] == 0
    assert result["dedup_actual_reclaimed_bytes"] is None


def test_completed_volume_identity_missing_evidence_and_retry_outcomes() -> None:
    with _session() as session:
        items = []
        for status in ["COMPLETED"] * 4 + [
            "MATCH_ERROR",
            "MATCH_TIMEOUT",
            "CONTENT_MISMATCH",
            "EXECUTION_ERROR",
            "MATCHING",
            "REVIEW_REQUIRED",
            "NO_MATCH",
            "CANCELLED",
        ]:
            item = UnpackExecutionItem(
                id=new_uuid(),
                execution_id="execution",
                source_object_key=new_uuid(),
                source_snapshot={"private": "/not-for-api"},
                status=status,
                retry_count=9,
            )
            items.append(item)
            session.add(item)
        for index in range(3):
            plan = _plan(items[index].id, downloader="other" if index == 2 else "client")
            items[index].execution_plan = unpack_execution_plan_to_payload(plan)
            items[index].execution_plan_digest = plan.plan_digest
        session.commit()
        result = overview_statistics(session)
        assert result["unpack_total"] == 12
        assert result["unpack_success"] == 4
        assert result["unpack_failed"] == 4
        assert result["unpack_running"] == result["unpack_review"] == 1
        assert result["unpack_seeded_torrents"] == 2
        assert result["unpack_seeded_bytes"] == 2048
        assert result["unpack_volume_unknown"] == 1
        # Same item retry recovery changes the current outcome, never creates 9 successes.
        items[4].status = "COMPLETED"
        bad_plan = replace(_plan(items[4].id), item_id="different-item", plan_digest="")
        items[4].execution_plan = unpack_execution_plan_to_payload(bad_plan)
        items[4].execution_plan_digest = bad_plan.plan_digest
        session.commit()
        next_result = overview_statistics(session)
        assert next_result["unpack_total"] == 12
        assert next_result["unpack_failed"] == 3
        assert next_result["unpack_success"] == 5
        assert next_result["unpack_volume_unknown"] == 2
        assert "/not-for-api" not in str(next_result)


def test_dedup_multiple_sources_and_repeated_scans_do_not_multiply_savings() -> None:
    with _session() as session:
        for job_index, job_status in enumerate(["COMPLETED", "REVIEW_REQUIRED", "CANCELLED"]):
            job = MovieDedupJob(
                id=new_uuid(),
                name="synthetic",
                source_root="a",
                target_root="b",
                mode="HARDLINK",
                cross_filesystem_policy="STOP",
                status=job_status,
                phase="REVIEW" if job_status != "COMPLETED" else "COMPLETED",
                trace_id=new_uuid(),
            )
            session.add(job)
            session.flush()
            for pair_index, (inode, pair_status, amount) in enumerate(
                [
                    (10, "COMPLETED" if job_index == 0 else "READY", 4096),
                    (10, "READY", 4096),  # duplicate candidate for the same target
                    (20 + job_index, "READY", 8192),
                ]
            ):
                target = MovieDedupFileInventory(
                    id=new_uuid(),
                    job_id=job.id,
                    side="B",
                    relative_path=f"target-{pair_index}",
                    basename="target",
                    extension=".mkv",
                    device=1,
                    inode=inode,
                    size_bytes=8192,
                    allocated_bytes=amount,
                    link_count=1,
                    mtime_ns=1,
                    ctime_ns=1,
                    mode=0o600,
                    uid=1,
                    gid=1,
                    scan_status="FULL_HASHED",
                )
                session.add(target)
                session.flush()
                session.add(
                    MovieDedupPair(
                        job_id=job.id,
                        source_file_id=f"source-{pair_index}",
                        target_file_id=target.id,
                        match_level="FULL_HASH",
                        status=pair_status,
                        full_hash_match=True,
                        resolved_action="HARDLINK",
                        estimated_reclaimable_bytes=amount,
                    )
                )
        session.commit()
        result = overview_statistics(session)
        assert result["dedup_scan_records"] == 9
        assert result["dedup_completed_targets"] == 1
        assert result["dedup_reclaimed_estimate_bytes"] == 4096
        # Exclude target 10 (already completed), completed job and cancelled job.
        assert result["dedup_pending_estimate_bytes"] == 8192
        assert result["dedup_actual_reclaimed_bytes"] is None
