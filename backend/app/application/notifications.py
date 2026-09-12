from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol
from urllib.parse import urlsplit

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.errors import ApplicationError
from backend.app.application.secrets import SecretNotFound, SecretStore
from backend.app.domain.notification import (
    NotificationChannelKind,
    NotificationCredential,
    NotificationDeliveryError,
    NotificationMessage,
    NotificationProvider,
    NotificationSeverity,
    ServerChanCredential,
    TelegramCredential,
)
from backend.app.infrastructure.adapters.notifications import NotificationProviderFactory
from backend.app.infrastructure.persistence.models import NotificationChannel
from backend.app.infrastructure.persistence.notification_repositories import (
    NotificationChannelRepository,
    NotificationOutboxRepository,
)
from backend.app.infrastructure.site_reliability import SiteReliabilityEvent

_SECRET_KIND = "notification-credential"
_SERVERCHAN_SC3_PATTERN = re.compile(r"^sctp\d+t")


class CredentialAction(StrEnum):
    KEEP = "KEEP"
    SET = "SET"
    CLEAR = "CLEAR"


@dataclass(frozen=True, slots=True)
class NotificationChannelView:
    id: str
    name: str
    type: NotificationChannelKind
    credential_configured: bool
    task_link_base_url: str | None
    aggregation_window_seconds: int
    connection_status: str
    enabled: bool
    version: int
    last_test_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class NotificationChannelCreate:
    name: str
    kind: NotificationChannelKind
    credential: NotificationCredential
    task_link_base_url: str | None = None
    aggregation_window_seconds: int = 300


@dataclass(frozen=True, slots=True)
class NotificationChannelUpdate:
    name: str
    task_link_base_url: str | None
    aggregation_window_seconds: int
    credential_action: CredentialAction = CredentialAction.KEEP
    credential: NotificationCredential | None = None


@dataclass(frozen=True, slots=True)
class NotificationDeliveryReport:
    scanned_count: int
    delivered_count: int
    retry_count: int
    dead_count: int


class NotificationProviderFactoryPort(Protocol):
    def create(
        self,
        *,
        kind: NotificationChannelKind,
        credential: NotificationCredential,
    ) -> NotificationProvider: ...


class NotificationService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        secret_store: SecretStore,
        *,
        provider_factory: NotificationProviderFactoryPort | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._secret_store = secret_store
        self._provider_factory = provider_factory or NotificationProviderFactory()

    def list_channels(self) -> list[NotificationChannelView]:
        with self._session_factory() as session:
            return [
                self._view(record) for record in NotificationChannelRepository(session).list_all()
            ]

    def get(self, channel_id: str) -> NotificationChannelView:
        with self._session_factory() as session:
            record = NotificationChannelRepository(session).get(channel_id)
            if record is None:
                raise _channel_not_found()
            return self._view(record)

    def create(self, request: NotificationChannelCreate) -> NotificationChannelView:
        name = _name(request.name)
        credential = _validate_credential(request.kind, request.credential)
        link = _link_base(request.task_link_base_url)
        window = _aggregation_window(request.aggregation_window_seconds)
        with self._session_factory() as session:
            secret_id = self._secret_store.put_in_session(
                session,
                kind=_SECRET_KIND,
                value=_encode_credential(request.kind, credential),
            )
            try:
                record = NotificationChannelRepository(session).create(
                    name=name,
                    kind=request.kind.value,
                    secret_id=secret_id,
                    task_link_base_url=link,
                    aggregation_window_seconds=window,
                )
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                raise _name_conflict() from exc
            return self._view(record)

    def update(
        self,
        channel_id: str,
        *,
        expected_version: int,
        request: NotificationChannelUpdate,
    ) -> NotificationChannelView:
        name = _name(request.name)
        link = _link_base(request.task_link_base_url)
        window = _aggregation_window(request.aggregation_window_seconds)
        with self._session_factory() as session:
            repository = NotificationChannelRepository(session)
            current = repository.get(channel_id)
            if current is None:
                raise _channel_not_found()
            if current.version != expected_version:
                raise _version_conflict()
            values: dict[str, object] = {
                "name": name,
                "task_link_base_url": link,
                "aggregation_window_seconds": window,
            }
            old_secret_id = current.secret_id
            if request.credential_action is CredentialAction.SET:
                if request.credential is None:
                    raise _credential_invalid("SET 必须提供新凭证")
                kind = NotificationChannelKind(current.type)
                credential = _validate_credential(kind, request.credential)
                values["secret_id"] = self._secret_store.put_in_session(
                    session,
                    kind=_SECRET_KIND,
                    value=_encode_credential(kind, credential),
                )
                values["connection_status"] = "UNTESTED"
                values["enabled"] = False
            elif request.credential_action is CredentialAction.CLEAR:
                values["secret_id"] = None
                values["connection_status"] = "UNTESTED"
                values["enabled"] = False
            elif request.credential is not None:
                raise _credential_invalid("KEEP 不能同时提交凭证明文")

            try:
                if not repository.update_config(
                    channel_id,
                    expected_version=expected_version,
                    values=values,
                ):
                    raise _version_conflict()
                if old_secret_id is not None and request.credential_action in {
                    CredentialAction.SET,
                    CredentialAction.CLEAR,
                }:
                    self._secret_store.delete_in_session(session, old_secret_id)
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                raise _name_conflict() from exc
            refreshed = repository.get(channel_id)
            if refreshed is None:
                raise _channel_not_found()
            return self._view(refreshed)

    def delete(self, channel_id: str, *, expected_version: int) -> None:
        with self._session_factory() as session:
            repository = NotificationChannelRepository(session)
            current = repository.get(channel_id)
            if current is None:
                raise _channel_not_found()
            if current.version != expected_version:
                raise _version_conflict()
            old_secret_id = current.secret_id
            if repository.delete(channel_id, expected_version=expected_version) is None:
                raise _version_conflict()
            if old_secret_id is not None:
                self._secret_store.delete_in_session(session, old_secret_id)
            session.commit()

    def set_enabled(
        self,
        channel_id: str,
        *,
        expected_version: int,
        enabled: bool,
    ) -> NotificationChannelView:
        with self._session_factory() as session:
            repository = NotificationChannelRepository(session)
            current = repository.get(channel_id)
            if current is None:
                raise _channel_not_found()
            if current.version != expected_version:
                raise _version_conflict()
            if enabled and (current.secret_id is None or current.connection_status != "OK"):
                raise ApplicationError(
                    code="NOTIFICATION_CHANNEL_NOT_READY",
                    status=409,
                    title="通知渠道尚未通过连接测试",
                    detail="启用通知前必须配置凭证并完成真实测试发送",
                )
            if not repository.update_config(
                channel_id,
                expected_version=expected_version,
                values={"enabled": enabled},
            ):
                raise _version_conflict()
            session.commit()
            refreshed = repository.get(channel_id)
            if refreshed is None:
                raise _channel_not_found()
            return self._view(refreshed)

    async def test_connection(self, channel_id: str) -> dict[str, object]:
        with self._session_factory() as session:
            record = NotificationChannelRepository(session).get(channel_id)
            if record is None:
                raise _channel_not_found()
            snapshot_id = record.id
            snapshot_version = record.version
            kind = NotificationChannelKind(record.type)
            secret_id = record.secret_id
        if secret_id is None:
            raise _credential_invalid("连接测试需要已配置通知凭证")
        provider = self._provider_factory.create(
            kind=kind,
            credential=self._load_credential(secret_id, kind),
        )
        tested_at = datetime.now(UTC)
        try:
            result = await provider.test_connection()
        except NotificationDeliveryError as exc:
            self._save_probe(snapshot_id, snapshot_version, "FAILED", tested_at)
            raise ApplicationError(
                code=exc.code,
                status=502,
                title="通知渠道测试失败",
                detail="通知服务拒绝、超时或返回无效响应；未记录远端响应正文",
            ) from exc
        self._save_probe(snapshot_id, snapshot_version, "OK", tested_at)
        return {"status": "ok", "type": result.provider.value, "tested_at": tested_at.isoformat()}

    def record_site_reliability_event(self, event: SiteReliabilityEvent) -> None:
        with self._session_factory() as session:
            NotificationOutboxRepository(session).project_site_reliability_event(
                site_id=event.config_id,
                event_type=event.event_type,
                error_code=event.error_code,
                occurred_at=datetime.now(UTC),
            )
            session.commit()

    async def deliver_due_once(
        self,
        *,
        limit: int,
        max_attempts: int,
    ) -> NotificationDeliveryReport:
        now = datetime.now(UTC)
        with self._session_factory() as session:
            due_ids = tuple(
                record.id
                for record in NotificationOutboxRepository(session).list_due(now=now, limit=limit)
            )
        delivered = retry = dead = 0
        for outbox_id in due_ids:
            outcome = await self._deliver_one(outbox_id, max_attempts=max_attempts)
            delivered += outcome == "DELIVERED"
            retry += outcome == "RETRY"
            dead += outcome == "DEAD"
        return NotificationDeliveryReport(len(due_ids), delivered, retry, dead)

    async def _deliver_one(self, outbox_id: str, *, max_attempts: int) -> str:
        with self._session_factory() as session:
            outbox = NotificationOutboxRepository(session).get(outbox_id)
            if outbox is None or outbox.pending_count <= 0:
                return "SKIPPED"
            channel = NotificationChannelRepository(session).get(outbox.channel_id)
            if channel is None or not channel.enabled or channel.secret_id is None:
                channel_version = None if channel is None else channel.version
                return self._record_delivery_failure(
                    outbox_id,
                    "NOTIFICATION_CHANNEL_DISABLED",
                    retryable=False,
                    max_attempts=max_attempts,
                    channel_version=channel_version,
                )
            kind = NotificationChannelKind(channel.type)
            channel_version = channel.version
            aggregation_window = channel.aggregation_window_seconds
            secret_id = channel.secret_id
            sent_count = outbox.pending_count
            message = NotificationMessage(
                title=outbox.title,
                body=outbox.body,
                severity=NotificationSeverity(outbox.severity),
                event_key=outbox.event_key,
                link=outbox.link,
                repeat_count=sent_count,
            )
        try:
            provider = self._provider_factory.create(
                kind=kind,
                credential=self._load_credential(secret_id, kind),
            )
            await provider.send(message)
        except NotificationDeliveryError as exc:
            return self._record_delivery_failure(
                outbox_id,
                exc.code,
                retryable=exc.retryable,
                max_attempts=max_attempts,
                channel_version=channel_version,
            )

        delivered_at = datetime.now(UTC)
        with self._session_factory() as session:
            NotificationOutboxRepository(session).mark_delivered(
                outbox_id,
                delivered_at=delivered_at,
                sent_count=sent_count,
                aggregation_window_seconds=aggregation_window,
                channel_version=channel_version,
            )
            session.commit()
        return "DELIVERED"

    def _record_delivery_failure(
        self,
        outbox_id: str,
        code: str,
        *,
        retryable: bool,
        max_attempts: int,
        channel_version: int | None,
    ) -> str:
        occurred_at = datetime.now(UTC)
        with self._session_factory() as session:
            repository = NotificationOutboxRepository(session)
            record = repository.get(outbox_id)
            if record is None:
                return "SKIPPED"
            delay_seconds = min(3600, 30 * (2 ** min(record.attempt_count, 7)))
            repository.mark_failure(
                outbox_id,
                error_code=code,
                retryable=retryable,
                max_attempts=max_attempts,
                retry_at=occurred_at + timedelta(seconds=delay_seconds),
                occurred_at=occurred_at,
                channel_version=channel_version,
            )
            refreshed = repository.get(outbox_id)
            session.commit()
            if refreshed is None:
                return "SKIPPED"
            return "RETRY" if refreshed.state == "RETRY" else "DEAD"

    def _save_probe(
        self,
        channel_id: str,
        version: int,
        status: str,
        tested_at: datetime,
    ) -> None:
        with self._session_factory() as session:
            NotificationChannelRepository(session).update_probe(
                channel_id,
                expected_version=version,
                status=status,
                tested_at=tested_at,
            )
            session.commit()

    def _load_credential(
        self,
        secret_id: str,
        kind: NotificationChannelKind,
    ) -> NotificationCredential:
        try:
            payload = json.loads(self._secret_store.get(secret_id).decode("utf-8"))
        except (SecretNotFound, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise NotificationDeliveryError(
                "NOTIFICATION_CREDENTIAL_INVALID", retryable=False
            ) from exc
        if not isinstance(payload, dict) or payload.get("type") != kind.value:
            raise NotificationDeliveryError("NOTIFICATION_CREDENTIAL_INVALID", retryable=False)
        try:
            if kind is NotificationChannelKind.TELEGRAM:
                return _validate_credential(
                    kind,
                    TelegramCredential(str(payload["bot_token"]), str(payload["chat_id"])),
                )
            return _validate_credential(kind, ServerChanCredential(str(payload["send_key"])))
        except (KeyError, ValueError) as exc:
            raise NotificationDeliveryError(
                "NOTIFICATION_CREDENTIAL_INVALID", retryable=False
            ) from exc

    @staticmethod
    def _view(record: NotificationChannel) -> NotificationChannelView:
        return NotificationChannelView(
            id=record.id,
            name=record.name,
            type=NotificationChannelKind(record.type),
            credential_configured=record.secret_id is not None,
            task_link_base_url=record.task_link_base_url,
            aggregation_window_seconds=record.aggregation_window_seconds,
            connection_status=record.connection_status,
            enabled=record.enabled,
            version=record.version,
            last_test_at=record.last_test_at,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )


def _encode_credential(kind: NotificationChannelKind, credential: NotificationCredential) -> bytes:
    if kind is NotificationChannelKind.TELEGRAM and isinstance(credential, TelegramCredential):
        payload = {
            "type": kind.value,
            "bot_token": credential.bot_token,
            "chat_id": credential.chat_id,
        }
    elif kind is NotificationChannelKind.SERVERCHAN and isinstance(
        credential, ServerChanCredential
    ):
        payload = {"type": kind.value, "send_key": credential.send_key}
    else:
        raise ValueError("通知凭证类型不匹配")
    return json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")


def _validate_credential(
    kind: NotificationChannelKind,
    credential: NotificationCredential,
) -> NotificationCredential:
    if kind is NotificationChannelKind.TELEGRAM:
        if not isinstance(credential, TelegramCredential):
            raise _credential_invalid("Telegram 需要 bot token 与 chat ID")
        token = credential.bot_token.strip()
        chat_id = credential.chat_id.strip()
        if not token or len(token) > 256 or any(char in token for char in "\r\n"):
            raise _credential_invalid("Telegram bot token 格式无效")
        if not chat_id or len(chat_id) > 128 or any(char in chat_id for char in "\r\n"):
            raise _credential_invalid("Telegram chat ID 格式无效")
        return TelegramCredential(token, chat_id)
    if not isinstance(credential, ServerChanCredential):
        raise _credential_invalid("Server酱需要 SendKey")
    send_key = credential.send_key.strip()
    if (
        not send_key
        or len(send_key) > 256
        or any(char in send_key for char in "\r\n")
        or not (send_key.startswith("SCT") or _SERVERCHAN_SC3_PATTERN.match(send_key))
    ):
        raise _credential_invalid("Server酱 SendKey 格式无效")
    return ServerChanCredential(send_key)


def _name(value: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 80:
        raise ApplicationError(
            code="NOTIFICATION_NAME_INVALID",
            status=422,
            title="通知渠道名称无效",
            detail="名称不能为空且最多 80 个字符",
        )
    return normalized


def _link_base(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    normalized = value.strip().rstrip("/")
    parsed = urlsplit(normalized)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ApplicationError(
            code="NOTIFICATION_LINK_INVALID",
            status=422,
            title="任务链接基础地址无效",
            detail="只允许不含用户信息、查询参数或 fragment 的 http/https 地址",
        )
    return normalized


def _aggregation_window(value: int) -> int:
    if isinstance(value, bool) or not 1 <= value <= 86400:
        raise ApplicationError(
            code="NOTIFICATION_AGGREGATION_WINDOW_INVALID",
            status=422,
            title="通知聚合窗口无效",
            detail="聚合窗口必须在 1 到 86400 秒之间",
        )
    return value


def _credential_invalid(detail: str) -> ApplicationError:
    return ApplicationError(
        code="NOTIFICATION_CREDENTIAL_INVALID",
        status=422,
        title="通知凭证无效",
        detail=detail,
    )


def _channel_not_found() -> ApplicationError:
    return ApplicationError(
        code="NOTIFICATION_CHANNEL_NOT_FOUND",
        status=404,
        title="通知渠道不存在",
        detail="指定通知渠道不存在或已经删除",
    )


def _version_conflict() -> ApplicationError:
    return ApplicationError(
        code="NOTIFICATION_VERSION_CONFLICT",
        status=409,
        title="通知渠道版本冲突",
        detail="通知配置已被修改，请重新读取后再提交",
    )


def _name_conflict() -> ApplicationError:
    return ApplicationError(
        code="NOTIFICATION_NAME_CONFLICT",
        status=409,
        title="通知渠道名称冲突",
        detail="通知渠道名称已经存在",
    )
