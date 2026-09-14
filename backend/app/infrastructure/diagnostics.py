from __future__ import annotations

import hashlib
import io
import json
import zipfile
from dataclasses import dataclass
from datetime import UTC

from backend.app.application.system_health import SystemHealthReport

DIAGNOSTIC_FORMAT_VERSION = 1


@dataclass(frozen=True, slots=True)
class DiagnosticBundle:
    filename: str
    content: bytes
    sha256: str


def build_diagnostic_bundle(report: SystemHealthReport) -> DiagnosticBundle:
    """生成只含白名单聚合指标的诊断包；不读取日志、配置、secret 或媒体内容。"""

    health_bytes = _json_bytes(report.as_dict())
    health_digest = hashlib.sha256(health_bytes).hexdigest()
    manifest = {
        "format_version": DIAGNOSTIC_FORMAT_VERSION,
        "generated_at": report.generated_at.isoformat().replace("+00:00", "Z"),
        "app_version": report.version,
        "privacy": {
            "contains_logs": False,
            "contains_credentials": False,
            "contains_urls": False,
            "contains_paths": False,
            "contains_task_ids": False,
            "contains_torrent_or_source_hashes": False,
            "contains_media_content": False,
        },
        "files": [{"name": "health.json", "sha256": health_digest}],
    }
    manifest_bytes = _json_bytes(manifest)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        _write_zip_member(archive, "health.json", health_bytes)
        _write_zip_member(archive, "manifest.json", manifest_bytes)
    content = buffer.getvalue()
    timestamp = report.generated_at.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return DiagnosticBundle(
        filename=f"packbreaker-diagnostics-{timestamp}.zip",
        content=content,
        sha256=hashlib.sha256(content).hexdigest(),
    )


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _write_zip_member(archive: zipfile.ZipFile, name: str, content: bytes) -> None:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o600 << 16
    archive.writestr(info, content)
