from __future__ import annotations

import os
import sys
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, build_opener


def main() -> int:
    port = os.environ.get("PACKBREAKER_PORT", "8000")
    url = f"http://127.0.0.1:{port}/api/v1/health/ready"
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(url, timeout=3) as response:
            return 0 if response.status == 200 else 1
    except (HTTPError, URLError, TimeoutError, ValueError):
        return 1


if __name__ == "__main__":
    sys.exit(main())
