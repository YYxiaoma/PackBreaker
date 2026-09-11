from ipaddress import ip_network
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppSettings(BaseSettings):
    """只包含应用启动前必须知道的非业务配置。"""

    model_config = SettingsConfigDict(
        env_prefix="PACKBREAKER_",
        case_sensitive=False,
        extra="ignore",
    )

    host: str = "0.0.0.0"
    port: int = Field(default=8000, ge=1, le=65535)
    config_dir: Path = Path("/config")
    data_dir: Path = Path("/data")
    frontend_dir: Path | None = None
    secret_key_file: Path | None = None
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    timezone: str = "Asia/Shanghai"
    trusted_proxies: str = ""
    task_driver_interval_seconds: float = Field(default=15.0, ge=1.0, le=3600.0)
    task_driver_limit: int = Field(default=100, ge=1, le=1000)
    task_driver_max_steps_per_task: int = Field(default=4, ge=1, le=16)
    notification_driver_interval_seconds: float = Field(default=5.0, ge=1.0, le=3600.0)
    notification_driver_limit: int = Field(default=50, ge=1, le=500)
    notification_max_attempts: int = Field(default=5, ge=1, le=20)

    @field_validator("config_dir", "data_dir")
    @classmethod
    def require_absolute_directory(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("启动目录必须使用绝对路径")
        return value

    @field_validator("secret_key_file", "frontend_dir")
    @classmethod
    def require_absolute_optional_path(cls, value: Path | None) -> Path | None:
        if value is not None and not value.is_absolute():
            raise ValueError("可选启动路径必须使用绝对路径")
        return value

    @field_validator("log_level", mode="before")
    @classmethod
    def normalize_log_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @field_validator("trusted_proxies")
    @classmethod
    def validate_trusted_proxies(cls, value: str) -> str:
        entries = [entry.strip() for entry in value.split(",") if entry.strip()]
        for entry in entries:
            try:
                ip_network(entry, strict=False)
            except ValueError as exc:
                raise ValueError("可信代理必须使用合法 IP 或 CIDR") from exc
        return ",".join(entries)

    @field_validator("timezone")
    @classmethod
    def require_known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("未知时区") from exc
        return value

    @property
    def database_path(self) -> Path:
        return self.config_dir / "packbreaker.db"

    @property
    def instance_lock_path(self) -> Path:
        return self.config_dir / "packbreaker.lock"

    @property
    def resolved_secret_key_file(self) -> Path:
        return self.secret_key_file or self.config_dir / "secret.key"

    @property
    def trusted_proxy_list(self) -> tuple[str, ...]:
        return tuple(value.strip() for value in self.trusted_proxies.split(",") if value.strip())
