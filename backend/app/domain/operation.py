from dataclasses import dataclass
from enum import StrEnum

from backend.app.domain.errors import DomainViolation, ErrorCode


class OperationStatus(StrEnum):
    INTENT_RECORDED = "INTENT_RECORDED"
    APPLIED = "APPLIED"
    NOOP = "NOOP"
    ROLLBACK_PENDING = "ROLLBACK_PENDING"
    ROLLED_BACK = "ROLLED_BACK"
    RECONCILE_REQUIRED = "RECONCILE_REQUIRED"
    ROLLBACK_BLOCKED = "ROLLBACK_BLOCKED"


class OperationKind(StrEnum):
    FILESYSTEM_DIRECTORY = "FILESYSTEM_DIRECTORY"
    FILESYSTEM_HARDLINK = "FILESYSTEM_HARDLINK"
    FILESYSTEM_REPAIR_ISOLATION = "FILESYSTEM_REPAIR_ISOLATION"
    QBITTORRENT_ADD = "QBITTORRENT_ADD"
    QBITTORRENT_RECHECK = "QBITTORRENT_RECHECK"
    QBITTORRENT_REPAIR_START = "QBITTORRENT_REPAIR_START"
    QBITTORRENT_REPAIR_STOP = "QBITTORRENT_REPAIR_STOP"
    QBITTORRENT_START = "QBITTORRENT_START"
    QBITTORRENT_REMOVE = "QBITTORRENT_REMOVE"
    TRANSMISSION_ADD = "TRANSMISSION_ADD"
    TRANSMISSION_VERIFY = "TRANSMISSION_VERIFY"
    TRANSMISSION_REPAIR_START = "TRANSMISSION_REPAIR_START"
    TRANSMISSION_REPAIR_STOP = "TRANSMISSION_REPAIR_STOP"
    TRANSMISSION_START = "TRANSMISSION_START"
    TRANSMISSION_REMOVE = "TRANSMISSION_REMOVE"
    OTHER = "OTHER"


_ALLOWED_TRANSITIONS: dict[OperationStatus, frozenset[OperationStatus]] = {
    OperationStatus.INTENT_RECORDED: frozenset(
        {
            OperationStatus.APPLIED,
            OperationStatus.NOOP,
            OperationStatus.RECONCILE_REQUIRED,
        }
    ),
    OperationStatus.APPLIED: frozenset(
        {OperationStatus.ROLLBACK_PENDING, OperationStatus.RECONCILE_REQUIRED}
    ),
    OperationStatus.NOOP: frozenset(),
    OperationStatus.ROLLBACK_PENDING: frozenset(
        {
            OperationStatus.ROLLED_BACK,
            OperationStatus.ROLLBACK_BLOCKED,
            OperationStatus.RECONCILE_REQUIRED,
        }
    ),
    OperationStatus.ROLLED_BACK: frozenset(),
    OperationStatus.RECONCILE_REQUIRED: frozenset(
        {
            OperationStatus.APPLIED,
            OperationStatus.NOOP,
            OperationStatus.ROLLBACK_PENDING,
            OperationStatus.ROLLBACK_BLOCKED,
        }
    ),
    OperationStatus.ROLLBACK_BLOCKED: frozenset({OperationStatus.RECONCILE_REQUIRED}),
}


@dataclass(frozen=True, slots=True)
class OperationTransition:
    from_status: OperationStatus
    to_status: OperationStatus


@dataclass(frozen=True, slots=True)
class OperationEventSummary:
    event_type: str
    reason: str


_OPERATION_EVENT_IDENTITIES: dict[str, tuple[str, str]] = {
    "CREATE_DIRECTORY": ("FILESYSTEM_DIRECTORY", "文件系统目录创建"),
    "CREATE_HARDLINK": ("FILESYSTEM_HARDLINK", "文件系统硬链接创建"),
    "ISOLATE_REPAIR_TARGET": ("FILESYSTEM_REPAIR_ISOLATION", "文件系统修复 inode 隔离"),
    "QBITTORRENT_ADD": ("QBITTORRENT_ADD", "qBittorrent 添加任务"),
    "QBITTORRENT_RECHECK": ("QBITTORRENT_RECHECK", "qBittorrent 强制校验"),
    "QBITTORRENT_REPAIR_START": ("QBITTORRENT_REPAIR_START", "qBittorrent 修复下载启动"),
    "QBITTORRENT_REPAIR_STOP": ("QBITTORRENT_REPAIR_STOP", "qBittorrent 修复下载停止"),
    "QBITTORRENT_START": ("QBITTORRENT_START", "qBittorrent 启动作种"),
    "QBITTORRENT_REMOVE": ("QBITTORRENT_REMOVE", "qBittorrent 移除任务"),
    "TRANSMISSION_ADD": ("TRANSMISSION_ADD", "Transmission 添加任务"),
    "TRANSMISSION_VERIFY": ("TRANSMISSION_VERIFY", "Transmission 强制校验"),
    "TRANSMISSION_REPAIR_START": (
        "TRANSMISSION_REPAIR_START",
        "Transmission 修复下载启动",
    ),
    "TRANSMISSION_REPAIR_STOP": ("TRANSMISSION_REPAIR_STOP", "Transmission 修复下载停止"),
    "TRANSMISSION_START": ("TRANSMISSION_START", "Transmission 启动作种"),
    "TRANSMISSION_REMOVE": ("TRANSMISSION_REMOVE", "Transmission 移除任务"),
}

_OPERATION_KINDS: dict[str, OperationKind] = {
    "CREATE_DIRECTORY": OperationKind.FILESYSTEM_DIRECTORY,
    "CREATE_HARDLINK": OperationKind.FILESYSTEM_HARDLINK,
    "ISOLATE_REPAIR_TARGET": OperationKind.FILESYSTEM_REPAIR_ISOLATION,
    "QBITTORRENT_ADD": OperationKind.QBITTORRENT_ADD,
    "QBITTORRENT_RECHECK": OperationKind.QBITTORRENT_RECHECK,
    "QBITTORRENT_REPAIR_START": OperationKind.QBITTORRENT_REPAIR_START,
    "QBITTORRENT_REPAIR_STOP": OperationKind.QBITTORRENT_REPAIR_STOP,
    "QBITTORRENT_START": OperationKind.QBITTORRENT_START,
    "QBITTORRENT_REMOVE": OperationKind.QBITTORRENT_REMOVE,
    "TRANSMISSION_ADD": OperationKind.TRANSMISSION_ADD,
    "TRANSMISSION_VERIFY": OperationKind.TRANSMISSION_VERIFY,
    "TRANSMISSION_REPAIR_START": OperationKind.TRANSMISSION_REPAIR_START,
    "TRANSMISSION_REPAIR_STOP": OperationKind.TRANSMISSION_REPAIR_STOP,
    "TRANSMISSION_START": OperationKind.TRANSMISSION_START,
    "TRANSMISSION_REMOVE": OperationKind.TRANSMISSION_REMOVE,
}

_OPERATION_STATUS_REASONS: dict[OperationStatus, str] = {
    OperationStatus.INTENT_RECORDED: (
        "已记录 operation journal intent，尚不能仅凭该事件断言副作用完成"
    ),
    OperationStatus.APPLIED: "已由 operation journal 与完成后证据确认副作用完成",
    OperationStatus.NOOP: "已确认无需执行副作用并记录为 NOOP",
    OperationStatus.ROLLBACK_PENDING: "已进入 journal-owned 资源回滚阶段",
    OperationStatus.ROLLED_BACK: "已确认 journal-owned 资源完成回滚",
    OperationStatus.RECONCILE_REQUIRED: "当前证据不足以自动确认真实状态，已要求安全对账",
    OperationStatus.ROLLBACK_BLOCKED: "回滚因所有权、快照或资源状态证据不足而阻断",
}


def operation_event_summary(
    operation_type: str,
    status: OperationStatus,
) -> OperationEventSummary | None:
    """生成可公开到 TaskEvent 的固定脱敏 operation journal 摘要。"""

    if operation_type in {
        "CREATE_DIRECTORY",
        "CREATE_HARDLINK",
        "ISOLATE_REPAIR_TARGET",
    } and status not in {
        OperationStatus.RECONCILE_REQUIRED,
        OperationStatus.ROLLBACK_BLOCKED,
    }:
        return None

    event_prefix, label = _OPERATION_EVENT_IDENTITIES.get(
        operation_type,
        ("OPERATION", "受控副作用操作"),
    )
    return OperationEventSummary(
        event_type=f"{event_prefix}_{status.value}",
        reason=f"{label}：{_OPERATION_STATUS_REASONS[status]}",
    )


def operation_kind(operation_type: str) -> OperationKind:
    """把内部 operation type 收敛成可公开的固定类别；未知类型不原样泄露。"""

    return _OPERATION_KINDS.get(operation_type, OperationKind.OTHER)


def transition_operation(
    current: OperationStatus,
    to_status: OperationStatus,
) -> OperationTransition:
    """校验 operation journal 状态推进；外部副作用不得绕过该状态机。"""

    if to_status not in _ALLOWED_TRANSITIONS[current]:
        raise DomainViolation(
            ErrorCode.INVALID_STATE_TRANSITION,
            f"operation journal 不允许从 {current.value} 推进到 {to_status.value}",
        )
    return OperationTransition(current, to_status)
