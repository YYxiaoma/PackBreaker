"""Independent host approval *evidence* reader for new CarPT acceptance.

A local JSON document is NOT proof of authority, a cryptographic grant,
a host-side policy decision, or an executable write permission. Even when
valid, these observations are permanently non-authorizing.
"""

from __future__ import annotations

import json
import os
import stat
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Literal

from backend.app.application.unpack_scoped_descriptor_evidence import _pin_directory
from backend.app.application.unpack_scoped_write_environment import _canonical_absolute

if TYPE_CHECKING:
    from backend.app.application.unpack_scoped_initial_intent import ScopedInitialIntentRequest

_SCHEMA = "packbreaker-v112-host-approval-evidence-v1"
_FIELDS = frozenset(
    {
        "schema",
        "execution_id",
        "item_id",
        "plan_digest",
        "operation_type",
        "target_path",
        "source_path",
        "relative_path",
        "idempotency_key",
        "approved_parent",
        "expected_output",
        "not_before_utc",
        "expires_at_utc",
        "revoked",
    }
)
_FileIdentity = tuple[int, int, int, int, int, int]


class HostEvidenceStatus(StrEnum):
    BLOCKED_ABSENT_OR_INVALID = "BLOCKED_ABSENT_OR_INVALID"
    BLOCKED_EXPIRED_OR_REVOKED = "BLOCKED_EXPIRED_OR_REVOKED"
    BLOCKED_SCOPE_MISMATCH = "BLOCKED_SCOPE_MISMATCH"
    OBSERVED_UNTRUSTED_NO_GRANT = "OBSERVED_UNTRUSTED_NO_GRANT"


@dataclass(frozen=True, slots=True)
class HostEvidenceResult:
    status: HostEvidenceStatus
    file_identity: _FileIdentity | None = None
    writes_authorized: Literal[False] = False
    host_approval_obtained: Literal[False] = False
    permits_replay: Literal[False] = False
    downloader_contacted: Literal[False] = False


def _file_identity(info: os.stat_result) -> _FileIdentity:
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_uid,
    )


def _canonical_path(path: str) -> bool:
    return (
        isinstance(path, str)
        and path.startswith("/")
        and path != "/"
        and "\x00" not in path
        and "\\" not in path
        and "//" not in path
        and str(PurePosixPath(path)) == path
        and all(segment not in {"", ".", ".."} for segment in path[1:].split("/"))
    )


class HostApprovalEvidenceInspector:
    """Read a separate, explicitly selected file; never issue a grant."""

    def __init__(self, evidence_path: Path) -> None:
        self.path = evidence_path

    def inspect(
        self,
        request: ScopedInitialIntentRequest,
        *,
        approved_parent: Path,
        expected_output: Path,
        now: datetime | None = None,
    ) -> HostEvidenceResult:
        result = HostEvidenceResult
        current = now or datetime.now(UTC)
        if current.tzinfo is None or not _canonical_absolute(self.path):
            return result(HostEvidenceStatus.BLOCKED_ABSENT_OR_INVALID)
        # O_NOFOLLOW on a whole pathname only protects its final component.
        # Pin *every* ancestor via openat(dir_fd) before opening the file.
        with ExitStack() as stack:
            try:
                parent_fd = _pin_directory(stack, self.path.parent)
                parent_before = os.fstat(parent_fd)
                fd = os.open(
                    self.path.name,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=parent_fd,
                )
                stack.callback(os.close, fd)
                initial = os.fstat(fd)
                if (
                    not stat.S_ISREG(initial.st_mode)
                    or stat.S_IMODE(initial.st_mode) & 0o077
                    or initial.st_uid != os.geteuid()
                    or initial.st_size < 2
                    or initial.st_size > 4096
                ):
                    return result(HostEvidenceStatus.BLOCKED_ABSENT_OR_INVALID)
                with os.fdopen(fd, "r", encoding="utf-8", closefd=False) as stream:
                    document = json.loads(stream.read(4097))
                final = os.fstat(fd)
                named = os.stat(self.path.name, dir_fd=parent_fd, follow_symlinks=False)
                if (
                    not stat.S_ISREG(named.st_mode)
                    or _file_identity(initial) != _file_identity(final)
                    or _file_identity(final) != _file_identity(named)
                ):
                    return result(HostEvidenceStatus.BLOCKED_ABSENT_OR_INVALID)
                # A pinned old dirfd can still refer to an unlinked ancestor.
                # Reopen from / and check parent and file name identities.
                with ExitStack() as again:
                    fresh_parent = _pin_directory(again, self.path.parent)
                    parent_after = os.fstat(fresh_parent)
                    fresh_name = os.stat(self.path.name, dir_fd=fresh_parent, follow_symlinks=False)
                    if (
                        (parent_before.st_dev, parent_before.st_ino)
                        != (parent_after.st_dev, parent_after.st_ino)
                        or not stat.S_ISREG(fresh_name.st_mode)
                        or _file_identity(final) != _file_identity(fresh_name)
                    ):
                        return result(HostEvidenceStatus.BLOCKED_ABSENT_OR_INVALID)
            except (OSError, UnicodeError, ValueError):
                return result(HostEvidenceStatus.BLOCKED_ABSENT_OR_INVALID)

        if (
            not isinstance(document, dict)
            or set(document) != _FIELDS
            or document.get("schema") != _SCHEMA
            or type(document.get("revoked")) is not bool
            or any(
                not isinstance(document.get(k), str) or not document[k]
                for k in _FIELDS - {"schema", "source_path", "relative_path", "revoked"}
            )
            or document["source_path"] is not None
            and not isinstance(document["source_path"], str)
            or document["relative_path"] is not None
            and not isinstance(document["relative_path"], str)
        ):
            return result(HostEvidenceStatus.BLOCKED_ABSENT_OR_INVALID)
        try:
            start = datetime.fromisoformat(document["not_before_utc"])
            expiry = datetime.fromisoformat(document["expires_at_utc"])
        except ValueError:
            return result(HostEvidenceStatus.BLOCKED_ABSENT_OR_INVALID)
        if (
            start.tzinfo is None
            or expiry.tzinfo is None
            or document["revoked"]
            or not start <= current < expiry
            or expiry - start > timedelta(minutes=15)
        ):
            return result(HostEvidenceStatus.BLOCKED_EXPIRED_OR_REVOKED)

        # A document is only evidence about exactly one requested intent; it
        # never supplies the trusted root or replaces the separate host policy.
        if (
            document["execution_id"] != request.execution_id
            or document["item_id"] != request.item_id
            or document["plan_digest"] != request.plan_digest
            or document["operation_type"] != request.operation_type
            or document["idempotency_key"] != request.idempotency_key
            or document["target_path"] != request.target_path
            or document["source_path"] != request.source_path
            or document["relative_path"] != request.relative_path
            or document["approved_parent"] != approved_parent.as_posix()
            or document["expected_output"] != expected_output.as_posix()
            or not all(
                _canonical_path(p)
                for p in (
                    document["approved_parent"],
                    document["expected_output"],
                    document["target_path"],
                )
            )
            or expected_output.parent != approved_parent
        ):
            return result(HostEvidenceStatus.BLOCKED_SCOPE_MISMATCH)
        return result(HostEvidenceStatus.OBSERVED_UNTRUSTED_NO_GRANT, _file_identity(final))
