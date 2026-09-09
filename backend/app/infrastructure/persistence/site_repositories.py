from datetime import datetime
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from backend.app.infrastructure.persistence.models import Site, new_uuid, utc_now


class SiteRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def list_all(self) -> list[Site]:
        return list(self._session.scalars(select(Site).order_by(Site.name, Site.id)))

    def get(self, site_id: str) -> Site | None:
        return self._session.get(Site, site_id)

    def create(
        self,
        *,
        name: str,
        kind: str,
        base_url: str,
        credential_kind: str,
        secret_id: str | None,
    ) -> Site:
        now = utc_now()
        record = Site(
            id=new_uuid(),
            name=name,
            type=kind,
            base_url=base_url,
            credential_kind=credential_kind,
            secret_id=secret_id,
            capabilities={},
            connection_status="UNTESTED",
            enabled=False,
            version=1,
            last_test_at=None,
            created_at=now,
            updated_at=now,
        )
        self._session.add(record)
        self._session.flush()
        return record

    def update_config(
        self,
        site_id: str,
        *,
        expected_version: int,
        values: dict[str, Any],
    ) -> bool:
        payload = dict(values)
        payload["version"] = expected_version + 1
        payload["updated_at"] = utc_now()
        updated = self._session.scalar(
            update(Site)
            .where(Site.id == site_id, Site.version == expected_version)
            .values(**payload)
            .returning(Site.id)
        )
        return updated is not None

    def update_connection_probe(
        self,
        site_id: str,
        *,
        expected_version: int,
        status: str,
        capabilities: dict[str, Any],
        tested_at: datetime,
    ) -> bool:
        updated = self._session.scalar(
            update(Site)
            .where(Site.id == site_id, Site.version == expected_version)
            .values(
                connection_status=status,
                capabilities=dict(capabilities),
                last_test_at=tested_at,
                updated_at=utc_now(),
            )
            .returning(Site.id)
        )
        return updated is not None

    def delete(self, site_id: str, *, expected_version: int) -> bool:
        deleted = self._session.scalar(
            delete(Site)
            .where(Site.id == site_id, Site.version == expected_version)
            .returning(Site.id)
        )
        return deleted is not None
