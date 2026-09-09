from __future__ import annotations

from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network, ip_address, ip_network

from starlette.datastructures import MutableHeaders

IPAddress = IPv4Address | IPv6Address
IPNetwork = IPv4Network | IPv6Network


@dataclass(frozen=True, slots=True)
class RequestNetworkContext:
    client_source: str
    scheme: str
    trusted_proxy: bool


class TrustedProxyPolicy:
    """只在直连来源可信时消费转发头，并从右向左剥离可信代理链。"""

    def __init__(self, entries: tuple[str, ...]) -> None:
        self._networks: tuple[IPNetwork, ...] = tuple(
            ip_network(entry, strict=False) for entry in entries
        )

    def resolve(
        self,
        *,
        direct_host: str | None,
        direct_scheme: str,
        forwarded_for: str | None,
        forwarded_proto: str | None,
    ) -> RequestNetworkContext:
        direct_ip = self._parse_ip(direct_host)
        if direct_ip is None or not self._is_trusted(direct_ip):
            return RequestNetworkContext(direct_host or "unknown", direct_scheme, False)

        client_source = self._resolve_forwarded_client(direct_ip, forwarded_for)
        scheme = self._resolve_forwarded_scheme(direct_scheme, forwarded_proto)
        return RequestNetworkContext(client_source, scheme, True)

    def _resolve_forwarded_client(self, direct_ip: IPAddress, forwarded_for: str | None) -> str:
        if not forwarded_for:
            return str(direct_ip)
        parsed: list[IPAddress] = []
        for raw in forwarded_for.split(","):
            address = self._parse_ip(raw.strip())
            if address is None:
                return str(direct_ip)
            parsed.append(address)

        chain = [*parsed, direct_ip]
        for address in reversed(chain):
            if not self._is_trusted(address):
                return str(address)
        return str(parsed[0]) if parsed else str(direct_ip)

    @staticmethod
    def _resolve_forwarded_scheme(direct_scheme: str, forwarded_proto: str | None) -> str:
        if not forwarded_proto:
            return direct_scheme
        value = forwarded_proto.split(",")[-1].strip().lower()
        return value if value in {"http", "https"} else direct_scheme

    def _is_trusted(self, address: IPAddress) -> bool:
        return any(
            address.version == network.version and address in network for network in self._networks
        )

    @staticmethod
    def _parse_ip(value: str | None) -> IPAddress | None:
        if not value:
            return None
        try:
            return ip_address(value)
        except ValueError:
            return None


def apply_security_headers(*, path: str, scheme: str, headers: MutableHeaders) -> None:
    """对 API 默认使用严格 CSP；交互文档仅放行 FastAPI Swagger 所需 CDN/内联脚本。"""

    headers["X-Content-Type-Options"] = "nosniff"
    headers["Referrer-Policy"] = "no-referrer"
    headers["X-Frame-Options"] = "DENY"
    headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if path == "/api/docs":
        csp = (
            "default-src 'none'; script-src 'unsafe-inline' https://cdn.jsdelivr.net; "
            "style-src 'unsafe-inline' https://cdn.jsdelivr.net; "
            "img-src data: https://fastapi.tiangolo.com; connect-src 'self'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
        )
    else:
        csp = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    headers["Content-Security-Policy"] = csp
    if scheme == "https":
        headers["Strict-Transport-Security"] = "max-age=31536000"
