from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from backend.app.infrastructure.persistence.models import Downloader, UnpackTask, new_uuid, utc_now


class DownloaderRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def list_all(self) -> list[Downloader]:
        return list(
            self._session.scalars(select(Downloader).order_by(Downloader.name, Downloader.id))
        )

    def get(self, downloader_id: str) -> Downloader | None:
        return self._session.get(Downloader, downloader_id)

    def create(
        self,
        *,
        name: str,
        kind: str,
        base_url: str,
        secret_id: str | None,
        monitor_rules: dict[str, Any],
        path_mappings: list[dict[str, str]],
    ) -> Downloader:
        now = utc_now()
        record = Downloader(
            id=new_uuid(),
            name=name,
            type=kind,
            base_url=base_url,
            secret_id=secret_id,
            monitor_rules=dict(monitor_rules),
            path_mappings=list(path_mappings),
            capabilities={},
            connection_status="UNTESTED",
            path_mapping_status="UNTESTED",
            enabled=False,
            version=1,
            last_test_at=None,
            last_path_diagnostic_at=None,
            created_at=now,
            updated_at=now,
        )
        self._session.add(record)
        self._session.flush()
        return record

    def update_config(
        self,
        downloader_id: str,
        *,
        expected_version: int,
        values: dict[str, Any],
    ) -> bool:
        payload = dict(values)
        payload["version"] = expected_version + 1
        payload["updated_at"] = utc_now()
        updated = self._session.scalar(
            update(Downloader)
            .where(Downloader.id == downloader_id, Downloader.version == expected_version)
            .values(**payload)
            .returning(Downloader.id)
        )
        return updated is not None

    def update_connection_probe(
        self,
        downloader_id: str,
        *,
        expected_version: int,
        status: str,
        capabilities: dict[str, Any],
        tested_at: datetime,
    ) -> bool:
        updated = self._session.scalar(
            update(Downloader)
            .where(Downloader.id == downloader_id, Downloader.version == expected_version)
            .values(
                connection_status=status,
                capabilities=dict(capabilities),
                last_test_at=tested_at,
                updated_at=utc_now(),
            )
            .returning(Downloader.id)
        )
        return updated is not None

    def update_path_probe(
        self,
        downloader_id: str,
        *,
        expected_version: int,
        status: str,
        tested_at: datetime,
    ) -> bool:
        updated = self._session.scalar(
            update(Downloader)
            .where(Downloader.id == downloader_id, Downloader.version == expected_version)
            .values(
                path_mapping_status=status,
                last_path_diagnostic_at=tested_at,
                updated_at=utc_now(),
            )
            .returning(Downloader.id)
        )
        return updated is not None

    def delete(self, downloader_id: str, *, expected_version: int) -> bool:
        deleted = self._session.scalar(
            delete(Downloader)
            .where(Downloader.id == downloader_id, Downloader.version == expected_version)
            .returning(Downloader.id)
        )
        return deleted is not None

    def task_count(self, downloader_id: str) -> int:
        return int(
            self._session.scalar(
                select(func.count())
                .select_from(UnpackTask)
                .where(UnpackTask.source_downloader_id == downloader_id)
            )
            or 0
        )

    def list_tasks(self, downloader_id: str) -> list[UnpackTask]:
        return list(
            self._session.scalars(
                select(UnpackTask)
                .where(UnpackTask.source_downloader_id == downloader_id)
                .order_by(UnpackTask.updated_at.desc(), UnpackTask.id)
            )
        )
