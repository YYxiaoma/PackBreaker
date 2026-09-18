from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ProxyConfig:
    """不包含代理密码明文的实例级代理配置。"""

    enabled: bool = False
    host: str | None = None
    port: int | None = None
    username: str | None = None
    credential_secret_id: str | None = None

    def __post_init__(self) -> None:
        host = self.host.strip() if self.host is not None else None
        username = self.username.strip() if self.username is not None else None
        object.__setattr__(self, "host", host or None)
        object.__setattr__(self, "username", username or None)
        if self.host is not None:
            if len(self.host) > 255 or any(char.isspace() for char in self.host):
                raise ValueError("代理地址格式无效")
            if any(token in self.host for token in ("://", "@", "/", "?", "#", "\r", "\n", "\x00")):
                raise ValueError("代理地址只能填写主机名或 IP")
        if self.username is not None and (
            len(self.username) > 255 or any(char in self.username for char in ("\r", "\n", "\x00"))
        ):
            raise ValueError("代理账号格式无效")
        if self.port is not None and not 1 <= self.port <= 65535:
            raise ValueError("代理端口必须在 1～65535 之间")
        if self.enabled and (self.host is None or self.port is None):
            raise ValueError("启用代理时必须提供代理地址和端口")
        if self.credential_secret_id is not None and self.host is None:
            raise ValueError("代理凭证不能脱离代理地址存在")

    @property
    def credential_configured(self) -> bool:
        return self.credential_secret_id is not None
