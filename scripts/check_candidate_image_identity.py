"""Verify a locally built candidate image before real Docker acceptance.

The expected version comes from the checked-out project, not from image labels.
This command prints no raw inspect payload or Docker error output.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_PLATFORMS = {"linux/amd64", "linux/arm64"}


def verify_candidate_image_identity(
    payload: object, *, version: str, commit: str, platform: str
) -> None:
    """Compare actual Docker image config to independent workflow inputs."""
    if platform not in _PLATFORMS or _COMMIT.fullmatch(commit) is None or not version:
        raise ValueError("candidate image identity: invalid expected identity")
    if not isinstance(payload, dict):
        raise ValueError("candidate image identity: missing image metadata")
    digest = payload.get("Id")
    os_name, architecture = platform.split("/", 1)
    config = payload.get("Config")
    labels = config.get("Labels") if isinstance(config, dict) else None
    if (
        not isinstance(digest, str)
        or _DIGEST.fullmatch(digest) is None
        or payload.get("Os") != os_name
        or payload.get("Architecture") != architecture
        or not isinstance(labels, dict)
        or labels.get("org.opencontainers.image.version") != version
        or labels.get("org.opencontainers.image.revision") != commit
    ):
        raise ValueError("candidate image identity: image configuration mismatch")


def main() -> int:
    parser = argparse.ArgumentParser(description="本地候选镜像版本、提交和平台身份门禁")
    parser.add_argument("--image", required=True)
    parser.add_argument("--platform", required=True, choices=sorted(_PLATFORMS))
    parser.add_argument("--commit", required=True)
    args = parser.parse_args()
    try:
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        version = project["project"]["version"]
        if not isinstance(version, str):
            raise ValueError("invalid project version")
        result = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{json .}}", args.image],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            raise ValueError("cannot inspect local image")
        verify_candidate_image_identity(
            json.loads(result.stdout), version=version, commit=args.commit, platform=args.platform
        )
    except (OSError, ValueError, KeyError, json.JSONDecodeError, subprocess.TimeoutExpired):
        # Do not leak Docker's raw error (which may contain image names or registry auth).
        print("candidate image identity: validation failed", file=sys.stderr)
        return 1
    print(f"candidate image identity: verified {args.platform} v{version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
