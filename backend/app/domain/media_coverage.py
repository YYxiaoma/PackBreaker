"""监控中心 · 辅种覆盖率的影片归一与分桶规则。"""

from __future__ import annotations

import re
from hashlib import sha256

from backend.app.domain.media_matching import MediaDescriptor

_NON_ALNUM_RE = re.compile(r"[^\w\u4e00-\u9fff]+", flags=re.UNICODE)


def normalize_media_title(value: str) -> str:
    normalized = _NON_ALNUM_RE.sub(" ", value.casefold()).strip()
    return " ".join(normalized.split())


def descriptor_movie_key(descriptor: MediaDescriptor) -> str:
    """Prefer explicit external IDs; otherwise fall back to title/year/resolution."""
    external = sorted(
        f"{item.namespace.strip().casefold()}:{item.value.strip().casefold()}"
        for item in descriptor.external_ids
        if item.namespace.strip() and item.value.strip()
    )
    title = normalize_media_title(descriptor.raw_name)
    if external:
        canonical = "external|" + "|".join(external)
    elif title:
        canonical = "title|" + "|".join(
            (
                title,
                str(descriptor.year or ""),
                (descriptor.resolution or "").casefold(),
            )
        )
    else:
        canonical = "raw|" + descriptor.raw_name.casefold()
    return sha256(canonical.encode("utf-8")).hexdigest()[:32]


def external_ids_movie_key(external_ids: list[dict[str, str]]) -> str | None:
    def pair(item: dict[str, str]) -> str:
        namespace = str(item.get("namespace", "")).strip().casefold()
        value = str(item.get("value", "")).strip().casefold()
        return f"{namespace}:{value}"

    pairs = sorted(
        pair(item)
        for item in external_ids
        if str(item.get("namespace", "")).strip() and str(item.get("value", "")).strip()
    )
    if not pairs:
        return None
    return sha256(("external|" + "|".join(pairs)).encode("utf-8")).hexdigest()[:32]


def coverage_bucket(coverage_count: int, *, unknown_only: bool) -> str:
    if unknown_only:
        return "UNKNOWN"
    if coverage_count <= 0:
        return "ZERO"
    if coverage_count == 1:
        return "ONE"
    if coverage_count == 2:
        return "TWO"
    return "THREE_PLUS"


def coverage_suggestion(bucket: str, *, confidence: str) -> str:
    if confidence == "LOW" or bucket == "UNKNOWN":
        return "CONFIRM_IDENTITY"
    if bucket in {"ZERO", "ONE"}:
        return "ADD_SEED"
    if bucket == "THREE_PLUS":
        return "REVIEW_LOW_YIELD"
    return "KEEP"
