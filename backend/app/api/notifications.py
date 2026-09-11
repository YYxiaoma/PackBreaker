from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, SecretStr

from backend.app.api.dependencies import (
    AccessPrincipal,
    notification_service,
    require_admin_csrf_or_scope,
    require_admin_or_scope,
)
from backend.app.application.errors import ApplicationError
from backend.app.application.notifications import (
    CredentialAction,
    NotificationChannelCreate,
    NotificationChannelUpdate,
    NotificationChannelView,
)
from backend.app.domain.auth import ApiScope
from backend.app.domain.notification import (
    NotificationChannelKind,
    ServerChanCredential,
    TelegramCredential,
)

router = APIRouter(tags=["notifications"])
CONFIG_READ_ACCESS = require_admin_or_scope(ApiScope.CONFIG_READ)
CONFIG_WRITE_ACCESS = require_admin_csrf_or_scope(ApiScope.CONFIG_WRITE)


class TelegramCredentialInput(BaseModel):
    bot_token: SecretStr
    chat_id: SecretStr


class ServerChanCredentialInput(BaseModel):
    send_key: SecretStr


class NotificationChannelCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    type: Literal["TELEGRAM", "SERVERCHAN"]
    telegram: TelegramCredentialInput | None = None
    serverchan: ServerChanCredentialInput | None = None
    task_link_base_url: str | None = Field(default=None, max_length=1024)
    aggregation_window_seconds: int = Field(default=300, ge=1, le=86400)


class NotificationChannelUpdateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    task_link_base_url: str | None = Field(default=None, max_length=1024)
    aggregation_window_seconds: int = Field(default=300, ge=1, le=86400)
    credential_action: Literal["KEEP", "SET", "CLEAR"] = "KEEP"
    telegram: TelegramCredentialInput | None = None
    serverchan: ServerChanCredentialInput | None = None


class NotificationChannelActionRequest(BaseModel):
    action: Literal["enable", "disable"]


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
        "task_link_base_url": record.task_link_base_url,
        "aggregation_window_seconds": record.aggregation_window_seconds,
        "connection_status": record.connection_status,
        "enabled": record.enabled,
        "version": record.version,
        "last_test_at": _timestamp(record.last_test_at),
        "created_at": _timestamp(record.created_at),
        "updated_at": _timestamp(record.updated_at),
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
            task_link_base_url=payload.task_link_base_url,
            aggregation_window_seconds=payload.aggregation_window_seconds,
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
    updated = notification_service(request).update(
        channel_id,
        expected_version=_expected_version(if_match),
        request=NotificationChannelUpdate(
            name=payload.name,
            task_link_base_url=payload.task_link_base_url,
            aggregation_window_seconds=payload.aggregation_window_seconds,
            credential_action=CredentialAction(payload.credential_action),
            credential=credential,
        ),
    )
    return _json_with_etag(updated)


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
