from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, SecretStr

from backend.app.api.dependencies import (
    AccessPrincipal,
    admin_notification_service,
    notification_service,
    require_admin_csrf_principal,
    require_admin_principal,
)
from backend.app.application.admin_notifications import AdminNotificationView
from backend.app.application.errors import ApplicationError
from backend.app.application.notifications import (
    CredentialAction,
    NotificationChannelCreate,
    NotificationChannelUpdate,
    NotificationChannelView,
)
from backend.app.domain.notification import (
    PRODUCT_NOTIFICATION_EVENT_TYPES,
    NotificationChannelKind,
    NotificationEventType,
    ServerChanCredential,
    TelegramCredential,
)

router = APIRouter(tags=["notifications"])
CONFIG_READ_ACCESS = require_admin_principal
CONFIG_WRITE_ACCESS = require_admin_csrf_principal


class TelegramCredentialInput(BaseModel):
    bot_token: SecretStr
    chat_id: SecretStr


class ServerChanCredentialInput(BaseModel):
    send_key: SecretStr


class NotificationProxyCreateInput(BaseModel):
    enabled: bool = False
    host: str | None = Field(default=None, max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    username: str | None = Field(default=None, max_length=255)
    password: SecretStr | None = Field(default=None, max_length=512)


class NotificationProxyPatchInput(BaseModel):
    enabled: bool = False
    host: str | None = Field(default=None, max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    username: str | None = Field(default=None, max_length=255)
    password: SecretStr | None = Field(default=None, max_length=512)
    clear_password: bool = False


class NotificationChannelCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    type: Literal["TELEGRAM", "SERVERCHAN"]
    telegram: TelegramCredentialInput | None = None
    serverchan: ServerChanCredentialInput | None = None
    event_types: list[NotificationEventType] = Field(
        default_factory=lambda: list(PRODUCT_NOTIFICATION_EVENT_TYPES), min_length=1
    )
    proxy: NotificationProxyCreateInput = Field(default_factory=NotificationProxyCreateInput)


class NotificationChannelUpdateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    event_types: list[NotificationEventType] = Field(min_length=1)
    credential_action: Literal["KEEP", "SET", "CLEAR"] = "KEEP"
    telegram: TelegramCredentialInput | None = None
    serverchan: ServerChanCredentialInput | None = None
    proxy: NotificationProxyPatchInput = Field(default_factory=NotificationProxyPatchInput)


class NotificationTemporaryProbeRequest(BaseModel):
    type: Literal["TELEGRAM", "SERVERCHAN"]
    telegram: TelegramCredentialInput | None = None
    serverchan: ServerChanCredentialInput | None = None
    proxy: NotificationProxyCreateInput = Field(default_factory=NotificationProxyCreateInput)


class NotificationChannelActionRequest(BaseModel):
    action: Literal["enable", "disable"]


class InboxNotificationActionRequest(BaseModel):
    action: Literal["mark_read"]


class InboxBulkActionRequest(BaseModel):
    action: Literal["mark_all_read"]


def _credential(
    kind: NotificationChannelKind,
    telegram: TelegramCredentialInput | None,
    serverchan: ServerChanCredentialInput | None,
) -> TelegramCredential | ServerChanCredential:
    if kind is NotificationChannelKind.TELEGRAM and telegram is not None and serverchan is None:
        return TelegramCredential(
            telegram.bot_token.get_secret_value(),
            telegram.chat_id.get_secret_value(),
        )
    if kind is NotificationChannelKind.SERVERCHAN and serverchan is not None and telegram is None:
        return ServerChanCredential(serverchan.send_key.get_secret_value())
    raise ApplicationError(
        code="NOTIFICATION_CREDENTIAL_INVALID",
        status=422,
        title="通知凭证与渠道类型不匹配",
        detail="Telegram 只接受 bot_token/chat_id；Server酱只接受 SendKey",
    )


def _timestamp(value: datetime | None) -> str | None:
    return value.isoformat().replace("+00:00", "Z") if value is not None else None


def _view(record: NotificationChannelView) -> dict[str, object]:
    return {
        "id": record.id,
        "name": record.name,
        "type": record.type.value,
        "credential_configured": record.credential_configured,
        "event_types": [item.value for item in record.event_types],
        "proxy_enabled": record.proxy_enabled,
        "proxy_host": record.proxy_host,
        "proxy_port": record.proxy_port,
        "proxy_username": record.proxy_username,
        "proxy_credential_configured": record.proxy_credential_configured,
        "connection_status": record.connection_status,
        "enabled": record.enabled,
        "version": record.version,
        "last_test_at": _timestamp(record.last_test_at),
        "created_at": _timestamp(record.created_at),
        "updated_at": _timestamp(record.updated_at),
    }


def _inbox_view(record: AdminNotificationView) -> dict[str, object]:
    return {
        "id": record.id,
        "event_type": record.event_type,
        "title": record.title,
        "message": record.message,
        "severity": record.severity,
        "read_at": _timestamp(record.read_at),
        "created_at": _timestamp(record.created_at),
    }


def _expected_version(value: str | None) -> int:
    if value is None:
        raise ApplicationError(
            code="IF_MATCH_REQUIRED",
            status=428,
            title="缺少版本前置条件",
            detail="修改或删除通知渠道时必须提供 If-Match 版本",
        )
    if len(value) < 3 or not value.startswith('"') or not value.endswith('"'):
        raise ApplicationError(
            code="IF_MATCH_INVALID",
            status=400,
            title="If-Match 格式无效",
            detail='If-Match 必须使用形如 "3" 的强 ETag',
        )
    try:
        parsed = int(value[1:-1])
    except ValueError as exc:
        raise ApplicationError(
            code="IF_MATCH_INVALID",
            status=400,
            title="If-Match 格式无效",
            detail='If-Match 必须使用形如 "3" 的强 ETag',
        ) from exc
    if parsed < 1:
        raise ApplicationError(
            code="IF_MATCH_INVALID",
            status=400,
            title="If-Match 格式无效",
            detail="版本必须大于 0",
        )
    return parsed


def _json_with_etag(
    record: NotificationChannelView,
    *,
    status_code: int = status.HTTP_200_OK,
) -> JSONResponse:
    return JSONResponse(
        _view(record),
        status_code=status_code,
        headers={"ETag": f'"{record.version}"'},
    )


@router.get("/notifications/inbox")
async def list_inbox_notifications(
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(CONFIG_READ_ACCESS)],
    unread_only: bool = False,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict[str, object]:
    items = admin_notification_service(request).list_recent(
        unread_only=unread_only,
        limit=limit,
    )
    return {"items": [_inbox_view(item) for item in items]}


@router.get("/notifications/inbox/unread-count")
async def inbox_unread_count(
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(CONFIG_READ_ACCESS)],
) -> dict[str, int]:
    return {"count": admin_notification_service(request).unread_count()}


@router.post("/notifications/inbox/{notification_id}/actions")
async def inbox_notification_action(
    notification_id: str,
    request: Request,
    payload: InboxNotificationActionRequest,
    _principal: Annotated[AccessPrincipal, Depends(CONFIG_WRITE_ACCESS)],
) -> dict[str, object]:
    return _inbox_view(admin_notification_service(request).mark_read(notification_id))


@router.post("/notifications/inbox/actions")
async def inbox_bulk_action(
    request: Request,
    payload: InboxBulkActionRequest,
    _principal: Annotated[AccessPrincipal, Depends(CONFIG_WRITE_ACCESS)],
) -> dict[str, int]:
    return {"updated": admin_notification_service(request).mark_all_read()}


@router.get("/notification-channels")
async def list_notification_channels(
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(CONFIG_READ_ACCESS)],
) -> dict[str, object]:
    return {"items": [_view(item) for item in notification_service(request).list_channels()]}


@router.post("/notification-channels", status_code=status.HTTP_201_CREATED)
async def create_notification_channel(
    request: Request,
    payload: NotificationChannelCreateRequest,
    _principal: Annotated[AccessPrincipal, Depends(CONFIG_WRITE_ACCESS)],
) -> Response:
    kind = NotificationChannelKind(payload.type)
    record = notification_service(request).create(
        NotificationChannelCreate(
            name=payload.name,
            kind=kind,
            credential=_credential(kind, payload.telegram, payload.serverchan),
            event_types=tuple(payload.event_types),
            proxy_enabled=payload.proxy.enabled,
            proxy_host=payload.proxy.host,
            proxy_port=payload.proxy.port,
            proxy_username=payload.proxy.username,
            proxy_password=(
                payload.proxy.password.get_secret_value()
                if payload.proxy.password is not None
                else None
            ),
        )
    )
    return _json_with_etag(record, status_code=status.HTTP_201_CREATED)


@router.put("/notification-channels/{channel_id}")
async def update_notification_channel(
    channel_id: str,
    request: Request,
    payload: NotificationChannelUpdateRequest,
    _principal: Annotated[AccessPrincipal, Depends(CONFIG_WRITE_ACCESS)],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Response:
    current = notification_service(request).get(channel_id)
    credential = None
    if payload.credential_action == "SET":
        credential = _credential(current.type, payload.telegram, payload.serverchan)
    elif payload.telegram is not None or payload.serverchan is not None:
        raise ApplicationError(
            code="NOTIFICATION_CREDENTIAL_INVALID",
            status=422,
            title="通知凭证更新方式无效",
            detail="只有 credential_action=SET 时才能提交凭证明文",
        )
    if payload.proxy.clear_password and payload.proxy.password is not None:
        raise ApplicationError(
            code="NOTIFICATION_PROXY_INVALID",
            status=422,
            title="通知代理配置无效",
            detail="proxy.password 与 proxy.clear_password 不能同时使用",
        )
    proxy_password_action = CredentialAction.KEEP
    if payload.proxy.clear_password:
        proxy_password_action = CredentialAction.CLEAR
    elif payload.proxy.password is not None:
        proxy_password_action = CredentialAction.SET
    updated = notification_service(request).update(
        channel_id,
        expected_version=_expected_version(if_match),
        request=NotificationChannelUpdate(
            name=payload.name,
            event_types=tuple(payload.event_types),
            proxy_enabled=payload.proxy.enabled,
            proxy_host=payload.proxy.host,
            proxy_port=payload.proxy.port,
            proxy_username=payload.proxy.username,
            credential_action=CredentialAction(payload.credential_action),
            credential=credential,
            proxy_password_action=proxy_password_action,
            proxy_password=(
                payload.proxy.password.get_secret_value()
                if payload.proxy.password is not None
                else None
            ),
        ),
    )
    return _json_with_etag(updated)


@router.post("/notification-channels/probe")
async def probe_notification_channel(
    request: Request,
    payload: NotificationTemporaryProbeRequest,
    _principal: Annotated[AccessPrincipal, Depends(CONFIG_WRITE_ACCESS)],
) -> dict[str, object]:
    kind = NotificationChannelKind(payload.type)
    return await notification_service(request).test_temporary(
        kind=kind,
        credential=_credential(kind, payload.telegram, payload.serverchan),
        proxy_enabled=payload.proxy.enabled,
        proxy_host=payload.proxy.host,
        proxy_port=payload.proxy.port,
        proxy_username=payload.proxy.username,
        proxy_password=(
            payload.proxy.password.get_secret_value()
            if payload.proxy.password is not None
            else None
        ),
    )


@router.delete("/notification-channels/{channel_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_notification_channel(
    channel_id: str,
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(CONFIG_WRITE_ACCESS)],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Response:
    notification_service(request).delete(channel_id, expected_version=_expected_version(if_match))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/notification-channels/{channel_id}/test")
async def test_notification_channel(
    channel_id: str,
    request: Request,
    _principal: Annotated[AccessPrincipal, Depends(CONFIG_WRITE_ACCESS)],
) -> dict[str, object]:
    return await notification_service(request).test_connection(channel_id)


@router.post("/notification-channels/{channel_id}/actions")
async def notification_channel_action(
    channel_id: str,
    request: Request,
    payload: NotificationChannelActionRequest,
    _principal: Annotated[AccessPrincipal, Depends(CONFIG_WRITE_ACCESS)],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> Response:
    updated = notification_service(request).set_enabled(
        channel_id,
        expected_version=_expected_version(if_match),
        enabled=payload.action == "enable",
    )
    return _json_with_etag(updated)
