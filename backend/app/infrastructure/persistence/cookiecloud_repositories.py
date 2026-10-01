from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.infrastructure.persistence.models import CookieCloudSetting, utc_now


class CookieCloudSettingRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self) -> CookieCloudSetting | None:
        return self._session.get(CookieCloudSetting, "default")

    def lock_current(self) -> CookieCloudSetting | None:
        return self._session.scalar(
            select(CookieCloudSetting).where(CookieCloudSetting.id == "default").with_for_update()
        )

    def create_default(self) -> CookieCloudSetting:
        existing = self.get()
        if existing is not None:
            return existing
        now = utc_now()
        record = CookieCloudSetting(
            id="default",
            enabled=False,
            server_url="",
            uuid="",
            password_secret_id=None,
            auto_sync=True,
            sync_interval_minutes=30,
            request_timeout_seconds=15,
            connection_status="UNTESTED",
            last_test_at=None,
            last_sync_at=None,
            last_sync_status="NEVER",
            last_sync_error_code=None,
            matched_sites=0,
            updated_sites=0,
            unmatched_domains=0,
            version=1,
            created_at=now,
            updated_at=now,
        )
        self._session.add(record)
        self._session.flush()
        return record
