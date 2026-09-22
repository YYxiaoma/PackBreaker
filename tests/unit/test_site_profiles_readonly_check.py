"""Offline tests only: this diagnostic must not need real site accounts or network."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from backend.app.domain.site_adapter import SiteUserProfile
from backend.app.domain.site_config import SiteKind
from backend.app.infrastructure.security import MasterKeyFile, SecretCipher
from scripts import check_site_profiles_readonly as check


def _synthetic_config(tmp_path: Path, *, enabled: bool = True, proxy: bool = False) -> Path:
    config = tmp_path / "local-config"
    config.mkdir()
    key = MasterKeyFile.load_or_create(config / "secret.key")
    secret = SecretCipher(key).encrypt(
        secret_id="synthetic-secret",
        kind="API_KEY",
        key_version=1,
        plaintext=b"SYNTHETIC-CREDENTIAL-NEVER-OUTPUT",
    )
    with sqlite3.connect(config / "packbreaker.db") as connection:
        connection.executescript(
            """CREATE TABLE site (
                 id TEXT, type TEXT, enabled INTEGER, version INTEGER, secret_id TEXT,
                 credential_kind TEXT, request_timeout_seconds INTEGER, user_agent TEXT,
                 browser_emulation_enabled INTEGER, proxy_enabled INTEGER);
               CREATE TABLE secret (id TEXT, kind TEXT, key_version INTEGER, ciphertext TEXT);"""
        )
        connection.execute(
            "INSERT INTO site VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "synthetic-site",
                "MTEAM",
                int(enabled),
                4,
                "synthetic-secret",
                "API_KEY",
                12,
                None,
                0,
                int(proxy),
            ),
        )
        connection.execute(
            "INSERT INTO secret VALUES (?, ?, ?, ?)",
            ("synthetic-secret", "API_KEY", 1, secret),
        )
    return config


def _factory(monkeypatch: pytest.MonkeyPatch, adapter: object) -> None:
    class SyntheticFactory:
        def create(self, **kwargs: object) -> object:
            assert kwargs["kind"] is SiteKind.MTEAM
            assert kwargs["base_url"] == "https://kp.m-team.cc"
            assert kwargs["credential"] == "SYNTHETIC-CREDENTIAL-NEVER-OUTPUT"
            return adapter

    monkeypatch.setattr(check, "SiteAdapterFactory", SyntheticFactory)


@pytest.mark.asyncio
async def test_readonly_check_only_emits_sanitized_presence_and_known_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _synthetic_config(tmp_path)

    class SyntheticAdapter:
        async def fetch_user_profile(self) -> SiteUserProfile:
            return SiteUserProfile(
                "mteam",
                uid="SYNTHETIC-PRIVATE-UID",
                username="SYNTHETIC-PRIVATE-USERNAME",
                user_level="Example Member",
                torrents_posted=0,
                seeding_count=None,
                seeding_size_bytes=1073741824,
                bonus_per_hour=0.0004,
                seeding_points=None,
            )

    _factory(monkeypatch, SyntheticAdapter())
    assert await check.inspect(config, (SiteKind.MTEAM,)) == 1
    output = capsys.readouterr().out
    assert "当前账号已确认" in output
    assert "字段可读取性：未完成" in output
    assert "用户等级：已取得" in output
    assert "发种数：真实零值" in output
    assert "做种数：缺失" in output
    assert "做种量：已取得" in output
    assert "每小时魔力值：已取得" in output
    for private in (
        "SYNTHETIC-PRIVATE-UID",
        "SYNTHETIC-PRIVATE-USERNAME",
        "SYNTHETIC-CREDENTIAL-NEVER-OUTPUT",
        "Example Member",
        "1073741824",
        "0.0004",
    ):
        assert private not in output


@pytest.mark.asyncio
async def test_successful_field_readability_does_not_claim_page_value_reconciliation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _synthetic_config(tmp_path)

    class SyntheticAdapter:
        async def fetch_user_profile(self) -> SiteUserProfile:
            return SiteUserProfile(
                "mteam",
                uid="SYNTHETIC-PRIVATE-UID",
                user_level="Member",
                torrents_posted=0,
                seeding_count=0,
                seeding_size_bytes=0,
                bonus_per_hour=0.0004,
                seeding_points=0,
            )

    _factory(monkeypatch, SyntheticAdapter())
    assert await check.inspect(config, (SiteKind.MTEAM,)) == 0
    output = capsys.readouterr().out
    assert "已取得全部目标字段；仍需站点页面同一时点数值对账" in output
    assert "SYNTHETIC-PRIVATE-UID" not in output
    assert "0.0004" not in output


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["version", "deleted", "disabled"])
async def test_changed_site_config_discards_fetched_account_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    config = _synthetic_config(tmp_path)

    class SyntheticAdapter:
        async def fetch_user_profile(self) -> SiteUserProfile:
            with sqlite3.connect(config / "packbreaker.db") as connection:
                if mutation == "version":
                    connection.execute("UPDATE site SET version=5")
                elif mutation == "deleted":
                    connection.execute("DELETE FROM site")
                else:
                    connection.execute("UPDATE site SET enabled=0")
            return SiteUserProfile("mteam", uid="SYNTHETIC-PRIVATE-UID", seeding_count=7)

    _factory(monkeypatch, SyntheticAdapter())
    status, fields = await check.check_one(SiteKind.MTEAM, config)
    assert status == "读取期间站点配置已变化，本次数据作废"
    assert fields is None


@pytest.mark.asyncio
@pytest.mark.parametrize("uid", [None, "", "  "])
async def test_missing_own_account_identity_does_not_accept_metrics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, uid: str | None
) -> None:
    config = _synthetic_config(tmp_path)

    class SyntheticAdapter:
        async def fetch_user_profile(self) -> SiteUserProfile:
            return SiteUserProfile("mteam", uid=uid, seeding_count=100)

    _factory(monkeypatch, SyntheticAdapter())
    status, fields = await check.check_one(SiteKind.MTEAM, config)
    assert status == "当前账号身份未确认，字段不予验收"
    assert fields is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("enabled", "proxy", "expected"),
    [
        (False, False, "站点未启用"),
        (True, True, "已配置站点代理，请使用现有 Web 用户详情只读入口验收"),
    ],
)
async def test_diagnostic_never_bypasses_site_disabled_or_proxy_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enabled: bool, proxy: bool, expected: str
) -> None:
    config = _synthetic_config(tmp_path, enabled=enabled, proxy=proxy)

    class MustNotCreateAdapter:
        def __init__(self) -> None:
            raise AssertionError("Forbidden external request")

    monkeypatch.setattr(check, "SiteAdapterFactory", MustNotCreateAdapter)
    status, fields = await check.check_one(SiteKind.MTEAM, config)
    assert (status, fields) == (expected, None)


def test_cli_defaults_to_no_requests_and_never_creates_a_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "not-created"
    monkeypatch.setattr("sys.argv", ["check_site_profiles_readonly", "--config-dir", str(config)])
    assert check.main() == 2
    assert "默认不访问站点" in capsys.readouterr().out
    assert not config.exists()


@pytest.mark.asyncio
async def test_legacy_local_config_schema_fails_closed_without_guessing_proxy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "legacy-config"
    config.mkdir()
    with sqlite3.connect(config / "packbreaker.db") as connection:
        connection.execute(
            "CREATE TABLE site (id TEXT, type TEXT, enabled INTEGER, version INTEGER, "
            "secret_id TEXT, credential_kind TEXT)"
        )
        connection.execute(
            "INSERT INTO site VALUES (?, ?, ?, ?, ?, ?)",
            ("synthetic-site", "MTEAM", 1, 4, "synthetic-secret", "API_KEY"),
        )

    class MustNotCreateAdapter:
        def __init__(self) -> None:
            raise AssertionError("No network or credential use is allowed with old schema")

    monkeypatch.setattr(check, "SiteAdapterFactory", MustNotCreateAdapter)
    status, fields = await check.check_one(SiteKind.MTEAM, config)
    assert status == "本地配置模式不兼容；不能推断代理或浏览器设置，请用当前实例的 Web 详情验收"
    assert fields is None


def test_blank_level_is_not_considered_a_real_grade() -> None:
    assert (
        check.field_states(SiteUserProfile("mteam", uid="synthetic", user_level=" \t"))["用户等级"]
        == "缺失"
    )


@pytest.mark.parametrize("level", ["1042", " 1042 \t"])
def test_numeric_level_id_is_not_a_confirmed_level_name(level: str) -> None:
    """Match the Web acceptance report: a rank ID is not an official name."""
    fields = check.field_states(SiteUserProfile("mteam", uid="synthetic", user_level=level))
    assert fields["用户等级"] == "缺失"


@pytest.mark.asyncio
async def test_numeric_level_does_not_mark_all_target_fields_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _synthetic_config(tmp_path)

    class SyntheticAdapter:
        async def fetch_user_profile(self) -> SiteUserProfile:
            return SiteUserProfile(
                "mteam",
                uid="SYNTHETIC-PRIVATE-UID",
                user_level="1042",
                torrents_posted=0,
                seeding_count=0,
                seeding_size_bytes=0,
                bonus_per_hour=0.0004,
                seeding_points=0,
            )

    _factory(monkeypatch, SyntheticAdapter())
    assert await check.inspect(config, (SiteKind.MTEAM,)) == 1
    output = capsys.readouterr().out
    assert "用户等级：缺失" in output
    assert "字段可读取性：未完成" in output
    assert "1042" not in output
    assert "SYNTHETIC-PRIVATE-UID" not in output
