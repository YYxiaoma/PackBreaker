"""Per-definition, time-bounded, read-only acceptance of a pending site.

The permission file is local, never a public API option or a production Registry
status change. It cannot authorize any filesystem or downloader mutation.
"""

from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.domain.site_config import SiteKind, SiteSupportStatus, site_profile
from backend.app.infrastructure.persistence.models import (
    Downloader,
    Site,
    UnpackDefinition,
    UnpackDefinitionSelectedSource,
    UnpackExecution,
)

_MARKER = "READ_ONLY_SITE_ACCEPTANCE"
_EXPECTED_FIELDS = frozenset(
    {
        "schema",
        "definition_id",
        "site_id",
        "site_version",
        "downloader_id",
        "source_directory",
        "source_filename",
        "output_directory",
        "expires_at_utc",
    }
)


class UnpackAcceptanceScope:
    """Zero privileges without an exact, private, non-symlink scope file."""

    def __init__(self, config_dir: Path) -> None:
        self.path = config_dir / "v112-unpack-readonly-acceptance.json"

    def _read(self) -> dict[str, object] | None:
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except FileNotFoundError:
            return None
        except OSError:
            return None
        try:
            st = os.fstat(fd)
            if (
                not stat.S_ISREG(st.st_mode)
                or stat.S_IMODE(st.st_mode) & 0o077
                or st.st_size > 4096
                or st.st_size < 1
            ):
                return None
            with os.fdopen(fd, "r", encoding="utf-8", closefd=False) as f:
                raw = f.read(4097)
            if len(raw) > 4096:
                return None
            value = json.loads(raw)
        except (OSError, UnicodeError, ValueError):
            return None
        finally:
            os.close(fd)
        if not isinstance(value, dict) or set(value) != _EXPECTED_FIELDS:
            return None
        if value["schema"] != "packbreaker-v112-readonly-site-acceptance-v1":
            return None
        strings = _EXPECTED_FIELDS - {"site_version"}
        if any(not isinstance(value[k], str) or not value[k] for k in strings):
            return None
        if type(value["site_version"]) is not int or value["site_version"] < 1:
            return None
        try:
            expires = datetime.fromisoformat(value["expires_at_utc"])
        except (TypeError, ValueError):
            return None
        now = datetime.now(UTC)
        if expires.tzinfo is None or not now < expires.astimezone(UTC) <= now + timedelta(hours=72):
            return None
        return value

    @staticmethod
    def is_acceptance_execution(execution: UnpackExecution) -> bool:
        return execution.config_snapshot.get("acceptance_mode") == _MARKER

    def authorize_definition(self, session: Session, definition: UnpackDefinition) -> bool:
        scope = self._read()
        if scope is None or definition.id != scope["definition_id"]:
            return False
        if (
            definition.trigger_kind != "MANUAL"
            or definition.source_kind != "DIRECTORY"
            or definition.execution_scope_kind != "SELECTED_MEDIA"
            or definition.status not in {"PENDING_EXECUTION", "PAUSED"}
            or definition.site_ids != [scope["site_id"]]
            or definition.source_config.get("directory_path") != scope["source_directory"]
            or definition.output_config.get("output_directory") != scope["output_directory"]
            or definition.output_config.get("target_downloader_id") != scope["downloader_id"]
            or definition.output_config.get("storage_mode") != "COPY"
            or definition.output_config.get("conflict_policy") != "VERIFY_REUSE_OR_STOP"
            or definition.retry_enabled
            or definition.max_retries
        ):
            return False
        sources = session.scalars(
            select(UnpackDefinitionSelectedSource).where(
                UnpackDefinitionSelectedSource.definition_id == definition.id
            )
        ).all()
        if len(sources) != 1 or sources[0].filename != scope["source_filename"]:
            return False
        if sources[0].canonical_path_hint != (
            str(scope["source_directory"]).rstrip("/") + "/" + str(scope["source_filename"])
        ):
            return False
        site = session.get(Site, scope["site_id"])
        client = session.get(Downloader, scope["downloader_id"])
        return bool(
            site is not None
            and site.type == SiteKind.CARPT.value
            and not site.enabled
            and site.version == scope["site_version"]
            and site.secret_id is not None
            and site.connection_status == "OK"
            and site_profile(SiteKind.CARPT).support_status
            is SiteSupportStatus.PENDING_REAL_VALIDATION
            and client is not None
            and client.type == "TRANSMISSION"
            and client.enabled
            and client.connection_status == "OK"
        )

    def authorize_execution(self, session: Session, execution: UnpackExecution) -> bool:
        if not self.is_acceptance_execution(execution):
            return False
        scope = self._read()
        if scope is None:
            return False
        definition = session.get(UnpackDefinition, execution.definition_id)
        if definition is None or not self.authorize_definition(session, definition):
            return False
        snapshot = execution.config_snapshot
        return (
            snapshot.get("definition_id") == definition.id
            and snapshot.get("definition_version") == definition.version
            and snapshot.get("trigger_kind") == "MANUAL"
            and snapshot.get("site_ids") == definition.site_ids
            and snapshot.get("source_config") == definition.source_config
            and snapshot.get("output_config") == definition.output_config
            and snapshot.get("retry") == {"enabled": False, "max_retries": 0}
            and isinstance(snapshot.get("selected_sources"), list)
            and len(snapshot["selected_sources"]) == 1
            and snapshot["selected_sources"][0].get("filename") == scope["source_filename"]
        )


READ_ONLY_ACCEPTANCE_MARKER = _MARKER


def is_readonly_acceptance_execution(
    session_factory: sessionmaker[Session], execution_id: str
) -> bool:
    """Frozen marker blocks ALL file/downloader writes regardless of scope revocation."""
    with session_factory() as session:
        execution = session.get(UnpackExecution, execution_id)
        return execution is not None and UnpackAcceptanceScope.is_acceptance_execution(execution)


def is_write_blocked_acceptance_execution(
    session_factory: sessionmaker[Session], execution_id: str
) -> bool:
    """Fail closed for ALL acceptance modes, including unknown/future ones.

    A write-capable acceptance mode must only be admitted through a separate,
    positive, execution-bound authorization gate. Until that gate exists and is
    explicitly enabled, any mode key (even null/empty) blocks worker side effects.
    Ordinary executions with no acceptance_mode key retain legacy behavior.
    """
    with session_factory() as session:
        execution = session.get(UnpackExecution, execution_id)
        if execution is None or not isinstance(execution.config_snapshot, dict):
            return True
        return "acceptance_mode" in execution.config_snapshot
