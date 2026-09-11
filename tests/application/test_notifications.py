from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.notifications import (
    NotificationChannelCreate,
    NotificationService,
)
from backend.app.application.secrets import SecretStore
from backend.app.domain.notification import (
    DeliveryResult,
    NotificationChannelKind,
    NotificationCredential,
    NotificationDeliveryError,
    NotificationMessage,
    NotificationProvider,
    NotificationTestResult,
    TelegramCredential,
)
from backend.app.infrastructure.persistence.base import Base
from backend.app.infrastructure.persistence.database import (
    create_session_factory,
    create_sqlite_engine,
)
from backend.app.infrastructure.persistence.models import (
    NotificationOutbox,
    SecretRecord,
    TaskEvent,
)
from backend.app.infrastructure.persistence.repositories import TaskCreate, TaskRepository
from backend.app.infrastructure.security import SecretCipher


@dataclass
class _FakeProvider:
    kind: NotificationChannelKind
    messages: list[NotificationMessage] = field(default_factory=list)
    failure: NotificationDeliveryError | None = None

    async def test_connection(self) -> NotificationTestResult:
        if self.failure is not None:
            raise self.failure
        return NotificationTestResult(True, self.kind)

    async def send(self, message: NotificationMessage) -> DeliveryResult:
        self.messages.append(message)
        if self.failure is not None:
            raise self.failure
        return DeliveryResult(self.kind)


@dataclass
class _FakeFactory:
    provider: _FakeProvider
    credentials: list[NotificationCredential] = field(default_factory=list)

    def create(
        self,
        *,
        kind: NotificationChannelKind,
        credential: NotificationCredential,
    ) -> NotificationProvider:
        assert kind is self.provider.kind
        self.credentials.append(credential)
        return self.provider


@dataclass(frozen=True)
class _Fixture:
    engine: Engine
    factory: sessionmaker[Session]
    service: NotificationService
    provider: _FakeProvider
    task_id: str


@pytest.fixture
def notification_fixture(tmp_path: Path) -> Iterator[_Fixture]:
    engine = create_sqlite_engine(tmp_path / "notifications.db")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    secret_store = SecretStore(factory, SecretCipher(os.urandom(32)))
    provider = _FakeProvider(NotificationChannelKind.TELEGRAM)
    service = NotificationService(factory, secret_store, provider_factory=_FakeFactory(provider))
    with factory() as session:
        task, _ = TaskRepository(session).create_or_get(
            TaskCreate("PACKAGE_UNPACK", "source", "hash", "movie:key", "trace")
        )
        session.commit()
        task_id = task.id
    try:
        yield _Fixture(engine, factory, service, provider, task_id)
    finally:
        engine.dispose()


async def _enable_channel(fixture: _Fixture, *, window: int = 300) -> str:
    view = fixture.service.create(
        NotificationChannelCreate(
            name="主 Telegram",
            kind=NotificationChannelKind.TELEGRAM,
            credential=TelegramCredential("123:synthetic", "456"),
            task_link_base_url="https://packbreaker.invalid",
            aggregation_window_seconds=window,
        )
    )
    tested = await fixture.service.test_connection(view.id)
    assert tested["status"] == "ok"
    refreshed = fixture.service.get(view.id)
    enabled = fixture.service.set_enabled(
        view.id,
        expected_version=refreshed.version,
        enabled=True,
    )
    return enabled.id


@pytest.mark.asyncio
async def test_task_event_and_outbox_share_transaction_and_aggregate(
    notification_fixture: _Fixture,
) -> None:
    await _enable_channel(notification_fixture)
    with notification_fixture.factory() as session:
        repository = TaskRepository(session)
        repository.append_event(
            task_id=notification_fixture.task_id,
            event_type="QBITTORRENT_ADD_RECONCILE_REQUIRED",
            reason="synthetic private reason must not enter notification",
        )
        session.rollback()
    with notification_fixture.factory() as session:
        assert session.scalar(select(func.count()).select_from(NotificationOutbox)) == 0

    with notification_fixture.factory() as session:
        repository = TaskRepository(session)
        for _ in range(2):
            repository.append_event(
                task_id=notification_fixture.task_id,
                event_type="QBITTORRENT_ADD_RECONCILE_REQUIRED",
                reason="private-path=/data/secret",
            )
        session.commit()
        outboxes = list(session.scalars(select(NotificationOutbox)))
        assert len(outboxes) == 1
        assert outboxes[0].pending_count == 2
        assert "/data/secret" not in outboxes[0].body

    report = await notification_fixture.service.deliver_due_once(limit=10, max_attempts=3)
    assert report.delivered_count == 1
    assert notification_fixture.provider.messages[-1].repeat_count == 2
    assert "/data/secret" not in notification_fixture.provider.messages[-1].body


@pytest.mark.asyncio
async def test_duplicate_after_delivery_waits_for_window_then_sends_summary(
    notification_fixture: _Fixture,
) -> None:
    await _enable_channel(notification_fixture, window=60)
    with notification_fixture.factory() as session:
        TaskRepository(session).append_event(
            task_id=notification_fixture.task_id,
            event_type="FILESYSTEM_HARDLINK_ROLLBACK_BLOCKED",
            reason="synthetic",
        )
        session.commit()
    await notification_fixture.service.deliver_due_once(limit=10, max_attempts=3)

    with notification_fixture.factory() as session:
        TaskRepository(session).append_event(
            task_id=notification_fixture.task_id,
            event_type="FILESYSTEM_HARDLINK_ROLLBACK_BLOCKED",
            reason="synthetic again",
        )
        session.commit()
    assert (
        await notification_fixture.service.deliver_due_once(limit=10, max_attempts=3)
    ).scanned_count == 0

    with notification_fixture.factory() as session:
        outbox = session.scalar(select(NotificationOutbox))
        assert outbox is not None
        outbox.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()
    report = await notification_fixture.service.deliver_due_once(limit=10, max_attempts=3)
    assert report.delivered_count == 1
    assert notification_fixture.provider.messages[-1].repeat_count == 1


@pytest.mark.asyncio
async def test_retry_failure_does_not_change_task_or_leak_secret(
    notification_fixture: _Fixture,
) -> None:
    await _enable_channel(notification_fixture)
    canary = "123:synthetic"
    with notification_fixture.factory() as session:
        secret = session.scalar(select(SecretRecord))
        assert secret is not None
        assert canary not in secret.ciphertext
        TaskRepository(session).append_event(
            task_id=notification_fixture.task_id,
            event_type="QBITTORRENT_START_RECONCILE_REQUIRED",
            reason="synthetic",
        )
        session.commit()
    notification_fixture.provider.failure = NotificationDeliveryError(
        "NOTIFICATION_UNAVAILABLE", retryable=True
    )

    report = await notification_fixture.service.deliver_due_once(limit=10, max_attempts=3)
    assert report.retry_count == 1
    with notification_fixture.factory() as session:
        task = TaskRepository(session).get(notification_fixture.task_id)
        outbox = session.scalar(select(NotificationOutbox))
        assert task is not None and task.status == "PENDING"
        assert outbox is not None and outbox.state == "RETRY"
        assert outbox.last_error_code == "NOTIFICATION_UNAVAILABLE"


def test_non_high_value_event_creates_no_outbox(notification_fixture: _Fixture) -> None:
    with notification_fixture.factory() as session:
        TaskRepository(session).append_event(
            task_id=notification_fixture.task_id,
            event_type="ANALYSIS_SEARCHING",
            reason="searching",
        )
        session.commit()
        event_count = session.scalar(select(func.count()).select_from(TaskEvent))
        assert event_count is not None and event_count >= 2
        assert session.scalar(select(func.count()).select_from(NotificationOutbox)) == 0
