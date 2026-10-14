"""Proposal metadata checks: no real sites/clients, no write-worker authorization."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.application.unpack_scoped_write_proposal import (
    PROPOSED_WRITE_ACCEPTANCE_MARKER,
    ScopedWriteProposalInspector,
)
from backend.app.domain.task_definition import TaskConflictPolicy, TaskStorageMode
from backend.app.domain.unpack_execution_plan import (
    UnpackExecutionAction,
    UnpackExecutionActionKind,
    UnpackExecutionPlan,
    unpack_execution_plan_to_payload,
)
from backend.app.domain.verification import FileSnapshot, VerificationLevel
from backend.app.infrastructure.persistence.models import (
    Downloader,
    Site,
    UnpackExecution,
    UnpackExecutionItem,
    UnpackMatchCandidate,
)
from tests.api.test_unpack_v2 import (
    _authenticated_client,
    _create_ready_downloader,
    _create_ready_site,
    _seed_item_action_fixture,
)

_NOW = datetime(2026, 10, 14, 10, 0, tzinfo=UTC)


def _setup(tmp_path: Path) -> tuple[TestClient, FastAPI, dict[str, Any], str, str]:
    client, app = _authenticated_client(tmp_path)
    site_id = _create_ready_site(app)
    downloader_id = _create_ready_downloader(app)
    item_id, candidate_id = _seed_item_action_fixture(app, item_status="PLAN_PENDING")
    key = app.state.secret_store.put(kind="SITE_COOKIE", value=b"synthetic=1")
    source_dir = (tmp_path / "isolated-source").as_posix()
    source_name = "synthetic-video.mkv"
    output = (tmp_path / "isolated-target").as_posix()
    video = source_dir + "/" + source_name
    with app.state.runtime.session_factory() as session:
        site = session.get(Site, site_id)
        assert site is not None
        site.type = "CARPT"
        site.credential_kind = "COOKIE"
        site.secret_id = key
        site.enabled = False
        site.version = 1
        downloader = session.get(Downloader, downloader_id)
        assert downloader is not None
        downloader.type = "TRANSMISSION"
        downloader.enabled = True
        downloader.version = 1
        item = session.get(UnpackExecutionItem, item_id)
        assert item is not None
        execution = session.get(UnpackExecution, item.execution_id)
        candidate = session.get(UnpackMatchCandidate, candidate_id)
        assert execution is not None and candidate is not None
        execution.status = "EXECUTING"
        execution.trigger = "MANUAL"
        execution.config_snapshot = {
            "acceptance_mode": PROPOSED_WRITE_ACCEPTANCE_MARKER,
            "definition_id": execution.definition_id,
            "definition_version": 1,
            "trigger_kind": "MANUAL",
            "source_kind": "DIRECTORY",
            "execution_scope_kind": "SELECTED_MEDIA",
            "site_ids": [site_id],
            "source_config": {"directory_path": source_dir},
            "output_config": {
                "output_directory": output,
                "storage_mode": "COPY",
                "conflict_policy": "VERIFY_REUSE_OR_STOP",
                "target_downloader_id": downloader_id,
            },
            "retry": {"enabled": False, "max_retries": 0},
            "selected_sources": [
                {
                    "source_object_key": item.source_object_key,
                    "filename": source_name,
                    "canonical_path_hint": video,
                    "source_snapshot": {"synthetic": True},
                }
            ],
        }
        candidate.site_id = site_id
        candidate.verification_status = "VERIFIED"
        candidate.verification_level = "FULL_VERIFIED"
        candidate.metainfo_digest = "a" * 64
        candidate.raw_ref = {"site_config_id": site_id, "site_config_version": 1}
        item.content_verification_level = "FULL_VERIFIED"
        item.torrent_metainfo_digest = "a" * 64
        item.selected_candidate_id = candidate_id
        plan = UnpackExecutionPlan(
            item_id=item_id,
            item_version_before=item.version - 1,
            candidate_id=candidate_id,
            candidate_generation=item.candidate_generation,
            metainfo_digest="a" * 64,
            verification_level=VerificationLevel.FULL_VERIFIED,
            output_directory=output,
            storage_mode=TaskStorageMode.COPY,
            conflict_policy=TaskConflictPolicy.VERIFY_REUSE_OR_STOP,
            target_device=14,
            target_downloader_id=downloader_id,
            target_downloader_version=1,
            target_downloader_binding_digest="b" * 64,
            target_remote_save_path="/remote/isolated-target",
            client_check_required=True,
            actions=(
                UnpackExecutionAction(
                    torrent_path="movie/synthetic-video.mkv",
                    kind=UnpackExecutionActionKind.MATERIALIZE,
                    length=5,
                    source_path=video,
                    source_snapshot=FileSnapshot(
                        device=14, inode=2, size=5, mtime_ns=123, file_type="regular"
                    ),
                ),
            ),
            create_directories=("movie",),
            blocked_reasons=(),
            created_at=_NOW,
        )
        item.execution_plan = unpack_execution_plan_to_payload(plan)
        item.execution_plan_digest = plan.plan_digest
        values: dict[str, Any] = {
            "schema": "packbreaker-v112-scoped-write-proposal-v1",
            "execution_id": execution.id,
            "definition_id": execution.definition_id,
            "definition_version": 1,
            "item_id": item_id,
            "item_version_before": plan.item_version_before,
            "candidate_id": candidate_id,
            "candidate_generation": item.candidate_generation,
            "plan_digest": plan.plan_digest,
            "metainfo_digest": "a" * 64,
            "site_id": site_id,
            "site_version": 1,
            "downloader_id": downloader_id,
            "downloader_version": 1,
            "downloader_binding_digest": "b" * 64,
            "source_directory": source_dir,
            "source_filename": source_name,
            "output_directory": output,
            "target_remote_save_path": "/remote/isolated-target",
            "target_device": 14,
            "expires_at_utc": (_NOW + timedelta(minutes=20)).isoformat(),
        }
        execution_id = execution.id
        session.commit()
    inspector = ScopedWriteProposalInspector(app.state.settings.config_dir)
    inspector.path.write_text(json.dumps(values), encoding="utf-8")
    inspector.path.chmod(0o600)
    return client, app, values, execution_id, item_id


def _matches(app: FastAPI, execution_id: str, item_id: str) -> bool:
    inspector = ScopedWriteProposalInspector(app.state.settings.config_dir)
    with app.state.runtime.session_factory() as session:
        return inspector.matches_frozen_preflight(
            session, execution_id=execution_id, item_id=item_id, now=_NOW
        )


def _rewrite(app: FastAPI, values: dict[str, Any]) -> None:
    inspector = ScopedWriteProposalInspector(app.state.settings.config_dir)
    inspector.path.write_text(json.dumps(values), encoding="utf-8")
    inspector.path.chmod(0o600)


def test_exact_proposal_matches_only_frozen_metadata_and_never_unlocks_workers(
    tmp_path: Path,
) -> None:
    client, app, _, execution_id, item_id = _setup(tmp_path)
    try:
        assert _matches(app, execution_id, item_id)
        # A matching metadata proposal is NOT a permission.
        assert app.state.unpack_materialization_service.list_materializable_execution_ids() == ()
        assert app.state.unpack_auxiliary_service.list_auxiliary_execution_ids() == ()
        assert app.state.unpack_seeding_service.list_seedable_execution_ids() == ()
        assert (
            app.state.unpack_materialization_service.materialize_next_batch(
                execution_id
            ).processed_count
            == 0
        )
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("execution_id", "different-execution"),
        ("definition_version", 2),
        ("item_id", "another-item"),
        ("item_version_before", 1),
        ("candidate_id", "another-candidate"),
        ("candidate_generation", 100),
        ("plan_digest", "c" * 64),
        ("metainfo_digest", "d" * 64),
        ("site_version", 2),
        ("downloader_id", "other-downloader"),
        ("downloader_binding_digest", "e" * 64),
        ("target_device", 19),
        ("output_directory", "/other-target"),
        ("source_directory", "/other-source"),
        ("source_filename", "different.mkv"),
        ("source_filename", "bad\x00name.mkv"),
        ("source_directory", "/"),
        ("output_directory", "/"),
        ("target_remote_save_path", "/remote/bad\x00name"),
        ("target_remote_save_path", "/remote/other"),
        ("schema", "wrong-schema"),
        ("output_directory", "/tmp/../isolated-target"),
        ("source_filename", "../traversal"),
        ("definition_version", True),
        ("candidate_generation", -1),
        ("site_version", "1"),
        ("expires_at_utc", (_NOW - timedelta(minutes=1)).isoformat()),
        ("expires_at_utc", (_NOW + timedelta(hours=2)).isoformat()),
        ("expires_at_utc", "2026-10-14T10:01:00"),
    ],
)
def test_any_proposal_identity_expiry_or_type_change_fails_closed(
    tmp_path: Path, field: str, replacement: object
) -> None:
    client, app, values, execution_id, item_id = _setup(tmp_path)
    try:
        values[field] = replacement
        _rewrite(app, values)
        assert not _matches(app, execution_id, item_id)
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize(
    "change", ("readonly", "wrong_candidate", "site_enabled", "tr_disabled", "bad_plan", "journal")
)
def test_frozen_db_mutation_is_never_accepted(tmp_path: Path, change: str) -> None:
    client, app, values, execution_id, item_id = _setup(tmp_path)
    try:
        with app.state.runtime.session_factory() as session:
            execution = session.get(UnpackExecution, execution_id)
            item = session.get(UnpackExecutionItem, item_id)
            site = session.get(Site, values["site_id"])
            downloader = session.get(Downloader, values["downloader_id"])
            assert execution is not None and item is not None
            assert site is not None and downloader is not None
            if change == "readonly":
                execution.config_snapshot = {
                    **execution.config_snapshot,
                    "acceptance_mode": "READ_ONLY_SITE_ACCEPTANCE",
                }
            elif change == "wrong_candidate":
                item.selected_candidate_id = "other-candidate"
            elif change == "site_enabled":
                site.enabled = True
            elif change == "tr_disabled":
                downloader.enabled = False
            elif change == "bad_plan":
                item.execution_plan = {**(item.execution_plan or {}), "output_directory": "/other"}
            elif change == "journal":
                # Avoid inserting malformed real-operation details: an unrelated
                # temp journal record is enough to prove the initial preflight refuses.
                from backend.app.infrastructure.persistence.models import (
                    UnpackExternalOperationJournal,
                    new_uuid,
                )

                session.add(
                    UnpackExternalOperationJournal(
                        id=new_uuid(),
                        item_id=item_id,
                        operation_type="UNPACK_EXEC_MATERIALIZE_COPY",
                        status="INTENT_RECORDED",
                        idempotency_key=new_uuid(),
                        target={"synthetic": True},
                        intent={},
                        created_at=_NOW,
                        updated_at=_NOW,
                    )
                )
            session.commit()
        assert not _matches(app, execution_id, item_id)
    finally:
        client.__exit__(None, None, None)


def test_proposal_revocation_unreadable_permissions_symlink_and_oversize(
    tmp_path: Path,
) -> None:
    client, app, values, execution_id, item_id = _setup(tmp_path)
    try:
        scope = ScopedWriteProposalInspector(app.state.settings.config_dir)
        assert _matches(app, execution_id, item_id)
        scope.path.chmod(0o644)
        assert not _matches(app, execution_id, item_id)
        scope.path.chmod(0o600)
        scope.path.unlink()
        assert not _matches(app, execution_id, item_id)
        elsewhere = tmp_path / "elsewhere.json"
        elsewhere.write_text(json.dumps(values), encoding="utf-8")
        elsewhere.chmod(0o600)
        scope.path.symlink_to(elsewhere)
        assert not _matches(app, execution_id, item_id)
        scope.path.unlink()
        scope.path.write_text(" " * 4097)
        scope.path.chmod(0o600)
        assert not _matches(app, execution_id, item_id)
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize("bad_snapshot", [None, "malformed", ["not-an-object"], 15])
def test_malformed_frozen_execution_config_is_explicitly_denied(
    tmp_path: Path, bad_snapshot: object
) -> None:
    client, app, _, execution_id, item_id = _setup(tmp_path)
    try:
        with app.state.runtime.session_factory() as session:
            execution = session.get(UnpackExecution, execution_id)
            assert execution is not None
            execution.config_snapshot = bad_snapshot
            session.commit()
        assert not _matches(app, execution_id, item_id)
    finally:
        client.__exit__(None, None, None)
