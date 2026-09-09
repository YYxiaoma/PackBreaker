from typing import Protocol
from uuid import UUID

from backend.app.domain.events import DomainEvent


class EventRepository(Protocol):
    """事件存储端口；实现可替换为 SQLite/PostgreSQL。"""

    async def append(self, event: DomainEvent) -> None: ...

    async def list_for_aggregate(self, aggregate_id: UUID) -> list[DomainEvent]: ...
