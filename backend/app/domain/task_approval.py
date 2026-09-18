from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256

from backend.app.domain.task_lifecycle import (
    TaskLifecycleRiskLevel,
    classify_execution_plan_risk,
)


class TaskApprovalState(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class TaskApprovalDecisionSource(StrEnum):
    WEB = "WEB"
    PREAUTHORIZED = "PREAUTHORIZED"
    TELEGRAM = "TELEGRAM"


@dataclass(frozen=True, slots=True)
class TaskRiskSummary:
    execution_plan_id: str
    plan_digest: str
    risk_level: TaskLifecycleRiskLevel
    reason_codes: tuple[str, ...]
    action_kinds: tuple[str, ...]
    hardlink_count: int
    client_fetch_count: int
    create_directory_count: int
    estimated_download_bytes_upper_bound: int
    risk_digest: str


def build_task_risk_summary(
    *,
    execution_plan_id: str,
    plan_digest: str,
    action_kinds: tuple[str, ...],
    blocked_reasons: tuple[str, ...],
    hardlink_count: int,
    client_fetch_count: int,
    create_directory_count: int,
    estimated_download_bytes_upper_bound: int,
) -> TaskRiskSummary:
    risk_level = classify_execution_plan_risk(
        action_kinds=action_kinds,
        blocked_reasons=blocked_reasons,
    )
    normalized_actions = tuple(sorted(set(action_kinds)))
    if blocked_reasons:
        reasons = tuple(sorted({f"PLAN_BLOCKED:{value}" for value in blocked_reasons}))
    elif risk_level is TaskLifecycleRiskLevel.HIGH:
        reasons = tuple(
            f"HIGH_RISK_ACTION:{value}"
            for value in normalized_actions
            if value not in _LOW_RISK_ACTION_KINDS
        ) or ("HIGH_RISK_PLAN",)
    elif risk_level is TaskLifecycleRiskLevel.LOW:
        reasons = ("LOW_RISK_ACTION_SET",)
    else:
        reasons = ("RISK_UNKNOWN",)

    payload = {
        "schema_version": "packbreaker-risk-summary-v1",
        "execution_plan_id": execution_plan_id,
        "plan_digest": plan_digest,
        "risk_level": risk_level.value,
        "reason_codes": list(reasons),
        "action_kinds": list(normalized_actions),
        "hardlink_count": hardlink_count,
        "client_fetch_count": client_fetch_count,
        "create_directory_count": create_directory_count,
        "estimated_download_bytes_upper_bound": estimated_download_bytes_upper_bound,
    }
    digest = sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return TaskRiskSummary(
        execution_plan_id=execution_plan_id,
        plan_digest=plan_digest,
        risk_level=risk_level,
        reason_codes=reasons,
        action_kinds=normalized_actions,
        hardlink_count=hardlink_count,
        client_fetch_count=client_fetch_count,
        create_directory_count=create_directory_count,
        estimated_download_bytes_upper_bound=estimated_download_bytes_upper_bound,
        risk_digest=digest,
    )


def preauthorization_covers(
    *,
    enabled: bool,
    allowed_action_kinds: tuple[str, ...],
    action_kinds: tuple[str, ...],
) -> bool:
    if not enabled:
        return False
    allowed = frozenset(allowed_action_kinds)
    required_high_risk = frozenset(
        value for value in action_kinds if value not in _LOW_RISK_ACTION_KINDS
    )
    return bool(required_high_risk) and required_high_risk.issubset(allowed)


_LOW_RISK_ACTION_KINDS = frozenset(
    {
        "HARDLINK",
        "CLIENT_FETCH",
        "PROTOCOL_PADDING",
        "ZERO_LENGTH",
    }
)
