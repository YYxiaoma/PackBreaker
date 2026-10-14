"""Cryptographically authenticated host evidence is still not host write permission.

Private signing keys exist only as ephemeral in-memory test fixtures. The
application is configured only with a separately pinned *public* key.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import func, select

from backend.app.application import unpack_scoped_initial_intent as initial_module
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
from backend.app.application.unpack_scoped_write_proposal import ScopedWriteProposalInspector
from backend.app.infrastructure.persistence.models import UnpackExternalOperationJournal
from tests.api.test_unpack_v112_scoped_host_evidence import _document
from tests.api.test_unpack_v112_scoped_initial_intent import _first
from tests.api.test_unpack_v112_scoped_readonly_chain import _scenario
from tests.api.test_unpack_v112_scoped_write_proposal import _NOW


def _pubkey(key: Ed25519PrivateKey) -> bytes:
    return key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def _signed(key: Ed25519PrivateKey, payload: dict[str, Any]) -> dict[str, Any]:
    message = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )
    return {
        "payload": payload,
        "signature": base64.b64encode(key.sign(message)).decode("ascii"),
    }


def _put(path: Path, envelope: dict[str, Any]) -> None:
    path.write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")
    path.chmod(0o600)


def _run(
    app: Any,
    path: Path,
    request: ScopedInitialIntentRequest,
    parent: Path,
    output: Path,
    pubkey: bytes,
) -> ScopedInitialIntentStatus:
    factory = app.state.runtime.session_factory
    with factory() as session:
        before = session.scalar(select(func.count(UnpackExternalOperationJournal.id)))
    result = inspect_initial_scoped_intent_transaction(
        factory,
        ScopedWriteProposalInspector(app.state.settings.config_dir),
        request,
        approved_parent=parent,
        expected_output=output,
        now=_NOW,
        host_evidence=HostApprovalEvidenceInspector(path, trusted_public_key=pubkey),
    )
    with factory() as session:
        after = session.scalar(select(func.count(UnpackExternalOperationJournal.id)))
    assert before == after
    assert result.writes_authorized is False
    assert result.host_approval_obtained is False
    assert result.journal_created is False
    assert result.downloader_contacted is False
    assert result.media_content_read is False
    assert result.permits_replay is False
    return result.status


def test_pinned_ed25519_signature_is_cryptographically_valid_but_no_write_grant(
    tmp_path: Path,
) -> None:
    client, app, proposal, ex, item, parent, output, source = _scenario(tmp_path)
    try:
        request = _first(ex, item, proposal)
        payload = _document(request, parent, output)
        payload["schema"] = "packbreaker-v112-host-evidence-signed-v1"
        signing_key = Ed25519PrivateKey.generate()
        path = tmp_path / "host-signature.json"
        _put(path, _signed(signing_key, payload))
        inspector = HostApprovalEvidenceInspector(path, trusted_public_key=_pubkey(signing_key))
        result = inspector.inspect(
            request, approved_parent=parent, expected_output=output, now=_NOW
        )
        assert result.status is HostEvidenceStatus.OBSERVED_SIGNED_NO_GRANT
        assert result.signature_verified is True
        assert result.host_approval_obtained is False
        assert result.writes_authorized is False
        source_before = source.read_bytes()
        assert _run(app, path, request, parent, output, _pubkey(signing_key)) is (
            ScopedInitialIntentStatus.HOST_SIGNED_EVIDENCE_ONLY_NO_GRANT
        )
        assert source.read_bytes() == source_before
        assert list(output.iterdir()) == []
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize(
    "corruption",
    [
        "wrong-key",
        "tampered-path",
        "tampered-item",
        "tampered-signature",
        "invalid-base64",
        "not-64-bytes",
        "missing-signature",
        "extra-envelope-key",
        "missing-pubkey",
        "short-pubkey",
        "replay-operation",
        "replay-idempotency",
        "expired",
        "revoked",
        "future-start",
        "different-trusted-root",
        "plain-unsigned-with-key",
    ],
)
def test_unsigned_tampered_expired_or_cross_scope_documents_are_not_accepted(
    tmp_path: Path, corruption: str
) -> None:
    client, app, proposal, ex, item, parent, output, _ = _scenario(tmp_path)
    try:
        request = _first(ex, item, proposal)
        key = Ed25519PrivateKey.generate()
        payload = _document(request, parent, output)
        payload["schema"] = "packbreaker-v112-host-evidence-signed-v1"
        envelope = _signed(key, payload)
        trusted_key = _pubkey(key)
        if corruption == "wrong-key":
            trusted_key = _pubkey(Ed25519PrivateKey.generate())
        elif corruption == "tampered-path":
            envelope["payload"]["target_path"] = "/elsewhere/different"
        elif corruption == "tampered-item":
            envelope["payload"]["item_id"] = "another"
        elif corruption == "tampered-signature":
            envelope["signature"] = base64.b64encode(os.urandom(64)).decode("ascii")
        elif corruption == "invalid-base64":
            envelope["signature"] = "not valid / base64!"
        elif corruption == "not-64-bytes":
            envelope["signature"] = base64.b64encode(b"a" * 9).decode("ascii")
        elif corruption == "missing-signature":
            del envelope["signature"]
        elif corruption == "extra-envelope-key":
            envelope["extra"] = "unknown"
        elif corruption == "missing-pubkey":
            trusted_key = b""
        elif corruption == "short-pubkey":
            trusted_key = b"a" * 31
        elif corruption == "replay-operation":
            envelope = _signed(key, {**payload, "operation_type": "UNPACK_EXEC_TRANSMISSION_ADD"})
        elif corruption == "replay-idempotency":
            envelope = _signed(key, {**payload, "idempotency_key": "different-key"})
        elif corruption == "expired":
            envelope = _signed(
                key, {**payload, "expires_at_utc": (_NOW - timedelta(seconds=1)).isoformat()}
            )
        elif corruption == "revoked":
            envelope = _signed(key, {**payload, "revoked": True})
        elif corruption == "future-start":
            envelope = _signed(
                key, {**payload, "not_before_utc": (_NOW + timedelta(minutes=1)).isoformat()}
            )
        elif corruption == "different-trusted-root":
            envelope = _signed(key, {**payload, "approved_parent": "/wrong-parent"})
        elif corruption == "plain-unsigned-with-key":
            envelope = payload
        path = tmp_path / "signed.json"
        _put(path, envelope)
        assert _run(app, path, request, parent, output, trusted_key) is (
            ScopedInitialIntentStatus.BLOCKED_HOST_EVIDENCE
        )
    finally:
        client.__exit__(None, None, None)


def test_duplicate_json_keys_are_rejected_even_when_signature_present(tmp_path: Path) -> None:
    client, app, proposal, ex, item, parent, output, _ = _scenario(tmp_path)
    try:
        request = _first(ex, item, proposal)
        key = Ed25519PrivateKey.generate()
        payload = _document(request, parent, output)
        payload["schema"] = "packbreaker-v112-host-evidence-signed-v1"
        envelope = _signed(key, payload)
        path = tmp_path / "duplicate.json"
        content = json.dumps(envelope)
        content = content[:-1] + ', "signature": "duplicate"}'
        path.write_text(content)
        path.chmod(0o600)
        assert _run(app, path, request, parent, output, _pubkey(key)) is (
            ScopedInitialIntentStatus.BLOCKED_HOST_EVIDENCE
        )
    finally:
        client.__exit__(None, None, None)


def test_signature_file_swapped_between_transaction_checks_is_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, app, proposal, ex, item, parent, output, _ = _scenario(tmp_path)
    try:
        request = _first(ex, item, proposal)
        key = Ed25519PrivateKey.generate()
        payload = _document(request, parent, output)
        payload["schema"] = "packbreaker-v112-host-evidence-signed-v1"
        envelope = _signed(key, payload)
        path = tmp_path / "signed.json"
        _put(path, envelope)
        core = initial_module._inspect_first_intent_inside_locked_session

        def replace_during_scan(*args: Any, **kwargs: Any) -> ScopedInitialIntentReport:
            report = core(*args, **kwargs)
            replacement = tmp_path / "replacement.json"
            _put(replacement, envelope)
            os.replace(replacement, path)
            return report

        monkeypatch.setattr(
            initial_module,
            "_inspect_first_intent_inside_locked_session",
            replace_during_scan,
        )
        assert _run(app, path, request, parent, output, _pubkey(key)) is (
            ScopedInitialIntentStatus.BLOCKED_HOST_EVIDENCE
        )
    finally:
        client.__exit__(None, None, None)


def test_signature_for_one_item_cannot_be_replayed_by_another_request(tmp_path: Path) -> None:
    client, app, proposal, ex, item, parent, output, _ = _scenario(tmp_path)
    try:
        request = _first(ex, item, proposal)
        key = Ed25519PrivateKey.generate()
        payload = _document(request, parent, output)
        payload["schema"] = "packbreaker-v112-host-evidence-signed-v1"
        path = tmp_path / "signed.json"
        _put(path, _signed(key, payload))
        changed = replace(request, idempotency_key="another-key")
        assert _run(app, path, changed, parent, output, _pubkey(key)) is (
            ScopedInitialIntentStatus.BLOCKED_HOST_EVIDENCE
        )
    finally:
        client.__exit__(None, None, None)
