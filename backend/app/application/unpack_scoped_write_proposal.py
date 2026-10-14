"""Read-only inspection of a *proposed* CarPT write acceptance scope.

This is NOT a permit, not a grant and not connected to write workers.
Even a matching proposal cannot enable any filesystem/downloader side effect.
"""

from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.domain.site_config import SiteKind, SiteSupportStatus, site_profile
from backend.app.domain.unpack_execution_plan import unpack_execution_plan_from_payload
from backend.app.infrastructure.persistence.models import (
    Downloader,
    Site,
    UnpackExecution,
    UnpackExecutionItem,
    UnpackExternalOperationJournal,
    UnpackMatchCandidate,
)

PROPOSED_WRITE_ACCEPTANCE_MARKER = "SCOPED_WRITE_SITE_ACCEPTANCE"
_SCHEMA = "packbreaker-v112-scoped-write-proposal-v1"
_FIELDS = frozenset(
    {
        "schema",
        "execution_id",
        "definition_id",
        "definition_version",
        "item_id",
        "item_version_before",
        "candidate_id",
        "candidate_generation",
        "plan_digest",
        "metainfo_digest",
        "site_id",
        "site_version",
        "downloader_id",
        "downloader_version",
        "downloader_binding_digest",
        "source_directory",
        "source_filename",
        "output_directory",
        "target_remote_save_path",
        "target_device",
        "expires_at_utc",
    }
)
_INTS = frozenset(
    {
        "definition_version",
        "item_version_before",
        "candidate_generation",
        "site_version",
        "downloader_version",
        "target_device",
    }
)
_HEX_FIELDS = frozenset({"plan_digest", "metainfo_digest", "downloader_binding_digest"})


def _absolute_canonical(value: str) -> bool:
    if value == "/" or not value.startswith("/") or "\x00" in value or "\\" in value:
        return False
    path = PurePosixPath(value)
    return (
        str(path) == value
        and all(segment not in {".", ".."} for segment in value.split("/"))
        and "//" not in value
    )


ProposalFileIdentity = tuple[int, int, int, int, int]


def _proposal_identity(info: os.stat_result) -> ProposalFileIdentity:
    """File identity, including metadata change detectors (not a write lease)."""
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


class ScopedWriteProposalInspector:
    """Fail-closed metadata validator; matching never grants execution privileges."""

    def __init__(self, config_dir: Path) -> None:
        self.path = config_dir / "v112-unpack-scoped-write-proposal.json"

    def _read(self, *, now: datetime | None = None) -> dict[str, Any] | None:
        observed = self._read_with_identity(now=now)
        return observed[0] if observed is not None else None

    def _read_with_identity(
        self, *, now: datetime | None = None
    ) -> tuple[dict[str, Any], ProposalFileIdentity] | None:
        """Pin the read to one regular file, verifying its name still points to it."""
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError:
            return None
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or stat.S_IMODE(info.st_mode) & 0o077
                or info.st_size < 1
                or info.st_size > 4096
            ):
                return None
            with os.fdopen(fd, "r", encoding="utf-8", closefd=False) as stream:
                payload = json.loads(stream.read(4097))
            end_info = os.fstat(fd)
            named_info = self.path.lstat()
            if _proposal_identity(info) != _proposal_identity(end_info) or _proposal_identity(
                end_info
            ) != _proposal_identity(named_info):
                return None

        except (OSError, UnicodeError, ValueError):
            return None
        finally:
            os.close(fd)
        if not isinstance(payload, dict) or set(payload) != _FIELDS:
            return None
        if payload["schema"] != _SCHEMA:
            return None
        if any(type(payload[key]) is not int for key in _INTS):
            return None
        if (
            any(payload[key] < 1 for key in _INTS - {"candidate_generation", "target_device"})
            or payload["candidate_generation"] < 0
            or payload["target_device"] < 0
        ):
            return None
        if any(not isinstance(payload[key], str) or not payload[key] for key in _FIELDS - _INTS):
            return None
        if any(
            len(payload[key]) != 64
            or any(character not in "0123456789abcdef" for character in payload[key])
            for key in _HEX_FIELDS
        ):
            return None
        if not all(
            _absolute_canonical(payload[key])
            for key in ("source_directory", "output_directory", "target_remote_save_path")
        ):
            return None
        name = payload["source_filename"]
        if name in {".", ".."} or "/" in name or "\\" in name or "\x00" in name:
            return None
        if payload["source_directory"] == payload["output_directory"] or payload[
            "output_directory"
        ].startswith(payload["source_directory"] + "/"):
            return None
        try:
            expiry = datetime.fromisoformat(payload["expires_at_utc"])
        except ValueError:
            return None
        current = now or datetime.now(UTC)
        if (
            current.tzinfo is None
            or expiry.tzinfo is None
            or not current < expiry <= current + timedelta(hours=1)
        ):
            return None
        return payload, _proposal_identity(end_info)

    def matches_frozen_preflight(
        self,
        session: Session,
        *,
        execution_id: str,
        item_id: str,
        now: datetime | None = None,
    ) -> bool:
        """Read frozen DB and scope file only; true means *metadata matches*, not a grant."""
        proposal = self._read(now=now)
        if (
            proposal is None
            or proposal["execution_id"] != execution_id
            or proposal["item_id"] != item_id
        ):
            return False
        execution = session.get(UnpackExecution, execution_id)
        item = session.get(UnpackExecutionItem, item_id)
        if (
            execution is None
            or item is None
            or item.execution_id != execution.id
            or execution.trigger != "MANUAL"
            or execution.status != "EXECUTING"
            or item.status != "PLAN_PENDING"
            or not isinstance(execution.config_snapshot, dict)
            or execution.config_snapshot.get("acceptance_mode") != PROPOSED_WRITE_ACCEPTANCE_MARKER
            or execution.definition_id != proposal["definition_id"]
            or item.selected_candidate_id != proposal["candidate_id"]
            or item.candidate_generation != proposal["candidate_generation"]
            or item.execution_plan is None
            or item.execution_plan_digest != proposal["plan_digest"]
        ):
            return False
        snapshot = execution.config_snapshot
        selected = snapshot.get("selected_sources")
        source_dir = proposal["source_directory"]
        source_path = source_dir + "/" + proposal["source_filename"]
        output_config = snapshot.get("output_config")
        if (
            snapshot.get("definition_id") != execution.definition_id
            or snapshot.get("definition_version") != proposal["definition_version"]
            or snapshot.get("trigger_kind") != "MANUAL"
            or snapshot.get("source_kind") != "DIRECTORY"
            or snapshot.get("execution_scope_kind") != "SELECTED_MEDIA"
            or snapshot.get("retry") != {"enabled": False, "max_retries": 0}
            or snapshot.get("site_ids") != [proposal["site_id"]]
            or not isinstance(snapshot.get("source_config"), dict)
            or snapshot["source_config"].get("directory_path") != source_dir
            or not isinstance(output_config, dict)
            or output_config.get("output_directory") != proposal["output_directory"]
            or output_config.get("storage_mode") != "COPY"
            or output_config.get("conflict_policy") != "VERIFY_REUSE_OR_STOP"
            or output_config.get("target_downloader_id") != proposal["downloader_id"]
            or not isinstance(selected, list)
            or len(selected) != 1
            or not isinstance(selected[0], dict)
            or selected[0].get("filename") != proposal["source_filename"]
            or selected[0].get("canonical_path_hint") != source_path
            or selected[0].get("source_object_key") != item.source_object_key
        ):
            return False
        candidate = session.get(UnpackMatchCandidate, proposal["candidate_id"])
        if (
            candidate is None
            or candidate.item_id != item_id
            or candidate.generation != item.candidate_generation
            or candidate.site_id != proposal["site_id"]
            or candidate.verification_status != "VERIFIED"
            or candidate.verification_level != "FULL_VERIFIED"
            or candidate.metainfo_digest != proposal["metainfo_digest"]
            or not isinstance(candidate.raw_ref, dict)
            or candidate.raw_ref.get("site_config_id") != proposal["site_id"]
            or candidate.raw_ref.get("site_config_version") != proposal["site_version"]
            or item.content_verification_level != "FULL_VERIFIED"
            or item.torrent_metainfo_digest != proposal["metainfo_digest"]
        ):
            return False
        try:
            plan = unpack_execution_plan_from_payload(item.execution_plan)
        except (TypeError, ValueError):
            return False
        if (
            not plan.ready
            or plan.plan_digest != proposal["plan_digest"]
            or plan.plan_digest != item.execution_plan_digest
            or plan.item_id != item.id
            or plan.item_version_before != proposal["item_version_before"]
            or item.version != plan.item_version_before + 1
            or plan.candidate_id != candidate.id
            or plan.candidate_generation != item.candidate_generation
            or plan.metainfo_digest != proposal["metainfo_digest"]
            or plan.storage_mode.value != "COPY"
            or plan.conflict_policy.value != "VERIFY_REUSE_OR_STOP"
            or plan.output_directory != proposal["output_directory"]
            or plan.target_device != proposal["target_device"]
            or plan.target_downloader_id != proposal["downloader_id"]
            or plan.target_downloader_version != proposal["downloader_version"]
            or plan.target_downloader_binding_digest != proposal["downloader_binding_digest"]
            or plan.target_remote_save_path != proposal["target_remote_save_path"]
            or not any(action.source_path == source_path for action in plan.actions)
        ):
            return False
        site = session.get(Site, proposal["site_id"])
        downloader = session.get(Downloader, proposal["downloader_id"])
        if (
            site is None
            or site.type != SiteKind.CARPT.value
            or site.version != proposal["site_version"]
            or site.enabled
            or site.secret_id is None
            or site.connection_status != "OK"
            or site_profile(SiteKind.CARPT).support_status
            is not SiteSupportStatus.PENDING_REAL_VALIDATION
            or downloader is None
            or downloader.type != "TRANSMISSION"
            or not downloader.enabled
            or downloader.connection_status != "OK"
            or downloader.version != proposal["downloader_version"]
        ):
            return False
        # Initial inspection only. Any existing external intent requires a
        # different, operation-by-operation journal-aware recovery policy.
        count = session.scalar(
            select(func.count(UnpackExternalOperationJournal.id)).where(
                UnpackExternalOperationJournal.item_id == item_id
            )
        )
        return count == 0
