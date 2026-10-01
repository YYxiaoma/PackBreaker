"""Explicitly gated read-only acceptance for an external easychen/CookieCloud service.

This script only performs CookieCloud GET + local decryption. It never opens the
PackBreaker database and never writes site credentials. It is intentionally
excluded from the default test suite and requires two affirmative CLI flags.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import stat
from pathlib import Path
from typing import Any

from backend.app.infrastructure.cookiecloud import (
    CookieCloudClient,
    CookieCloudError,
    decrypt_cookiecloud_payload,
)

_SECRET_PATH = Path(__file__).resolve().parents[1] / "runtime/cookiecloud-acceptance.secret"


async def check_cookiecloud(
    config: dict[str, Any],
    *,
    client: CookieCloudClient | None = None,
) -> dict[str, object]:
    server_url = config.get("server_url")
    uuid = config.get("uuid")
    password = config.get("password")
    timeout_seconds = config.get("timeout_seconds", 10)

    if (
        not isinstance(server_url, str)
        or not server_url.strip()
        or not isinstance(uuid, str)
        or not uuid.strip()
        or not isinstance(password, str)
        or not password
        or not isinstance(timeout_seconds, (int, float))
        or isinstance(timeout_seconds, bool)
        or not 1 <= float(timeout_seconds) <= 60
    ):
        return {"status": "CONFIG_BLOCKED"}

    stage = "FETCH"
    try:
        envelope = await (client or CookieCloudClient()).fetch(
            server_url=server_url,
            uuid=uuid,
            timeout_seconds=float(timeout_seconds),
        )
        stage = "DECRYPT"
        payload = decrypt_cookiecloud_payload(
            uuid=uuid,
            password=password,
            encrypted=envelope.encrypted,
            crypto_type=envelope.crypto_type,
        )
    except CookieCloudError as exc:
        return {
            "status": "FAILED",
            "stage": stage,
            "error_code": exc.code,
            "retryable": exc.retryable,
        }
    except (ValueError, OSError):
        return {
            "status": "FAILED",
            "stage": stage,
            "error_code": "COOKIECLOUD_INPUT_INVALID",
            "retryable": False,
        }

    return {
        "status": "VALID_PAYLOAD",
        "crypto_type": envelope.crypto_type.value,
        "domain_count": len(payload.cookie_data),
        "cookie_count": sum(len(items) for items in payload.cookie_data.values()),
        "update_time_present": bool(payload.update_time),
        "site_state_mutated": False,
        "password_sent_to_server": False,
    }


def _load_config(path: Path) -> dict[str, Any] | None:
    try:
        if not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return None
    return value if isinstance(value, dict) else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="只读验证 easychen/CookieCloud 真实服务的 GET 与本地解密链路"
    )
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--acknowledge-cookie-read", action="store_true")
    args = parser.parse_args(argv)

    if not (args.live and args.acknowledge_cookie_read):
        print("未发起 CookieCloud 请求：须同时指定 --live 与 --acknowledge-cookie-read")
        return 2

    config = _load_config(_SECRET_PATH)
    if config is None:
        print("配置文件不存在、格式错误或权限不为 0600；未发起 CookieCloud 请求")
        return 2

    result = asyncio.run(check_cookiecloud(config))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("status") == "VALID_PAYLOAD" else 1


if __name__ == "__main__":
    raise SystemExit(main())
