"""Synthetic end-to-end chain: proposal + pinned filesystem + real journal ORM."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from backend.app.application.unpack_scoped_descriptor_evidence import (
    ScopedDescriptorEvidence,
    inspect_scoped_descriptor_evidence,
)
from backend.app.application.unpack_scoped_readonly_chain import (
    ScopedChainReport,
    ScopedChainStatus,
    inspect_pristine_scoped_carpt_chain,
)
from backend.app.application.unpack_scoped_write_proposal import ScopedWriteProposalInspector
from backend.app.domain.unpack_execution_plan import (
    unpack_execution_plan_from_payload,
    unpack_execution_plan_to_payload,
)
from backend.app.infrastructure.persistence.models import (
    UnpackExecution,
    UnpackExecutionItem,
    UnpackExternalOperationJournal,
)
from backend.app.infrastructure.source_inventory import current_file_snapshot
from tests.api.test_unpack_v112_scoped_journal_audit import _journal
from tests.api.test_unpack_v112_scoped_write_proposal import _NOW, _rewrite, _setup


def _scenario(tmp_path: Path) -> tuple[Any, Any, dict[str, Any], str, str, Path, Path, Path]:
    client, app, proposal, execution_id, item_id = _setup(tmp_path)
    source_dir = Path(proposal["source_directory"])
    source_dir.mkdir()
    source = source_dir / proposal["source_filename"]
    source.write_bytes(b"fake test bytes, NEVER real media")
    output = Path(proposal["output_directory"])
    output.mkdir()
    with app.state.runtime.session_factory() as session:
        item = session.get(UnpackExecutionItem, item_id)
        assert item is not None and item.execution_plan is not None
        original = unpack_execution_plan_from_payload(item.execution_plan)
        action = replace(
            original.actions[0],
            length=source.stat().st_size,
            source_snapshot=current_file_snapshot(source),
        )
        plan = replace(
            original,
            target_device=output.stat().st_dev,
            actions=(action,),
            plan_digest="",
        )
        item.execution_plan = unpack_execution_plan_to_payload(plan)
        item.execution_plan_digest = plan.plan_digest
        proposal["plan_digest"] = plan.plan_digest
        proposal["target_device"] = plan.target_device
        session.commit()
    _rewrite(app, proposal)
    return client, app, proposal, execution_id, item_id, output.parent, output, source


def _inspect(
    app: Any,
    execution_id: str,
    item_id: str,
    parent: Path,
    output: Path,
    *,
    external_journal_insert_expected: bool = False,
) -> ScopedChainReport:
    inspector = ScopedWriteProposalInspector(app.state.settings.config_dir)
    with app.state.runtime.session_factory() as session:
        before = {j.id for j in session.query(UnpackExternalOperationJournal).all()}
        result = inspect_pristine_scoped_carpt_chain(
            session,
            inspector,
            execution_id=execution_id,
            item_id=item_id,
            approved_parent=parent,
            expected_output=output,
            now=_NOW,
        )
        after = {j.id for j in session.query(UnpackExternalOperationJournal).all()}
        if external_journal_insert_expected:
            # Another synthetic connection intentionally writes mid-scan.
            assert len(after - before) == 1 and before <= after
        else:
            assert after == before
        assert result.writes_authorized is False
        assert result.permits_replay is False
        assert result.realtime_write_lease is False
        assert result.downloader_contacted is False
        assert result.media_content_read is False
        return result


def test_pristine_scope_with_real_synthetic_files_is_still_non_authorizing(
    tmp_path: Path,
) -> None:
    client, app, _, ex, it, parent, output, source = _scenario(tmp_path)
    try:
        before = source.read_bytes()
        result = _inspect(app, ex, it, parent, output)
        assert result.status is ScopedChainStatus.OBSERVED_ONLY_NO_GRANT
        assert result.source_count == 1 and result.copy_bytes == len(before)
        assert source.read_bytes() == before
        assert list(output.iterdir()) == []
        assert app.state.unpack_materialization_service.list_materializable_execution_ids() == ()
        assert app.state.unpack_seeding_service.list_seedable_execution_ids() == ()
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("source_filename", "other.mkv"),
        ("downloader_version", 77),
        ("plan_digest", "b" * 64),
        ("expires_at_utc", (_NOW - timedelta(minutes=1)).isoformat()),
    ],
)
def test_expired_or_changed_proposal_fails_closed(
    tmp_path: Path, field: str, replacement: str | int
) -> None:
    client, app, proposal, ex, it, parent, output, _ = _scenario(tmp_path)
    try:
        _rewrite(app, {**proposal, field: replacement})
        assert _inspect(app, ex, it, parent, output).status is ScopedChainStatus.BLOCKED_PROPOSAL
    finally:
        client.__exit__(None, None, None)


def test_wrong_trusted_parent_and_output_never_derive_trust_from_proposal(
    tmp_path: Path,
) -> None:
    client, app, _, ex, it, parent, output, _ = _scenario(tmp_path)
    try:
        assert (
            _inspect(app, ex, it, parent / "unapproved", output).status
            is ScopedChainStatus.BLOCKED_TRUSTED_TARGET
        )
        assert (
            _inspect(app, ex, it, parent, parent / "wrong").status
            is ScopedChainStatus.BLOCKED_PROPOSAL
        )
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize(
    "kind", ("source_replaced", "output_file", "output_missing", "output_symlink")
)
def test_real_filesystem_drift_fails_without_side_effects(tmp_path: Path, kind: str) -> None:
    client, app, _, ex, it, parent, output, source = _scenario(tmp_path)
    try:
        if kind == "source_replaced":
            source.write_bytes(b"different bytes after plan freeze")
        if kind == "output_file":
            (output / "unknown_file").write_bytes(b"unowned")
        if kind in {"output_missing", "output_symlink"}:
            output.rmdir()
            if kind == "output_symlink":
                output.symlink_to(parent / "isolated-source", target_is_directory=True)
        assert _inspect(app, ex, it, parent, output).status is ScopedChainStatus.BLOCKED_DESCRIPTOR
    finally:
        client.__exit__(None, None, None)


def test_old_readonly_execution_never_passes_new_scope(
    tmp_path: Path,
) -> None:
    client, app, _, ex, it, parent, output, _ = _scenario(tmp_path)
    try:
        with app.state.runtime.session_factory() as session:
            execution = session.get(UnpackExecution, ex)
            assert execution is not None
            execution.config_snapshot = {
                **execution.config_snapshot,
                "acceptance_mode": "READ_ONLY_SITE_ACCEPTANCE",
            }
            session.commit()
        assert _inspect(app, ex, it, parent, output).status is ScopedChainStatus.BLOCKED_PROPOSAL
    finally:
        client.__exit__(None, None, None)


def test_existing_intent_stays_blocked_not_resumed(
    tmp_path: Path,
) -> None:
    client, app, proposal, ex, it, parent, output, _ = _scenario(tmp_path)
    try:
        with app.state.runtime.session_factory() as session:
            session.add(
                _journal(
                    item=it,
                    digest=proposal["plan_digest"],
                    output=proposal["output_directory"],
                    downloader=proposal["downloader_id"],
                    save_path=proposal["target_remote_save_path"],
                    operation="UNPACK_EXEC_TRANSMISSION_ADD",
                    status="INTENT_RECORDED",
                )
            )
            session.commit()
        assert _inspect(app, ex, it, parent, output).status is ScopedChainStatus.BLOCKED_PROPOSAL
    finally:
        client.__exit__(None, None, None)


def test_scope_file_revoked_during_descriptor_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, app, proposal, ex, it, parent, output, _ = _scenario(tmp_path)
    try:
        original = inspect_scoped_descriptor_evidence

        def revoke_after_descriptor(*args: Any, **kwargs: Any) -> ScopedDescriptorEvidence:
            result = original(*args, **kwargs)
            _rewrite(app, {**proposal, "expires_at_utc": (_NOW - timedelta(minutes=1)).isoformat()})
            return result

        monkeypatch.setattr(
            "backend.app.application.unpack_scoped_readonly_chain.inspect_scoped_descriptor_evidence",
            revoke_after_descriptor,
        )
        assert _inspect(app, ex, it, parent, output).status is ScopedChainStatus.BLOCKED_PROPOSAL
    finally:
        client.__exit__(None, None, None)


def test_version_drift_after_fixture_is_blocked(
    tmp_path: Path,
) -> None:
    client, app, _, ex, it, parent, output, _ = _scenario(tmp_path)
    try:
        with app.state.runtime.session_factory() as session:
            item = session.get(UnpackExecutionItem, it)
            assert item is not None
            item.version += 1
            session.commit()
        assert _inspect(app, ex, it, parent, output).status is ScopedChainStatus.BLOCKED_PROPOSAL
    finally:
        client.__exit__(None, None, None)


def test_journal_inserted_while_descriptor_scan_is_not_observed_as_pristine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, app, proposal, ex, it, parent, output, _ = _scenario(tmp_path)
    try:
        original = inspect_scoped_descriptor_evidence

        def insert_intent(*args: Any, **kwargs: Any) -> ScopedDescriptorEvidence:
            result = original(*args, **kwargs)
            with app.state.runtime.session_factory() as session:
                session.add(
                    _journal(
                        item=it,
                        digest=proposal["plan_digest"],
                        output=proposal["output_directory"],
                        downloader=proposal["downloader_id"],
                        save_path=proposal["target_remote_save_path"],
                        operation="UNPACK_EXEC_TRANSMISSION_ADD",
                        status="INTENT_RECORDED",
                    )
                )
                session.commit()
            return result

        monkeypatch.setattr(
            "backend.app.application.unpack_scoped_readonly_chain.inspect_scoped_descriptor_evidence",
            insert_intent,
        )
        assert (
            _inspect(app, ex, it, parent, output, external_journal_insert_expected=True).status
            is ScopedChainStatus.BLOCKED_PROPOSAL
        )
    finally:
        client.__exit__(None, None, None)


def test_frozen_plan_changed_during_descriptor_observation_is_not_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, app, _, ex, it, parent, output, _ = _scenario(tmp_path)
    try:
        original = inspect_scoped_descriptor_evidence

        def change_plan(*args: Any, **kwargs: Any) -> ScopedDescriptorEvidence:
            result = original(*args, **kwargs)
            with app.state.runtime.session_factory() as session:
                item = session.get(UnpackExecutionItem, it)
                assert item is not None
                item.version += 1
                session.commit()
            return result

        monkeypatch.setattr(
            "backend.app.application.unpack_scoped_readonly_chain.inspect_scoped_descriptor_evidence",
            change_plan,
        )
        assert _inspect(app, ex, it, parent, output).status is ScopedChainStatus.BLOCKED_PROPOSAL
    finally:
        client.__exit__(None, None, None)
