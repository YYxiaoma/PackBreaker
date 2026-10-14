"""Host approval evidence is independent input; NEVER permits live writes."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select

from backend.app.application import unpack_scoped_initial_intent as initial_intent
from backend.app.application.unpack_scoped_host_evidence import (
    HostApprovalEvidenceInspector,
    HostEvidenceStatus,
)
from backend.app.application.unpack_scoped_initial_intent import (
    ScopedInitialIntentReport,
    ScopedInitialIntentRequest,
    ScopedInitialIntentStatus,
    inspect_initial_scoped_intent_transaction,
)
from backend.app.infrastructure.persistence.models import UnpackExternalOperationJournal
from tests.api.test_unpack_v112_scoped_initial_intent import _first
from tests.api.test_unpack_v112_scoped_readonly_chain import _scenario
from tests.api.test_unpack_v112_scoped_write_proposal import _NOW


def _document(
    request: ScopedInitialIntentRequest,
    parent: Path,
    output: Path,
) -> dict[str, Any]:
    return {
        "schema": "packbreaker-v112-host-approval-evidence-v1",
        "execution_id": request.execution_id,
        "item_id": request.item_id,
        "plan_digest": request.plan_digest,
        "operation_type": request.operation_type,
        "target_path": request.target_path,
        "source_path": request.source_path,
        "relative_path": request.relative_path,
        "idempotency_key": request.idempotency_key,
        "approved_parent": parent.as_posix(),
        "expected_output": output.as_posix(),
        "not_before_utc": (_NOW - timedelta(minutes=1)).isoformat(),
        "expires_at_utc": (_NOW + timedelta(minutes=8)).isoformat(),
        "revoked": False,
    }


def _put(file: Path, payload: dict[str, Any]) -> None:
    file.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    file.chmod(0o600)


def _run(
    app: Any, path: Path, request: ScopedInitialIntentRequest, parent: Path, output: Path
) -> ScopedInitialIntentReport:
    from backend.app.application.unpack_scoped_write_proposal import ScopedWriteProposalInspector

    factory = app.state.runtime.session_factory
    with factory() as session:
        count_before = session.scalar(select(func.count(UnpackExternalOperationJournal.id)))
    result = inspect_initial_scoped_intent_transaction(
        factory,
        ScopedWriteProposalInspector(app.state.settings.config_dir),
        request,
        approved_parent=parent,
        expected_output=output,
        now=_NOW,
        host_evidence=HostApprovalEvidenceInspector(path),
    )
    with factory() as session:
        count_after = session.scalar(select(func.count(UnpackExternalOperationJournal.id)))
    assert count_before == count_after
    assert result.writes_authorized is False
    assert result.journal_created is False
    assert result.host_approval_obtained is False
    assert result.permits_replay is False
    assert result.downloader_contacted is False
    assert result.media_content_read is False
    return result


def test_matching_separate_host_evidence_is_not_a_write_grant(tmp_path: Path) -> None:
    client, app, proposal, ex, item, parent, output, source = _scenario(tmp_path)
    try:
        req = _first(ex, item, proposal)
        host_path = tmp_path / "separate-evidence.json"
        _put(host_path, _document(req, parent, output))
        before = source.read_bytes()
        result = _run(app, host_path, req, parent, output)
        assert result.status is ScopedInitialIntentStatus.HOST_EVIDENCE_ONLY_NO_GRANT
        assert source.read_bytes() == before
        assert list(output.iterdir()) == []
        # Entire evidence remains untrusted: 'host_approval_obtained' is False.
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "symlink",
        "readable-by-all",
        "bad-schema",
        "revoked",
        "expired",
        "not-yet-valid",
        "overlong",
        "wrong-execution",
        "wrong-plan",
        "wrong-key",
        "wrong-target",
        "wrong-output",
        "wrong-parent",
        "wrong-kind",
        "unknown-field",
        "bad-path",
        "file-too-large",
        "unbounded-duration",
        "bad-revoked-type",
    ],
)
def test_host_evidence_bad_inputs_block_first_intent(tmp_path: Path, mutation: str) -> None:
    client, app, proposal, ex, item, parent, output, _ = _scenario(tmp_path)
    try:
        req = _first(ex, item, proposal)
        host_path = tmp_path / "separate-evidence.json"
        document = _document(req, parent, output)
        if mutation == "bad-schema":
            document["schema"] = "arbitrary"
        elif mutation == "revoked":
            document["revoked"] = True
        elif mutation == "expired":
            document["expires_at_utc"] = (_NOW - timedelta(seconds=1)).isoformat()
        elif mutation == "not-yet-valid":
            document["not_before_utc"] = (_NOW + timedelta(minutes=1)).isoformat()
        elif mutation == "overlong":
            document["expires_at_utc"] = (_NOW + timedelta(hours=3)).isoformat()
        elif mutation == "unbounded-duration":
            document["not_before_utc"] = (_NOW - timedelta(minutes=20)).isoformat()
        elif mutation == "wrong-execution":
            document["execution_id"] = "another"
        elif mutation == "wrong-plan":
            document["plan_digest"] = "b" * 64
        elif mutation == "wrong-key":
            document["idempotency_key"] = "replay-from-elsewhere"
        elif mutation == "wrong-target":
            document["target_path"] = "/unauthorized/path"
        elif mutation == "wrong-output":
            document["expected_output"] = "/outside"
        elif mutation == "wrong-parent":
            document["approved_parent"] = "/not-trusted"
        elif mutation == "wrong-kind":
            document["operation_type"] = "UNPACK_EXEC_TRANSMISSION_ADD"
        elif mutation == "unknown-field":
            document["extra"] = "unexpected"
        elif mutation == "bad-path":
            document["target_path"] = "/bad\x00path"
        elif mutation == "bad-revoked-type":
            document["revoked"] = "false"
        if mutation not in {"missing", "symlink"}:
            _put(host_path, document)
        if mutation == "file-too-large":
            host_path.write_bytes(b"z" * 4097)
            host_path.chmod(0o600)
        elif mutation == "readable-by-all":
            host_path.chmod(0o644)
        elif mutation == "symlink":
            elsewhere = tmp_path / "elsewhere-evidence.json"
            _put(elsewhere, document)
            host_path.symlink_to(elsewhere)
        report = _run(app, host_path, req, parent, output)
        assert report.status is ScopedInitialIntentStatus.BLOCKED_HOST_EVIDENCE
        assert (
            HostApprovalEvidenceInspector(host_path)
            .inspect(req, approved_parent=parent, expected_output=output, now=_NOW)
            .status
            is not HostEvidenceStatus.OBSERVED_UNTRUSTED_NO_GRANT
        )
    finally:
        client.__exit__(None, None, None)


def test_identical_host_file_atomic_swap_while_sqlite_locked_is_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, app, proposal, ex, item, parent, output, _ = _scenario(tmp_path)
    try:
        req = _first(ex, item, proposal)
        host_path = tmp_path / "evidence.json"
        content = _document(req, parent, output)
        _put(host_path, content)
        original = initial_intent._inspect_first_intent_inside_locked_session

        def replace_during_scan(*args: Any, **kwargs: Any) -> ScopedInitialIntentReport:
            report = original(*args, **kwargs)
            replacement = tmp_path / "new-evidence.json"
            _put(replacement, content)
            os.replace(replacement, host_path)
            return report

        monkeypatch.setattr(
            initial_intent, "_inspect_first_intent_inside_locked_session", replace_during_scan
        )
        assert _run(app, host_path, req, parent, output).status is (
            ScopedInitialIntentStatus.BLOCKED_HOST_EVIDENCE
        )
    finally:
        client.__exit__(None, None, None)


def test_revoked_host_evidence_during_sqlite_locked_plan_check_is_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, app, proposal, ex, item, parent, output, _ = _scenario(tmp_path)
    try:
        req = _first(ex, item, proposal)
        host_path = tmp_path / "evidence.json"
        content = _document(req, parent, output)
        _put(host_path, content)
        original = initial_intent._inspect_first_intent_inside_locked_session

        def revoke_during_scan(*args: Any, **kwargs: Any) -> ScopedInitialIntentReport:
            report = original(*args, **kwargs)
            _put(host_path, {**content, "revoked": True})
            return report

        monkeypatch.setattr(
            initial_intent, "_inspect_first_intent_inside_locked_session", revoke_during_scan
        )
        assert _run(app, host_path, req, parent, output).status is (
            ScopedInitialIntentStatus.BLOCKED_HOST_EVIDENCE
        )
    finally:
        client.__exit__(None, None, None)


def test_host_evidence_mismatch_to_request_is_not_overridden_by_proposal(tmp_path: Path) -> None:
    client, app, proposal, ex, item, parent, output, _ = _scenario(tmp_path)
    try:
        req = _first(ex, item, proposal)
        host_path = tmp_path / "evidence.json"
        _put(host_path, _document(req, parent, output))
        changed = replace(req, idempotency_key="different-intent")
        assert _run(app, host_path, changed, parent, output).status is (
            ScopedInitialIntentStatus.BLOCKED_HOST_EVIDENCE
        )
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize("depth", ["parent", "ancestor"])
def test_host_evidence_parent_symlink_cannot_redirect_identical_document(
    tmp_path: Path, depth: str
) -> None:
    client, app, proposal, ex, item, parent, output, _ = _scenario(tmp_path)
    try:
        request = _first(ex, item, proposal)
        actual_dir = tmp_path / "evidence-source"
        actual_dir.mkdir()
        _put(actual_dir / "evidence.json", _document(request, parent, output))
        alias = tmp_path / "evidence-alias"
        alias.symlink_to(actual_dir, target_is_directory=True)
        path = alias / "evidence.json"
        if depth == "ancestor":
            path = alias / "nested" / "evidence.json"
            (actual_dir / "nested").mkdir()
            _put(actual_dir / "nested" / "evidence.json", _document(request, parent, output))
        assert _run(app, path, request, parent, output).status is (
            ScopedInitialIntentStatus.BLOCKED_HOST_EVIDENCE
        )
    finally:
        client.__exit__(None, None, None)


def test_host_evidence_parent_directory_replaced_during_openat_read_is_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from backend.app.application import unpack_scoped_host_evidence as host_module
    from backend.app.application.unpack_scoped_descriptor_evidence import (
        _pin_directory as pinned_directory_open,
    )

    client, app, proposal, ex, item, parent, output, _ = _scenario(tmp_path)
    try:
        request = _first(ex, item, proposal)
        evidence_dir = tmp_path / "evidence-dir"
        evidence_dir.mkdir()
        evidence_path = evidence_dir / "evidence.json"
        same_document = _document(request, parent, output)
        _put(evidence_path, same_document)
        original_pin = pinned_directory_open
        count = 0

        def replace_on_second_pin(stack: Any, path: Path) -> int:
            nonlocal count
            count += 1
            if count == 2:
                evidence_dir.rename(tmp_path / "moved-original-evidence-dir")
                evidence_dir.mkdir()
                _put(evidence_path, same_document)
            return original_pin(stack, path)

        monkeypatch.setattr(host_module, "_pin_directory", replace_on_second_pin)
        assert _run(app, evidence_path, request, parent, output).status is (
            ScopedInitialIntentStatus.BLOCKED_HOST_EVIDENCE
        )
        assert count == 2
    finally:
        client.__exit__(None, None, None)


def test_host_evidence_noncanonical_parent_path_is_rejected(tmp_path: Path) -> None:
    client, app, proposal, ex, item, parent, output, _ = _scenario(tmp_path)
    try:
        request = _first(ex, item, proposal)
        evidence_dir = tmp_path / "evidence-dir"
        evidence_dir.mkdir()
        evidence_path = evidence_dir / "evidence.json"
        _put(evidence_path, _document(request, parent, output))
        # Do not normalize /../ before the inspector sees the path.
        poisoned = Path(str(evidence_dir) + "/../evidence-dir/evidence.json")
        assert _run(app, poisoned, request, parent, output).status is (
            ScopedInitialIntentStatus.BLOCKED_HOST_EVIDENCE
        )
    finally:
        client.__exit__(None, None, None)
