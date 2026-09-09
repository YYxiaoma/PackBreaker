from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4


@dataclass(frozen=True, slots=True)
class DomainEvent:
    """不可变事件载体；持久化层接入前不承担数据库职责。"""

    aggregate_id: UUID
    event_type: str
    payload: dict[str, str]
    occurred_at: datetime
    event_id: UUID


def new_domain_event(
    *,
    aggregate_id: UUID,
    event_type: str,
    payload: dict[str, str],
) -> DomainEvent:
    return DomainEvent(
        aggregate_id=aggregate_id,
        event_type=event_type,
        payload=dict(payload),
        occurred_at=datetime.now(UTC),
        event_id=uuid4(),
    )
