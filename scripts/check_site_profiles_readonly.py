"""Explicit, local-only, privacy-preserving v1.0.1 site-profile acceptance probe.

Run manually from a development checkout with an existing *local* config:
    .venv/bin/python -m scripts.check_site_profiles_readonly --live \
        --config-dir /workspace/PackBreaker/runtime/config

The published runtime Docker image does not include scripts/; for a Synology
instance use its existing authenticated Web user-details read-only endpoint
instead of copying credentials or mounting another instance's config here.

No requests happen without --live. Reads existing SQLite and key only; never
creates a database, changes site configuration, saves responses, or prints
credentials, account identifiers, names, metric values, URLs or exceptions.
The runner is NOT a same-time reconciliation against third-party site pages.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Literal

from backend.app.domain.site_adapter import SiteUserProfile
from backend.app.domain.site_config import (
    SiteCredentialKind,
    SiteKind,
    required_site_credential_kind,
    trusted_site_base_url,
)
from backend.app.infrastructure.adapters.site_errors import SiteAdapterError
from backend.app.infrastructure.adapters.sites import SiteAdapterFactory
from backend.app.infrastructure.security import MasterKeyFile, SecretCipher

_SITES = (SiteKind.MTEAM, SiteKind.HHCLUB, SiteKind.HDTIME)
_FIELDS = (
    ("用户等级", "user_level"),
    ("发种数", "torrents_posted"),
    ("做种数", "seeding_count"),
    ("做种量", "seeding_size_bytes"),
    ("每小时魔力值", "bonus_per_hour"),
    ("做种积分", "seeding_points"),
)
_EXPECTED_ID = {SiteKind.MTEAM: "mteam", SiteKind.HHCLUB: "hhclub", SiteKind.HDTIME: "hdtime"}
MetricState = Literal["已取得", "真实零值", "缺失"]


class LocalConfigVersionError(RuntimeError):
    """A local database schema cannot prove the current site runtime settings."""


def field_states(profile: SiteUserProfile) -> dict[str, MetricState]:
    """Serialize only presence, never the user's identifiers or metric values."""
    output: dict[str, MetricState] = {}
    for label, attribute in _FIELDS:
        value = getattr(profile, attribute)
        # A numeric-only rank ID is not the site's confirmed level name. Keep
        # this offline diagnostic consistent with the authenticated Web report.
        unconfirmed_level = (
            attribute == "user_level"
            and isinstance(value, str)
            and re.fullmatch(r"[0-9]+", value.strip()) is not None
        )
        output[label] = (
            "缺失"
            if value is None or (isinstance(value, str) and not value.strip()) or unconfirmed_level
            else "真实零值"
            if value == 0
            else "已取得"
        )
    return output


def _open_readonly(db: Path) -> sqlite3.Connection:
    if not db.is_file():
        raise OSError("database_unavailable")
    connection = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True, timeout=2)
    connection.row_factory = sqlite3.Row
    return connection


def _configured_site(db: Path, kind: SiteKind) -> sqlite3.Row | None:
    with closing(_open_readonly(db)) as connection:
        columns = {record["name"] for record in connection.execute("PRAGMA table_info(site)")}
        required = {
            "id",
            "type",
            "enabled",
            "version",
            "secret_id",
            "credential_kind",
            "request_timeout_seconds",
            "user_agent",
            "browser_emulation_enabled",
            "proxy_enabled",
        }
        if not required.issubset(columns):
            # In particular, never silently treat an unknown proxy setting as
            # disabled when probing a retained, older workspace database.
            raise LocalConfigVersionError
        rows = connection.execute(
            """SELECT s.id AS site_id, s.type, s.enabled, s.version, s.secret_id,
                      s.credential_kind, s.request_timeout_seconds, s.user_agent,
                      s.browser_emulation_enabled, s.proxy_enabled,
                      t.id AS key_id, t.kind AS key_kind,
                      t.key_version, t.ciphertext
                 FROM site AS s LEFT JOIN secret AS t ON s.secret_id = t.id
                 WHERE s.type = ? LIMIT 2""",
            (kind.value,),
        ).fetchall()
    return rows[0] if len(rows) == 1 else None


def _same_site_version(db: Path, row: sqlite3.Row) -> bool:
    with closing(_open_readonly(db)) as connection:
        current = connection.execute(
            "SELECT version, secret_id, enabled FROM site WHERE id = ?",
            (row["site_id"],),
        ).fetchone()
    return (
        current is not None
        and current["version"] == row["version"]
        and current["secret_id"] == row["secret_id"]
        and bool(current["enabled"])
    )


async def check_one(kind: SiteKind, config_dir: Path) -> tuple[str, dict[str, MetricState] | None]:
    """One current-account adapter read at most; no retries or write endpoints."""
    db = config_dir / "packbreaker.db"
    try:
        row = _configured_site(db, kind)
    except LocalConfigVersionError:
        return "本地配置模式不兼容；不能推断代理或浏览器设置，请用当前实例的 Web 详情验收", None
    except (OSError, sqlite3.Error):
        return "本地配置不可读取", None
    if row is None:
        return "站点配置缺失或不唯一", None
    if not row["enabled"]:
        return "站点未启用", None
    if row["proxy_enabled"]:
        # Never silently bypass the per-site proxy or independently recreate
        # credentials for a diagnostic command. Use the authenticated UI then.
        return "已配置站点代理，请使用现有 Web 用户详情只读入口验收", None
    if row["key_id"] is None or row["secret_id"] is None:
        return "凭证未配置", None
    try:
        credential_kind = SiteCredentialKind(row["credential_kind"])
        if credential_kind is not required_site_credential_kind(kind):
            return "凭证类型不匹配", None
        cipher = SecretCipher(MasterKeyFile.load_existing(config_dir / "secret.key"))
        credential = cipher.decrypt(
            secret_id=row["key_id"],
            kind=row["key_kind"],
            key_version=row["key_version"],
            ciphertext=row["ciphertext"],
        ).decode("utf-8")
        adapter = SiteAdapterFactory().create(
            kind=kind,
            base_url=trusted_site_base_url(kind),
            credential_kind=credential_kind,
            credential=credential,
            timeout_seconds=min(20, max(1, row["request_timeout_seconds"])),
            user_agent=row["user_agent"],
            browser_emulation_enabled=bool(row["browser_emulation_enabled"]),
            proxy_url=None,
        )
    except Exception:
        return "本地凭证或配置不可用", None
    try:
        # A single fetch may access the adapter's known, read-only subpages.
        profile = await asyncio.wait_for(adapter.fetch_user_profile(), timeout=75)
    except SiteAdapterError as exc:
        code = exc.code if re.fullmatch(r"SITE_[A-Z0-9_]{1,48}", exc.code) else "SITE_ERROR"
        return f"读取失败：{code}", None
    except TimeoutError:
        return "读取超时", None
    except Exception:
        return "读取失败：未分类", None
    try:
        if not _same_site_version(db, row):
            return "读取期间站点配置已变化，本次数据作废", None
    except (OSError, sqlite3.Error):
        return "无法复核配置版本，本次数据作废", None
    if profile.site_id != _EXPECTED_ID[kind] or not profile.uid or not profile.uid.strip():
        return "当前账号身份未确认，字段不予验收", None
    return "当前账号已确认，字段可读取性如下（并非页面数值对账）", field_states(profile)


async def inspect(config_dir: Path, selected: tuple[SiteKind, ...]) -> int:
    complete = True
    for kind in selected:
        status, fields = await check_one(kind, config_dir)
        print(f"{kind.value}：{status}")
        if fields is not None:
            for label, state in fields.items():
                print(f"  {label}：{state}")
        if fields is None or any(value == "缺失" for value in fields.values()):
            complete = False
    print(
        "字段可读取性：已取得全部目标字段；仍需站点页面同一时点数值对账。"
        if complete
        else "字段可读取性：未完成；缺失或未确认的字段不得作为发布验收通过证据。"
    )
    return 0 if complete else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="三站点当前账号资料脱敏只读验收；不输出统计数值")
    parser.add_argument("--live", action="store_true", help="明确授权本次在线只读检查")
    parser.add_argument("--config-dir", type=Path, help="现有本地 PackBreaker 配置目录")
    parser.add_argument("--site", choices=[kind.value for kind in _SITES], help="只检查指定站点")
    args = parser.parse_args()
    if not args.live:
        print("默认不访问站点。需要主动指定 --live 和 --config-dir 才会进行只读检查。")
        return 2
    if args.config_dir is None or not args.config_dir.is_absolute():
        print("必须指定已存在的绝对路径 --config-dir；不会创建配置文件或数据库。")
        return 2
    selected = (SiteKind(args.site),) if args.site else _SITES
    return asyncio.run(inspect(args.config_dir, selected))


if __name__ == "__main__":
    raise SystemExit(main())
