"""Outbound URL host safety for administrator-configured endpoints.

This is deliberately not a blanket private-range ban: local downloaders and
self-hosted CookieCloud servers are valid RFC1918/loopback targets. What is
never valid is a cloud metadata endpoint, link-local address, multicast or
reserved/unspecified address, which are the classic SSRF pivot targets.
"""

from __future__ import annotations

import ipaddress

_METADATA_HOSTNAMES = frozenset(
    {
        "metadata.google.internal",
        "metadata.goog",
        "instance-data.ec2.internal",
        "169.254.169.254.nip.io",
    }
)


def is_blocked_outbound_host(host: str) -> bool:
    """Return True when the host must never be used for an outbound request."""
    normalized = host.strip().rstrip(".").casefold()
    if not normalized:
        return True
    if normalized in _METADATA_HOSTNAMES:
        return True
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        # Hostname: redirect handling is disabled at every call site, and the
        # trusted-site registry pins known hosts. Literal-IP pivots are blocked
        # above; DNS-rebinding mitigation belongs at the HTTP transport layer.
        return False
    return bool(
        address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def assert_safe_outbound_host(host: str, *, label: str) -> None:
    if is_blocked_outbound_host(host):
        raise ValueError(f"{label}主机地址不被允许")
