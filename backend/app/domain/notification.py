from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class NotificationChannelKind(StrEnum):
    TELEGRAM = "TELEGRAM"
    SERVERCHAN = "SERVERCHAN"


class NotificationSeverity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class NotificationDeliveryState(StrEnum):
    PENDING = "PENDING"
    RETRY = "RETRY"
    DELIVERED = "DELIVERED"
    DEAD = "DEAD"


@dataclass(frozen=True, slots=True)
class NotificationMessage:
    title: str
    body: str
    severity: NotificationSeverity
    event_key: str
    link: str | None = None
    repeat_count: int = 1

    def __post_init__(self) -> None:
        if not self.title or len(self.title) > 64:
            raise ValueError("通知标题长度无效")
        if not self.body or len(self.body) > 2048:
            raise ValueError("通知正文长度无效")
        if not self.event_key or len(self.event_key) > 128:
            raise ValueError("通知事件键长度无效")
        if self.link is not None and len(self.link) > 1024:
            raise ValueError("通知链接长度无效")
        if self.repeat_count < 1:
            raise ValueError("通知重复次数必须大于 0")


@dataclass(frozen=True, slots=True)
class TelegramCredential:
    bot_token: str
    chat_id: str


@dataclass(frozen=True, slots=True)
class ServerChanCredential:
    send_key: str


NotificationCredential = TelegramCredential | ServerChanCredential


@dataclass(frozen=True, slots=True)
class NotificationTestResult:
    ok: bool
    provider: NotificationChannelKind


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    provider: NotificationChannelKind


class NotificationDeliveryError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class NotificationProvider(Protocol):
    async def test_connection(self) -> NotificationTestResult: ...

    async def send(self, message: NotificationMessage) -> DeliveryResult: ...


_TERMINAL_MESSAGES: dict[str, tuple[str, NotificationSeverity]] = {
    "DONE": ("辅种任务已完成", NotificationSeverity.INFO),
    "FAILED": ("辅种任务失败", NotificationSeverity.ERROR),
    "CANCELLED": ("辅种任务已取消", NotificationSeverity.INFO),
}


def notification_message_for_site_reliability_event(
    *,
    site_id: str,
    event_type: str,
    error_code: str | None,
) -> NotificationMessage | None:
    if event_type == "CIRCUIT_OPENED":
        safe_code = (
            error_code
            if error_code
            and len(error_code) <= 64
            and error_code.isascii()
            and error_code == error_code.upper()
            and error_code.replace("_", "").isalnum()
            else "SITE_RELIABILITY_FAILURE"
        )
        return NotificationMessage(
            title="站点自动化已熔断",
            body=f"站点配置 {site_id} 因连续异常进入熔断保护；错误码：{safe_code}。",
            severity=NotificationSeverity.WARNING,
            event_key="SITE_CIRCUIT_OPENED",
        )
    if event_type == "CIRCUIT_RECOVERED":
        return NotificationMessage(
            title="站点自动化已恢复",
            body=f"站点配置 {site_id} 已通过半开探测并恢复正常调用。",
            severity=NotificationSeverity.INFO,
            event_key="SITE_CIRCUIT_RECOVERED",
        )
    return None


def notification_message_for_task_event(
    *,
    task_id: str,
    event_type: str,
    to_status: str,
    link: str | None,
) -> NotificationMessage | None:
    terminal = _TERMINAL_MESSAGES.get(to_status)
    if terminal is not None:
        title, severity = terminal
        return NotificationMessage(
            title=title,
            body=f"任务 {task_id} 状态已变为 {to_status}。",
            severity=severity,
            event_key=f"TASK_{to_status}",
            link=link,
        )

    if event_type.endswith("_RECONCILE_REQUIRED"):
        return NotificationMessage(
            title="任务需要人工对账",
            body=f"任务 {task_id} 出现需要人工重新证明的操作结果。",
            severity=NotificationSeverity.WARNING,
            event_key="OPERATION_RECONCILE_REQUIRED",
            link=link,
        )
    if event_type.endswith("_ROLLBACK_BLOCKED"):
        return NotificationMessage(
            title="任务回滚被阻断",
            body=f"任务 {task_id} 的资源回滚被安全门阻断，需要人工检查。",
            severity=NotificationSeverity.ERROR,
            event_key="OPERATION_ROLLBACK_BLOCKED",
            link=link,
        )
    return None
