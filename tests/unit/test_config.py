from pathlib import Path

import pytest
from pydantic import ValidationError

from backend.app.config import AppSettings


def test_runtime_paths_follow_config_directory(tmp_path: Path) -> None:
    config_dir = (tmp_path / "config").resolve()
    settings = AppSettings(
        config_dir=config_dir,
        data_dir=(tmp_path / "data").resolve(),
        secret_key_file=None,
    )

    assert settings.database_path == config_dir / "packbreaker.db"
    assert settings.instance_lock_path == config_dir / "packbreaker.lock"
    assert settings.resolved_secret_key_file == config_dir / "secret.key"


def test_environment_values_are_typed_and_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PACKBREAKER_PORT", "8123")
    monkeypatch.setenv("PACKBREAKER_LOG_LEVEL", "debug")
    monkeypatch.setenv("PACKBREAKER_TRUSTED_PROXIES", "10.0.0.0/8, 192.168.0.0/16")

    settings = AppSettings()

    assert settings.port == 8123
    assert settings.log_level == "DEBUG"
    assert settings.trusted_proxy_list == ("10.0.0.0/8", "192.168.0.0/16")


def test_relative_startup_paths_are_rejected() -> None:
    with pytest.raises(ValidationError):
        AppSettings(config_dir=Path("relative"))


def test_unknown_timezone_is_rejected() -> None:
    with pytest.raises(ValidationError):
        AppSettings(timezone="Invalid/PackBreaker")


def test_invalid_trusted_proxy_is_rejected() -> None:
    with pytest.raises(ValidationError):
        AppSettings(trusted_proxies="10.0.0.0/8,not-an-ip")
