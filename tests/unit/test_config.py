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


def test_unpack_match_query_budget_defaults_to_eight() -> None:
    assert AppSettings().unpack_match_max_queries_per_site == 8


def test_relative_startup_paths_are_rejected() -> None:
    with pytest.raises(ValidationError):
        AppSettings(config_dir=Path("relative"))

    with pytest.raises(ValidationError):
        AppSettings(frontend_dir=Path("frontend/dist"))


def test_unknown_timezone_is_rejected() -> None:
    with pytest.raises(ValidationError):
        AppSettings(timezone="Invalid/PackBreaker")


def test_invalid_trusted_proxy_is_rejected() -> None:
    with pytest.raises(ValidationError):
        AppSettings(trusted_proxies="10.0.0.0/8,not-an-ip")


def test_match_stage_pause_is_not_a_deployment_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PACKBREAKER_UNPACK_TEST_PAUSE_AFTER_MATCH", "true")

    settings = AppSettings()
    compose_text = (Path(__file__).resolve().parents[2] / "compose.yaml").read_text(
        encoding="utf-8"
    )

    assert not hasattr(settings, "unpack_test_pause_after_match")
    assert "PACKBREAKER_UNPACK_TEST_PAUSE_AFTER_MATCH" not in compose_text
