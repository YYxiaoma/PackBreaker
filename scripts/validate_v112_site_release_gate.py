"""Offline, fail-closed v1.1.2 seven-site real E2E release evidence gate.

Default mode validates the inventory without claiming it has passed.
--require-ready is exclusively for the v1.1.2 *release* workflow.
Neither mode makes network calls, probes downloaders, or writes files.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = Path("docs/v1.1.2-seven-site-release-gate.json")
RECEIPTS_DIR = Path("docs/v1.1.2-site-receipts")
SITES = frozenset({"PTerClub", "Audiences", "SpringSunday", "HDDolby", "U2", "TANGPT", "CarPT"})
PHASES = frozenset(
    {
        "identity_auth",
        "search_detail",
        "authorized_torrent",
        "piece_hash",
        "frozen_plan",
        "file_materialization",
        "client_verify_start",
        "journal_recovery",
    }
)
PENDING = "PENDING_REAL_VALIDATION"
COMPLETE = "COMPLETE_REAL_E2E"
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_ALLOWED_DOC_PATH = str(MANIFEST)


def _source_ref_valid(value: object) -> bool:
    if not isinstance(value, str) or len(value) > 400:
        return False
    parts = urlsplit(value)
    return (
        parts.scheme == "https"
        and parts.netloc == "github.com"
        and parts.path.startswith("/YYxiaoma/PackBreaker/")
        and not parts.username
        and not parts.password
        and not parts.query
    )


def _read_json(path: Path) -> Any:
    if path.stat().st_size > 128 * 1024:
        raise ValueError("验收证据文件超过 128 KiB")
    return json.loads(path.read_text(encoding="utf-8"))


def validate_manifest(
    root: Path, manifest: Path, *, require_ready: bool
) -> tuple[list[str], str | None]:
    """Validate an offline manifest and every referenced real E2E receipt.

    Returns errors and the shared accepted *code* commit when all seven pass.
    Historical partial acceptance cannot be promoted by copying test output.
    """
    errors: list[str] = []
    try:
        payload = _read_json(manifest)
    except (OSError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
        return [f"七站验收清单无法读取：{type(exc).__name__}"], None

    if (
        not isinstance(payload, dict)
        or set(payload)
        != {"format_version", "release_tag", "policy", "publication_approval", "sites"}
        or payload["format_version"] != 1
        or payload["release_tag"] != "v1.1.2"
        or payload["policy"] != "ALL_SEVEN_SITES_REAL_E2E_REQUIRED"
        or not isinstance(payload["sites"], list)
    ):
        return ["七站验收清单版本、发布身份、策略或结构错误"], None

    approval = payload["publication_approval"]
    if (
        not isinstance(approval, dict)
        or set(approval) != {"status", "evidence_ref"}
        or approval["status"] not in {"NOT_GRANTED", "APPROVED"}
    ):
        errors.append("正式发布用户批准字段结构不正确")
    elif approval["status"] == "NOT_GRANTED":
        if approval["evidence_ref"] is not None:
            errors.append("用户尚未批准发布时不允许设置批准凭据")
        if require_ready:
            errors.append("用户尚未独立批准 v1.1.2 正式发布，保持阻断")
    elif not _source_ref_valid(approval["evidence_ref"]):
        errors.append("正式发布必须提供用户明确批准的脱敏 GitHub 证据链接")

    sites: list[dict[str, Any]] = payload["sites"]
    found: set[str] = set()
    accepted_commits: set[str] = set()
    for index, entry in enumerate(sites):
        if not isinstance(entry, dict) or set(entry) != {
            "site_key",
            "status",
            "receipt_path",
            "receipt_sha256",
        }:
            errors.append(f"sites[{index}]: 字段结构不符合白名单")
            continue
        key = entry["site_key"]
        if not isinstance(key, str) or key not in SITES or key in found:
            errors.append(f"sites[{index}]: 站点缺失、重复或未知")
            continue
        found.add(key)
        status = entry["status"]
        if status == PENDING:
            if entry["receipt_path"] is not None or entry["receipt_sha256"] is not None:
                errors.append(f"{key}: 待验收记录禁止挂载冒充完成的证明")
            if require_ready:
                errors.append(f"{key}: 真实完整 E2E 尚未验收，正式发布阻断")
            continue
        if status != COMPLETE:
            errors.append(f"{key}: 未知状态或未经批准的范围豁免 {status!r}")
            continue

        relative = entry["receipt_path"]
        expected_sha = entry["receipt_sha256"]
        if (
            not isinstance(relative, str)
            or not isinstance(expected_sha, str)
            or not _SHA.fullmatch(expected_sha)
        ):
            errors.append(f"{key}: 完整验收必须有独立证据文件及 SHA256")
            continue
        relative_path = Path(relative)
        if (
            relative_path.is_absolute()
            or ".." in relative_path.parts
            or relative_path.parts[:2] != RECEIPTS_DIR.parts
            or len(relative_path.parts) != 3
            or relative_path.suffix != ".json"
            or relative_path.name != f"{key}.json"
        ):
            errors.append(f"{key}: 证据路径必须为站点专属 docs/v1.1.2-site-receipts/{key}.json")
            continue
        path = root / relative_path
        try:
            if path.is_symlink() or not path.is_file():
                raise ValueError("证据不存在或是符号链接")
            receipt_root = root / RECEIPTS_DIR
            if receipt_root.is_symlink():
                raise ValueError("验收证据目录不允许符号链接")
            directory = receipt_root.resolve(strict=True)
            if not directory.is_relative_to(root.resolve(strict=True)) or not path.resolve(
                strict=True
            ).is_relative_to(directory):
                raise ValueError("验收证据路径越界")
            raw = path.read_bytes()
            if len(raw) > 128 * 1024:
                raise ValueError("验收证据过大")
            if hashlib.sha256(raw).hexdigest() != expected_sha:
                raise ValueError("验收证据摘要不匹配")
            receipt = json.loads(raw)
            if not isinstance(receipt, dict) or set(receipt) != {
                "site_key",
                "execution_id",
                "source_revision",
                "evidence_ref",
                "phases",
            }:
                raise ValueError("验收证据字段不完整或携带非白名单字段")
            if receipt["site_key"] != key:
                raise ValueError("验收站点身份不一致")
            run_id = receipt["execution_id"]
            if not isinstance(run_id, str) or str(UUID(run_id)) != run_id:
                raise ValueError("缺少合法的单一正式 execution_id")
            revision = receipt["source_revision"]
            if not isinstance(revision, str) or not _COMMIT.fullmatch(revision):
                raise ValueError("未绑定精确候选源码提交")
            if not _source_ref_valid(receipt["evidence_ref"]):
                raise ValueError("缺少可信的脱敏外部验收记录引用")
            phases = receipt["phases"]
            if not isinstance(phases, dict) or set(phases) != PHASES:
                raise ValueError("未完成要求的全部真实业务链阶段")
            for phase, proof in phases.items():
                if (
                    not isinstance(proof, dict)
                    or set(proof) != {"status", "execution_id", "evidence_ref"}
                    or proof["status"] != "PASSED"
                    or proof["execution_id"] != run_id
                    or not _source_ref_valid(proof["evidence_ref"])
                ):
                    raise ValueError(f"{phase}: 阶段未通过或并非同一个真实 execution")
            accepted_commits.add(revision)
        except (OSError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
            errors.append(f"{key}: 验收证据不合格：{exc}")

    if found != SITES or len(sites) != len(SITES):
        errors.append("七站必须完整且各出现一次；不允许少站、重站或缩小范围")

    if len(accepted_commits) > 1:
        errors.append("七站必须对应同一份源码候选；禁止拼接不同源码版本的验收")
    candidate_commit = next(iter(accepted_commits)) if len(accepted_commits) == 1 else None
    if require_ready and (
        len(accepted_commits) != 1
        or any(e["status"] != COMPLETE for e in sites if isinstance(e, dict) and "status" in e)
    ):
        errors.append("未集齐同一源码候选的七站完整验收")
    return errors, candidate_commit


def validate_source_unchanged(root: Path, accepted_commit: str) -> list[str]:
    """Fail closed if application, dependencies, scripts or workflow changed after E2E.

    Only the gate manifest and the seven receipt files may be committed later;
    the source commit must be an ancestor of the release tag. GitHub Release's
    quality/image checks independently validate the release tag's exact SHA.
    """

    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )

    ancestor = git("merge-base", "--is-ancestor", accepted_commit, "HEAD")
    if ancestor.returncode != 0:
        return ["验收源码提交不是当前候选/发布源码的祖先，必须重新验收"]
    changed = git("diff", "--name-only", accepted_commit, "HEAD")
    if changed.returncode != 0:
        return ["不能校验真实验收后源码是否发生变化"]
    allowed = {_ALLOWED_DOC_PATH} | {str(RECEIPTS_DIR / f"{site}.json") for site in SITES}
    if any(path not in allowed for path in changed.stdout.splitlines()):
        return ["真实验收之后候选源码或文档发生非证据变更，需重新完成七站验收"]
    status = git("status", "--porcelain")
    if status.returncode != 0 or status.stdout.strip():
        return ["正式发布必须基于无未提交改动的精确候选提交"]
    return []


def main() -> int:
    parser = argparse.ArgumentParser(description="v1.1.2 七站真实全链离线发布阻断门禁")
    parser.add_argument("--manifest", type=Path, default=ROOT / MANIFEST)
    parser.add_argument("--require-ready", action="store_true")
    args = parser.parse_args()
    errors, source_commit = validate_manifest(ROOT, args.manifest, require_ready=args.require_ready)
    if args.require_ready and not errors:
        if source_commit is None:
            errors.append("完整验收缺少单一来源源码身份")
        else:
            errors.extend(validate_source_unchanged(ROOT, source_commit))
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    if args.require_ready:
        print("v1.1.2 七站真实全链证据/源码一致性门禁通过（仍需 CI 与用户正式发布批准）")
    else:
        print("v1.1.2 七站验收清单结构通过；待验收状态不代表正式发布就绪")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
