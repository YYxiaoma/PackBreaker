from ipaddress import ip_network
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator
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
    log_file_max_bytes: int = Field(default=2 * 1024 * 1024, ge=64 * 1024, le=64 * 1024 * 1024)
    log_file_backup_count: int = Field(default=4, ge=1, le=20)
    timezone: str = "Asia/Shanghai"
    trusted_proxies: str = ""
    admin_username: str = "admin"
    admin_password: SecretStr | None = None
    task_driver_interval_seconds: float = Field(default=15.0, ge=1.0, le=3600.0)
    task_driver_limit: int = Field(default=100, ge=1, le=1000)
    task_driver_max_steps_per_task: int = Field(default=4, ge=1, le=16)
    task_definition_driver_interval_seconds: float = Field(default=15.0, ge=1.0, le=3600.0)
    task_definition_driver_scan_limit: int = Field(default=20, ge=1, le=500)
    task_definition_driver_retry_limit: int = Field(default=20, ge=1, le=500)
    task_definition_directory_scan_batch_size: int = Field(default=250, ge=1, le=5000)
    notification_driver_interval_seconds: float = Field(default=5.0, ge=1.0, le=3600.0)
    notification_driver_limit: int = Field(default=50, ge=1, le=500)
    notification_max_attempts: int = Field(default=5, ge=1, le=20)
    ai_telegram_driver_interval_seconds: float = Field(default=1.0, ge=0.2, le=3600.0)
    ai_telegram_poll_timeout_seconds: int = Field(default=20, ge=5, le=50)
    ai_telegram_poll_limit: int = Field(default=20, ge=1, le=100)
    backup_driver_interval_seconds: float = Field(default=60.0, ge=1.0, le=3600.0)

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

    @field_validator("admin_username")
    @classmethod
    def validate_admin_username(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or len(normalized) > 80:
            raise ValueError("管理员用户名不能为空且最长 80 个字符")
        if any(char in normalized for char in ("\r", "\n", "\x00")):
            raise ValueError("管理员用户名不能包含控制字符")
        return normalized

    @field_validator("admin_password")
    @classmethod
    def validate_admin_password(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        password = value.get_secret_value()
        if not 12 <= len(password) <= 256:
            raise ValueError("管理员密码长度必须在 12 到 256 个字符之间")
        return value

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
    def log_dir(self) -> Path:
        return self.config_dir / "logs"

    @property
    def updater_socket_path(self) -> Path:
        return self.config_dir / "updater" / "updater.sock"

    @property
    def updater_token_path(self) -> Path:
        return self.config_dir / "updater" / "token"

    @property
    def resolved_secret_key_file(self) -> Path:
        return self.secret_key_file or self.config_dir / "secret.key"

    @property
    def trusted_proxy_list(self) -> tuple[str, ...]:
        return tuple(value.strip() for value in self.trusted_proxies.split(",") if value.strip())
