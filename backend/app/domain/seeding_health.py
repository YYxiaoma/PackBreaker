"""做种健康巡查的领域规则：异常分类、风险评分、状态机与站点级降级判定。

本模块只包含枚举与纯函数，不接触数据库、网络或文件系统，便于单测覆盖
全部真值表。所有「未知」都显式表达为 None / UNKNOWN，绝不用 0 兜底。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

# 单种子连续确认次数：站点抖动不应立刻产生异常。
DEFAULT_CONFIRM_THRESHOLD = 2
# 站点级熔断：单次巡查中「未找到」比例超过该值即判定站点异常。
DEFAULT_DEGRADE_RATIO = 0.5
DEFAULT_DEGRADE_MIN_SAMPLES = 10
# 风险分档。
SEVERITY_HIGH_MIN = 80
SEVERITY_MEDIUM_MIN = 50
# H&R 剩余时长压力阈值（小时）。
HNR_CRITICAL_HOURS = 24.0
HNR_WARNING_HOURS = 72.0


class SeedingIssueKind(StrEnum):
    SITE_MISSING = "SITE_MISSING"
    SITE_ONLY = "SITE_ONLY"
    TRACKER_ERROR = "TRACKER_ERROR"
    FILE_MISSING = "FILE_MISSING"


class SeedingIssueSeverity(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class SeedingIssueStatus(StrEnum):
    OPEN = "OPEN"
    ACKED = "ACKED"
    REPAIRING = "REPAIRING"
    RESOLVED = "RESOLVED"
    IGNORED = "IGNORED"


# 只有这些状态占「同类异常唯一活跃」的部分唯一索引槽位。
ACTIVE_ISSUE_STATUSES: tuple[SeedingIssueStatus, ...] = (
    SeedingIssueStatus.OPEN,
    SeedingIssueStatus.ACKED,
    SeedingIssueStatus.REPAIRING,
)


class SeedingIssueResolution(StrEnum):
    AUTO = "AUTO"
    MANUAL = "MANUAL"
    REPAIRED = "REPAIRED"
    GONE = "GONE"


class SeedingRunStatus(StrEnum):
    RUNNING = "RUNNING"
    OK = "OK"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class SeedingRunTrigger(StrEnum):
    SCHEDULED = "SCHEDULED"
    MANUAL = "MANUAL"


class SeedingIdentitySource(StrEnum):
    """种子身份来源，按可信度排序。"""

    COMMENT = "COMMENT"
    PLAN = "PLAN"
    NAME_SIZE = "NAME_SIZE"
    MANUAL = "MANUAL"


class SeedingWatchPresence(StrEnum):
    ACTIVE = "ACTIVE"
    GONE = "GONE"
    SITE_ONLY = "SITE_ONLY"


class SeedingSuggestion(StrEnum):
    RETRIEVE_TORRENT = "RETRIEVE_TORRENT"
    RECHECK = "RECHECK"
    RESEED = "RESEED"
    NONE = "NONE"


class SiteProbeOutcome(StrEnum):
    """站点侧单次观测结果。

    UNKNOWN 覆盖超时、403、429、5xx 与解析失败，这些情况绝不参与判定，
    也不会清除已有异常。
    """

    FOUND = "FOUND"
    MISSING = "MISSING"
    UNKNOWN = "UNKNOWN"


_BASE_SCORE: dict[SeedingIssueKind, int] = {
    SeedingIssueKind.SITE_MISSING: 60,
    SeedingIssueKind.SITE_ONLY: 50,
    SeedingIssueKind.TRACKER_ERROR: 40,
    SeedingIssueKind.FILE_MISSING: 55,
}

_SUGGESTION: dict[SeedingIssueKind, SeedingSuggestion] = {
    SeedingIssueKind.SITE_MISSING: SeedingSuggestion.RETRIEVE_TORRENT,
    SeedingIssueKind.SITE_ONLY: SeedingSuggestion.RESEED,
    SeedingIssueKind.TRACKER_ERROR: SeedingSuggestion.RECHECK,
    SeedingIssueKind.FILE_MISSING: SeedingSuggestion.RECHECK,
}


def suggestion_for(kind: SeedingIssueKind) -> SeedingSuggestion:
    return _SUGGESTION[kind]


def hnr_remaining_hours(
    *, required_hours: float | None, seeding_seconds: int | None
) -> float | None:
    """站点要求的剩余做种时长。

    站点规则未知或本地做种时长未知时返回 None —— 未知不等于「已满足」，
    调用方必须据此显示为「规则未知」而不是 0 风险。
    """
    if required_hours is None or seeding_seconds is None:
        return None
    return max(0.0, required_hours - seeding_seconds / 3600.0)


def risk_score(
    kind: SeedingIssueKind,
    *,
    hnr_remaining: float | None = None,
    persistent_days: float = 0.0,
    sole_source: bool = False,
) -> int:
    score = _BASE_SCORE[kind]
    if hnr_remaining is not None:
        if hnr_remaining < HNR_CRITICAL_HOURS:
            score += 30
        elif hnr_remaining < HNR_WARNING_HOURS:
            score += 15
    if persistent_days > 7:
        score += 15
    elif persistent_days > 3:
        score += 10
    if sole_source:
        score += 10
    return min(score, 100)


def severity_for(score: int) -> SeedingIssueSeverity:
    if score >= SEVERITY_HIGH_MIN:
        return SeedingIssueSeverity.HIGH
    if score >= SEVERITY_MEDIUM_MIN:
        return SeedingIssueSeverity.MEDIUM
    return SeedingIssueSeverity.LOW


def should_confirm_missing(
    outcome: SiteProbeOutcome, consecutive_misses: int, threshold: int
) -> bool:
    """只有明确的 MISSING 连续命中才允许建异常。"""
    return outcome is SiteProbeOutcome.MISSING and consecutive_misses >= max(1, threshold)


def should_clear_issue(outcome: SiteProbeOutcome) -> bool:
    """FOUND 才能自动恢复；UNKNOWN(站点不可达) 不得清除异常。"""
    return outcome is SiteProbeOutcome.FOUND


def status_on_redetect(current: SeedingIssueStatus | None) -> SeedingIssueStatus:
    """再次命中时的目标状态。RESOLVED / IGNORED 视为复发，复用原记录。"""
    if current is None:
        return SeedingIssueStatus.OPEN
    if current in (SeedingIssueStatus.RESOLVED, SeedingIssueStatus.IGNORED):
        return SeedingIssueStatus.OPEN
    return current


def status_on_clear(current: SeedingIssueStatus) -> SeedingIssueStatus:
    if current in (SeedingIssueStatus.OPEN, SeedingIssueStatus.ACKED):
        return SeedingIssueStatus.RESOLVED
    if current is SeedingIssueStatus.REPAIRING:
        return SeedingIssueStatus.RESOLVED
    return current


@dataclass(frozen=True, slots=True)
class SiteProbeTally:
    site_key: str
    probed: int
    missing: int
    unknown: int


def is_site_degraded(
    tally: SiteProbeTally,
    *,
    ratio: float = DEFAULT_DEGRADE_RATIO,
    min_samples: int = DEFAULT_DEGRADE_MIN_SAMPLES,
) -> bool:
    """判定整站是否进入降级。

    样本不足或明确未找到比例未超阈值时视为正常。命中时本次该站点的所有
    结果都按 UNKNOWN 处理，避免凭证过期被误报成大面积删种。
    """
    if tally.probed < max(1, min_samples):
        return False
    return tally.missing / tally.probed > ratio


def tracker_is_unregistered(message: str | None) -> bool:
    """识别 tracker 侧明确的「种子已不在站上」文案，仅用于提示，不单独建异常。"""
    if not message:
        return False
    lowered = message.casefold()
    return any(
        marker in lowered
        for marker in ("unregistered", "not registered", "not found", "torrent not found")
    )
