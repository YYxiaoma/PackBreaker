"""模板文件（`.pbtemplate`）的导出、解析与导入。

设计约束：
- 文件是可移植的离线数据，不包含站点配置 id、Cookie、下载器 id 或本机绝对路径。
- `collection_root_name` 只取「大包」这一级目录名，绝不含父级或绝对路径。
- 导入与内置安装共用同一套解析 + 落库逻辑，避免两份校验。
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import unicodedata
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.errors import ApplicationError
from backend.app.application.unpack_templates import (
    _clean_mapping_media_name,
    _descriptor_payload,
    _digest,
    _normalize_seed_mappings,
)
from backend.app.domain.site_config import SiteKind
from backend.app.domain.unpack import UnpackExecutionStatus, UnpackItemStatus
from backend.app.domain.unpack_execution_plan import unpack_execution_plan_from_payload
from backend.app.infrastructure.persistence.database import begin_immediate_write
from backend.app.infrastructure.persistence.models import (
    Site,
    UnpackDefinition,
    UnpackExecution,
    UnpackExecutionItem,
    UnpackMatchCandidate,
    UnpackTemplate,
    UnpackTemplateSeed,
    UnpackTemplateVersion,
    UnpackTorrentMetadata,
    new_uuid,
    utc_now,
)
from backend.app.versioning import app_version

TEMPLATE_FILE_SCHEMA_VERSION = 1
TEMPLATE_FILE_KIND = "packbreaker.unpack.template"
TEMPLATE_FILE_SUFFIX = ".pbtemplate"

MANIFEST_MEMBER = "manifest.json"
PAYLOAD_MEMBER = "payload.json"

MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
MAX_PAYLOAD_BYTES = 64 * 1024 * 1024
MAX_SEEDS = 5000
MAX_MAPPINGS_PER_SEED = 20
MAX_FILE_SUMMARY_ENTRIES = 5000
MAX_EXTERNAL_IDS = 32

ORIGIN_USER = "USER"
ORIGIN_EXPORTED = "EXPORTED"
ORIGIN_BUILTIN = "BUILTIN"
ORIGIN_COMMUNITY = "COMMUNITY"  # 预留：云端/社区模板

_TERMINAL_EXECUTION_STATUSES = frozenset(
    {
        UnpackExecutionStatus.COMPLETED.value,
        UnpackExecutionStatus.COMPLETED_WITH_ERRORS.value,
        UnpackExecutionStatus.FAILED.value,
        UnpackExecutionStatus.CANCELLED.value,
    }
)

_ACTIVE_ITEM_STATUSES = frozenset(
    {
        UnpackItemStatus.MATCH_PENDING.value,
        UnpackItemStatus.MATCHING.value,
        UnpackItemStatus.TORRENT_FETCHING.value,
        UnpackItemStatus.AUXILIARY_FETCHING.value,
        UnpackItemStatus.CONTENT_VERIFYING.value,
        UnpackItemStatus.PLAN_PENDING.value,
        UnpackItemStatus.EXECUTING.value,
        UnpackItemStatus.CLIENT_VERIFYING.value,
    }
)


@dataclass(frozen=True, slots=True)
class TemplateSeedEntry:
    remote_torrent_id: str
    display_name: str
    descriptor: dict[str, Any]
    total_size: int | None
    file_summary: tuple[dict[str, Any], ...]
    external_ids: tuple[dict[str, str], ...]
    mappings: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class ParsedTemplateFile:
    name: str
    description: str | None
    site_key: str
    seeds: tuple[TemplateSeedEntry, ...]
    payload_digest: str
    exported_at: str | None
    exporter_version: str | None
    source_definition_name: str | None

    @property
    def mapped_media_count(self) -> int:
        return sum(len(seed.mappings) for seed in self.seeds)


@dataclass(frozen=True, slots=True)
class TemplateFilePreview:
    name: str
    description: str | None
    site_key: str
    seed_count: int
    mapped_media_count: int
    resolved_name: str
    renamed: bool
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TemplateImportResult:
    template_id: str
    template_version: int
    name: str
    site_key: str
    seed_count: int
    status: str
    renamed: bool


@dataclass(frozen=True, slots=True)
class TemplateExportSummary:
    filename: str
    payload: bytes
    site_key: str
    collection_root_name: str
    seed_count: int
    skipped_count: int
    skipped_reasons: tuple[str, ...]


# --------------------------------------------------------------------- 解析


class UnpackTemplateTransferService:
    """模板文件导入/导出的应用入口，供 API 与启动安装复用。"""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def preview_file(self, raw: bytes) -> TemplateFilePreview:
        return preview_template_file(self._session_factory, parse_template_archive(raw))

    def import_file(self, raw: bytes) -> TemplateImportResult:
        return import_template_file(
            self._session_factory,
            parse_template_archive(raw),
            origin=ORIGIN_EXPORTED,
        )

    def export_execution(
        self,
        execution_id: str,
        *,
        target_site_key: str | None = None,
        collection_root: str | None = None,
    ) -> TemplateExportSummary:
        return export_execution_template(
            self._session_factory,
            execution_id,
            target_site_key=target_site_key,
            collection_root_override=collection_root,
        )


def parse_template_archive(raw: bytes) -> ParsedTemplateFile:
    if not raw:
        raise _invalid("UNPACK_TEMPLATE_FILE_EMPTY", "模板文件为空")
    if len(raw) > MAX_ARCHIVE_BYTES:
        raise _invalid("UNPACK_TEMPLATE_FILE_TOO_LARGE", "模板文件超过 32 MiB 上限")
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise _invalid("UNPACK_TEMPLATE_FILE_INVALID", "模板文件不是有效的 ZIP 包") from exc
    with archive:
        names = set(archive.namelist())
        if names != {MANIFEST_MEMBER, PAYLOAD_MEMBER}:
            raise _invalid(
                "UNPACK_TEMPLATE_FILE_MEMBERS",
                "模板包只允许包含 manifest.json 与 payload.json",
            )
        manifest = _load_json(archive.read(MANIFEST_MEMBER), "manifest.json")
        payload_raw = archive.read(PAYLOAD_MEMBER)
    if len(payload_raw) > MAX_PAYLOAD_BYTES:
        raise _invalid("UNPACK_TEMPLATE_FILE_TOO_LARGE", "payload.json 超过 64 MiB 上限")
    payload = _load_json(payload_raw, "payload.json")

    if manifest.get("kind") != TEMPLATE_FILE_KIND:
        raise _invalid("UNPACK_TEMPLATE_FILE_INVALID", "模板文件类型标识无效")
    schema_version = _required_int(manifest, "schema_version", "manifest.schema_version")
    if schema_version != TEMPLATE_FILE_SCHEMA_VERSION:
        raise _invalid(
            "UNPACK_TEMPLATE_FILE_SCHEMA_UNSUPPORTED",
            f"不支持的模板文件版本：{schema_version}，当前仅支持 {TEMPLATE_FILE_SCHEMA_VERSION}",
        )
    if payload.get("schema_version") != TEMPLATE_FILE_SCHEMA_VERSION:
        raise _invalid("UNPACK_TEMPLATE_FILE_SCHEMA_UNSUPPORTED", "payload 版本不受支持")

    expected_digest = manifest.get("payload_sha256")
    actual_digest = hashlib.sha256(payload_raw).hexdigest()
    if not isinstance(expected_digest, str) or expected_digest != actual_digest:
        raise _invalid("UNPACK_TEMPLATE_FILE_DIGEST", "模板文件摘要校验失败，文件可能已损坏")

    name = _required_text(manifest, "name", 120, "模板名称")
    description_raw = manifest.get("description")
    description: str | None = None
    if description_raw is not None:
        if not isinstance(description_raw, str):
            raise _invalid("UNPACK_TEMPLATE_FILE_INVALID", "模板描述格式无效")
        description = description_raw.strip()[:2000] or None

    site_key = _required_site_key(manifest, payload)
    raw_seeds = payload.get("seeds")
    if not isinstance(raw_seeds, list) or not raw_seeds:
        raise _invalid("UNPACK_TEMPLATE_FILE_INVALID", "模板文件不包含任何种子")
    if len(raw_seeds) > MAX_SEEDS:
        raise _invalid("UNPACK_TEMPLATE_FILE_TOO_MANY_SEEDS", f"种子数量超过 {MAX_SEEDS} 条上限")

    seeds = _normalize_seeds(raw_seeds, site_key=site_key)
    return ParsedTemplateFile(
        name=name,
        description=description,
        site_key=site_key,
        seeds=seeds,
        payload_digest=actual_digest,
        exported_at=_optional_text(manifest.get("exported_at"), 64),
        exporter_version=_optional_text(manifest.get("exporter_version"), 32),
        source_definition_name=_optional_text(
            (manifest.get("source") or {}).get("definition_name")
            if isinstance(manifest.get("source"), dict)
            else None,
            120,
        ),
    )


def _normalize_seeds(raw_seeds: list[Any], *, site_key: str) -> tuple[TemplateSeedEntry, ...]:
    pending: list[dict[str, Any]] = []
    display_names: dict[str, str] = {}
    descriptors: dict[str, dict[str, Any]] = {}
    sizes: dict[str, int | None] = {}
    files: dict[str, tuple[dict[str, Any], ...]] = {}
    externals: dict[str, tuple[dict[str, str], ...]] = {}
    for raw in raw_seeds:
        if not isinstance(raw, dict):
            raise _invalid("UNPACK_TEMPLATE_FILE_INVALID", "种子条目格式无效")
        torrent_id = _required_text(raw, "remote_torrent_id", 128, "种子 ID")
        if torrent_id in display_names:
            raise _invalid("UNPACK_TEMPLATE_FILE_INVALID", f"种子 ID 重复：{torrent_id}")
        display_name = _required_text(raw, "display_name", 2000, "种子名称")
        display_names[torrent_id] = display_name
        descriptors[torrent_id] = _normalize_descriptor(raw.get("descriptor"), display_name)
        sizes[torrent_id] = _normalize_total_size(raw.get("total_size"))
        files[torrent_id] = _normalize_file_summary(raw.get("file_summary"))
        externals[torrent_id] = _normalize_external_ids(raw.get("external_ids"))
        raw_mappings = raw.get("mappings")
        if raw_mappings is None:
            continue
        if not isinstance(raw_mappings, list):
            raise _invalid("UNPACK_TEMPLATE_FILE_INVALID", "目录映射格式无效")
        if len(raw_mappings) > MAX_MAPPINGS_PER_SEED:
            raise _invalid("UNPACK_TEMPLATE_FILE_INVALID", "单个种子的目录映射数量过多")
        for mapping in raw_mappings:
            if not isinstance(mapping, dict):
                raise _invalid("UNPACK_TEMPLATE_FILE_INVALID", "目录映射条目格式无效")
            pending.append({**mapping, "torrent_id": torrent_id})

    normalized = _normalize_seed_mappings(
        tuple(pending), allowed_torrent_ids=frozenset(display_names)
    )
    by_torrent: dict[str, list[dict[str, Any]]] = {torrent_id: [] for torrent_id in display_names}
    for mapping in normalized:
        by_torrent[mapping["torrent_id"]].append(
            {
                "collection_root_name": mapping["collection_root_name"],
                "relative_directory": mapping["relative_directory"],
                "relative_file_name": mapping.get("relative_file_name"),
                "media_title": mapping["media_title"],
                "confidence_bps": mapping.get("confidence_bps", 0),
            }
        )
    return tuple(
        TemplateSeedEntry(
            remote_torrent_id=torrent_id,
            display_name=display_names[torrent_id],
            descriptor=descriptors[torrent_id],
            total_size=sizes[torrent_id],
            file_summary=files[torrent_id],
            external_ids=externals[torrent_id],
            mappings=tuple(by_torrent[torrent_id]),
        )
        for torrent_id in display_names
    )


def _normalize_descriptor(value: object, display_name: str) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    from backend.app.domain.media_matching import parse_media_name

    return _descriptor_payload(parse_media_name(display_name))


def _normalize_total_size(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _invalid("UNPACK_TEMPLATE_FILE_INVALID", "种子大小无效")
    return value


def _normalize_file_summary(value: object) -> tuple[dict[str, Any], ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > MAX_FILE_SUMMARY_ENTRIES:
        raise _invalid("UNPACK_TEMPLATE_FILE_INVALID", "文件清单格式无效")
    files: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise _invalid("UNPACK_TEMPLATE_FILE_INVALID", "文件清单条目格式无效")
        basename = _required_text(item, "basename", 255, "文件名")
        length = item.get("length")
        if isinstance(length, bool) or not isinstance(length, int) or length < 0:
            raise _invalid("UNPACK_TEMPLATE_FILE_INVALID", "文件长度无效")
        files.append({"basename": basename, "length": length})
    return tuple(files)


def _normalize_external_ids(value: object) -> tuple[dict[str, str], ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > MAX_EXTERNAL_IDS:
        raise _invalid("UNPACK_TEMPLATE_FILE_INVALID", "外部 ID 格式无效")
    result: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            raise _invalid("UNPACK_TEMPLATE_FILE_INVALID", "外部 ID 条目格式无效")
        namespace = _required_text(item, "namespace", 32, "外部 ID 命名空间")
        value = _required_text(item, "value", 64, "外部 ID")
        result.append({"namespace": namespace, "value": value})
    return tuple(result)


# --------------------------------------------------------------------- 导入


def preview_template_file(
    session_factory: sessionmaker[Session],
    parsed: ParsedTemplateFile,
) -> TemplateFilePreview:
    with session_factory() as session:
        resolved_name, renamed = _resolve_template_name(session, parsed.name)
    warnings: list[str] = []
    if not parsed.seeds:
        warnings.append("模板文件不包含可导入的种子")
    if parsed.mapped_media_count == 0:
        warnings.append("模板不含目录映射，只能依赖标题匹配")
    missing = [seed.remote_torrent_id for seed in parsed.seeds if not seed.file_summary]
    if missing:
        warnings.append(f"{len(missing)} 条种子缺少文件清单，文件级匹配将降级为标题匹配")
    return TemplateFilePreview(
        name=parsed.name,
        description=parsed.description,
        site_key=parsed.site_key,
        seed_count=len(parsed.seeds),
        mapped_media_count=parsed.mapped_media_count,
        resolved_name=resolved_name,
        renamed=renamed,
        warnings=tuple(warnings),
    )


def import_template_file(
    session_factory: sessionmaker[Session],
    parsed: ParsedTemplateFile,
    *,
    origin: str = ORIGIN_EXPORTED,
    builtin_key: str | None = None,
    builtin_version: int | None = None,
) -> TemplateImportResult:
    now = utc_now()
    with session_factory() as session:
        begin_immediate_write(session)
        if builtin_key is not None:
            template = session.scalar(
                select(UnpackTemplate).where(UnpackTemplate.builtin_key == builtin_key)
            )
        else:
            template = None
        if template is None:
            resolved_name, renamed = _resolve_template_name(session, parsed.name)
            template = UnpackTemplate(
                id=new_uuid(),
                name=resolved_name,
                description=parsed.description,
                origin=origin,
                builtin_key=builtin_key,
                builtin_version=builtin_version,
                current_version=1,
                source_provenance=_template_provenance(parsed, origin),
                created_at=now,
                updated_at=now,
            )
            session.add(template)
            session.flush()
            version_number = 1
            renamed_flag = renamed
        else:
            resolved_name = template.name
            version_number = template.current_version + 1
            template.current_version = version_number
            template.updated_at = now
            if builtin_version is not None:
                template.builtin_version = builtin_version
            renamed_flag = False

        version = UnpackTemplateVersion(
            id=new_uuid(),
            template_id=template.id,
            version=version_number,
            schema_version=TEMPLATE_FILE_SCHEMA_VERSION,
            status="READY",
            expected_seed_count=len(parsed.seeds),
            seed_count=0,
            site_keys=[parsed.site_key],
            content_digest=parsed.payload_digest,
            source_provenance=_template_provenance(parsed, origin),
            created_at=now,
        )
        session.add(version)
        session.flush()

        seed_count = 0
        for index, entry in enumerate(parsed.seeds):
            metadata = _upsert_metadata(session, entry, parsed.site_key, now)
            session.add(
                UnpackTemplateSeed(
                    id=new_uuid(),
                    template_version_id=version.id,
                    metadata_id=metadata.id,
                    mapping_rules={
                        "source_mappings": [
                            {**mapping, "torrent_id": entry.remote_torrent_id}
                            for mapping in entry.mappings
                        ]
                    },
                    sort_order=index,
                    created_at=now,
                )
            )
            seed_count += 1
        version.seed_count = seed_count
        version.status = "READY" if seed_count == len(parsed.seeds) else "INCOMPLETE"
        session.flush()
        session.commit()
        return TemplateImportResult(
            template_id=template.id,
            template_version=version_number,
            name=resolved_name,
            site_key=parsed.site_key,
            seed_count=seed_count,
            status=version.status,
            renamed=renamed_flag,
        )


def _upsert_metadata(
    session: Session,
    entry: TemplateSeedEntry,
    site_key: str,
    now: Any,
) -> UnpackTorrentMetadata:
    metadata = session.scalar(
        select(UnpackTorrentMetadata)
        .where(UnpackTorrentMetadata.site_key == site_key)
        .where(UnpackTorrentMetadata.remote_torrent_id == entry.remote_torrent_id)
    )
    payload = {
        "site_key": site_key,
        "remote_torrent_id": entry.remote_torrent_id,
        "display_name": entry.display_name,
        "descriptor": entry.descriptor,
        "total_size": entry.total_size,
        "file_summary": list(entry.file_summary),
        "external_ids": list(entry.external_ids),
    }
    digest = _digest(payload)
    if metadata is None:
        metadata = UnpackTorrentMetadata(
            id=new_uuid(),
            site_key=site_key,
            remote_torrent_id=entry.remote_torrent_id,
            display_name=entry.display_name,
            descriptor=entry.descriptor,
            total_size=entry.total_size,
            file_summary=list(entry.file_summary),
            external_ids=list(entry.external_ids),
            info_hash=None,
            metadata_digest=digest,
            fetched_at=now,
            verification_status="DETAILS_VERIFIED",
            source_provenance={"source": "template_file", "site_key": site_key},
            created_at=now,
            updated_at=now,
        )
        session.add(metadata)
        session.flush()
        return metadata
    metadata.display_name = entry.display_name
    metadata.descriptor = entry.descriptor
    if entry.total_size is not None:
        metadata.total_size = entry.total_size
    if entry.file_summary:
        metadata.file_summary = list(entry.file_summary)
    if entry.external_ids:
        metadata.external_ids = list(entry.external_ids)
    metadata.metadata_digest = digest
    metadata.fetched_at = now
    metadata.updated_at = now
    return metadata


def _resolve_template_name(session: Session, name: str) -> tuple[str, bool]:
    existing = set(session.scalars(select(UnpackTemplate.name)).all())
    if name not in existing:
        return name, False
    for index in range(2, 1000):
        candidate = _clamp_name(f"{name}（导入 {index}）")
        if candidate not in existing:
            return candidate, True
    raise _invalid("UNPACK_TEMPLATE_NAME_CONFLICT", "同名模板过多，请先清理后再导入")


def _template_provenance(parsed: ParsedTemplateFile, origin: str) -> dict[str, Any]:
    provenance: dict[str, Any] = {
        "source": "template_file",
        "origin": origin,
        "payload_sha256": parsed.payload_digest,
        "site_key": parsed.site_key,
    }
    if parsed.exported_at:
        provenance["exported_at"] = parsed.exported_at
    if parsed.exporter_version:
        provenance["exporter_version"] = parsed.exporter_version
    if parsed.source_definition_name:
        provenance["source_definition_name"] = parsed.source_definition_name
    return provenance


# --------------------------------------------------------------------- 导出


def export_execution_template(
    session_factory: sessionmaker[Session],
    execution_id: str,
    *,
    target_site_key: str | None = None,
    collection_root_override: str | None = None,
) -> TemplateExportSummary:
    with session_factory() as session:
        execution = session.get(UnpackExecution, execution_id)
        if execution is None:
            raise _invalid("UNPACK_EXECUTION_NOT_FOUND", "数据拆包执行不存在")
        if execution.status not in _TERMINAL_EXECUTION_STATUSES:
            raise _invalid(
                "UNPACK_TEMPLATE_EXPORT_NOT_FINISHED",
                "只有已结束的执行才能导出模板（已完成 / 完成但有错误 / 失败 / 已取消）",
            )
        definition = session.get(UnpackDefinition, execution.definition_id)
        if definition is None:
            raise _invalid("UNPACK_EXECUTION_NOT_FOUND", "执行对应的拆包任务不存在")
        items = session.scalars(
            select(UnpackExecutionItem).where(UnpackExecutionItem.execution_id == execution.id)
        ).all()
        active = [item.id for item in items if item.status in _ACTIVE_ITEM_STATUSES]
        if active:
            raise _invalid(
                "UNPACK_TEMPLATE_EXPORT_NOT_FINISHED",
                f"仍有 {len(active)} 个影片处于进行中状态，无法导出模板",
            )

        source_dir = _definition_source_directory(definition)
        collection_root = _normalize_collection_root(
            collection_root_override or (PurePosixPath(source_dir).name if source_dir else "")
        )

        site_types = {row.id: row.type for row in session.scalars(select(Site)).all()}
        entries: list[TemplateSeedEntry] = []
        skipped: list[str] = []
        chosen_site_key = target_site_key.strip().upper() if target_site_key else None
        for item in items:
            if item.status != UnpackItemStatus.COMPLETED.value:
                continue
            candidate = (
                session.get(UnpackMatchCandidate, item.selected_candidate_id)
                if item.selected_candidate_id
                else None
            )
            if candidate is None or candidate.generation != item.candidate_generation:
                skipped.append(f"{item.id[:8]}：缺少有效候选")
                continue
            site_key = site_types.get(candidate.site_id)
            if not site_key:
                skipped.append(f"{item.id[:8]}：候选站点已不存在")
                continue
            if chosen_site_key is not None and site_key != chosen_site_key:
                continue
            plan = None
            if item.execution_plan is not None:
                try:
                    plan = unpack_execution_plan_from_payload(item.execution_plan)
                except ValueError:
                    plan = None
            if plan is None:
                skipped.append(f"{item.id[:8]}：缺少冻结执行计划")
                continue
            mapping = _mapping_for_item(item, source_dir, collection_root)
            if mapping is None:
                skipped.append(f"{item.id[:8]}：来源路径不在任务来源目录下，无法推导目录映射")
                continue
            entries.append(
                TemplateSeedEntry(
                    remote_torrent_id=candidate.candidate_key,
                    display_name=candidate.title,
                    descriptor=_descriptor_payload(_parse_title(candidate.title)),
                    total_size=candidate.size_bytes,
                    file_summary=_file_summary_from_plan(plan),
                    external_ids=_external_ids_from_candidate(candidate),
                    mappings=(mapping,),
                )
            )

        if not entries:
            raise _invalid(
                "UNPACK_TEMPLATE_EXPORT_EMPTY",
                "该执行没有可导出的已完成影片（只导出「已完成」状态的影片）",
            )
        resolved_site_key = chosen_site_key or _single_site_key_for_entries(
            items, site_types, execution.id, session
        )
        payload = {
            "schema_version": TEMPLATE_FILE_SCHEMA_VERSION,
            "site_key": resolved_site_key,
            "seeds": [
                {
                    "remote_torrent_id": entry.remote_torrent_id,
                    "display_name": entry.display_name,
                    "descriptor": entry.descriptor,
                    "total_size": entry.total_size,
                    "file_summary": list(entry.file_summary),
                    "external_ids": list(entry.external_ids),
                    "mappings": list(entry.mappings),
                }
                for entry in entries
            ],
            "file_summary_source": "execution_plan",
        }
        payload_bytes = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        manifest = {
            "schema_version": TEMPLATE_FILE_SCHEMA_VERSION,
            "kind": TEMPLATE_FILE_KIND,
            "name": _template_name_from_definition(definition, collection_root),
            "description": f"由任务「{definition.name}」导出的拆包模板",
            "site_key": resolved_site_key,
            "seed_count": len(entries),
            "mapped_media_count": len(entries),
            "payload_sha256": hashlib.sha256(payload_bytes).hexdigest(),
            "exported_at": utc_now().isoformat(),
            "exporter_version": app_version(),
            "source": {"definition_name": definition.name},
        }
        archive = _build_archive(manifest, payload_bytes)
        template_name = manifest["name"]
        filename = (
            f"{_safe_filename(str(template_name))}-{resolved_site_key}-v1{TEMPLATE_FILE_SUFFIX}"
        )
        return TemplateExportSummary(
            filename=filename,
            payload=archive,
            site_key=resolved_site_key,
            collection_root_name=collection_root,
            seed_count=len(entries),
            skipped_count=len(skipped),
            skipped_reasons=tuple(skipped[:20]),
        )


def _single_site_key_for_entries(
    items: Sequence[UnpackExecutionItem],
    site_types: dict[str, str],
    execution_id: str,
    session: Session,
) -> str:
    keys: set[str] = set()
    for item in items:
        if item.status != UnpackItemStatus.COMPLETED.value or not item.selected_candidate_id:
            continue
        candidate = session.get(UnpackMatchCandidate, item.selected_candidate_id)
        if candidate is None:
            continue
        site_key = site_types.get(candidate.site_id)
        if site_key:
            keys.add(site_key)
    if len(keys) == 1:
        return keys.pop()
    if not keys:
        raise _invalid("UNPACK_TEMPLATE_EXPORT_EMPTY", "该执行没有可导出的已完成影片")
    raise _invalid(
        "UNPACK_TEMPLATE_EXPORT_SITE_AMBIGUOUS",
        "该执行的完成影片来自多个站点：" + ", ".join(sorted(keys)) + "。请选择要导出哪一个站点。",
    )


def _definition_source_directory(definition: UnpackDefinition) -> str | None:
    config = definition.source_config if isinstance(definition.source_config, dict) else {}
    value = config.get("directory_path")
    return value if isinstance(value, str) and value.strip() else None


def _template_name_from_definition(definition: UnpackDefinition, collection_root: str) -> str:
    base = f"{collection_root}" if collection_root else definition.name
    return _clamp_name(base)


def _normalize_collection_root(value: str) -> str:
    normalized = unicodedata.normalize("NFC", (value or "").strip())
    if (
        not normalized
        or len(normalized) > 255
        or normalized in {".", ".."}
        or "/" in normalized
        or "\\" in normalized
        or "\x00" in normalized
    ):
        raise _invalid(
            "UNPACK_TEMPLATE_COLLECTION_ROOT_INVALID",
            "大包目录名必须是单级安全目录名（不含路径分隔符）",
        )
    return normalized


def _mapping_for_item(
    item: UnpackExecutionItem,
    source_dir: str | None,
    collection_root: str,
) -> dict[str, Any] | None:
    snapshot = item.source_snapshot if isinstance(item.source_snapshot, dict) else {}
    raw_path = snapshot.get("path")
    if not isinstance(raw_path, str) or not raw_path or not source_dir:
        return None
    source_path = PurePosixPath(raw_path.replace("\\", "/"))
    root = PurePosixPath(source_dir.replace("\\", "/"))
    try:
        relative = source_path.relative_to(root)
    except ValueError:
        return None
    if not relative.parts or any(part in {".", ".."} for part in relative.parts):
        return None
    if len(relative.parts) < 2:
        # 来源文件直接位于大包根目录：相对目录用大包名本身
        relative_directory = collection_root
    else:
        relative_directory = PurePosixPath(*relative.parts[:-1]).as_posix()
    movie_directory = (
        PurePosixPath(*relative.parts[:-1]).name if len(relative.parts) > 1 else collection_root
    )
    media_title = _clean_mapping_media_name(movie_directory)
    return {
        "collection_root_name": collection_root,
        "relative_directory": relative_directory,
        "relative_file_name": relative.name,
        "media_title": media_title,
        "confidence_bps": 10000,
    }


def _file_summary_from_plan(plan: Any) -> tuple[dict[str, Any], ...]:
    seen: set[str] = set()
    files: list[dict[str, Any]] = []
    for action in plan.actions:
        if action.length <= 0:
            continue
        basename = PurePosixPath(action.torrent_path).name
        if not basename or basename in seen:
            continue
        seen.add(basename)
        files.append({"basename": basename, "length": action.length})
    return tuple(files)


def _external_ids_from_candidate(candidate: UnpackMatchCandidate) -> tuple[dict[str, str], ...]:
    ids: list[dict[str, str]] = []
    if candidate.imdb_id:
        ids.append({"namespace": "imdb", "value": candidate.imdb_id})
    if candidate.douban_id:
        ids.append({"namespace": "douban", "value": candidate.douban_id})
    return tuple(ids)


def _parse_title(title: str) -> Any:
    from backend.app.domain.media_matching import parse_media_name

    return parse_media_name(title)


def _build_archive(manifest: dict[str, Any], payload_bytes: bytes) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            MANIFEST_MEMBER,
            json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
        )
        archive.writestr(PAYLOAD_MEMBER, payload_bytes)
    return buffer.getvalue()


def _safe_filename(value: str) -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", value).strip(" .")
    return (cleaned or "template")[:120]


# --------------------------------------------------------------------- 通用


def _load_json(raw: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _invalid("UNPACK_TEMPLATE_FILE_INVALID", f"{label} 不是有效的 JSON") from exc
    if not isinstance(value, dict):
        raise _invalid("UNPACK_TEMPLATE_FILE_INVALID", f"{label} 必须是 JSON 对象")
    return value


def _required_text(value: dict[str, Any], key: str, limit: int, label: str) -> str:
    raw = value.get(key)
    if not isinstance(raw, str) or not raw.strip():
        raise _invalid("UNPACK_TEMPLATE_FILE_INVALID", f"{label}缺失")
    normalized = unicodedata.normalize("NFC", raw.strip())
    if len(normalized) > limit or "\x00" in normalized:
        raise _invalid("UNPACK_TEMPLATE_FILE_INVALID", f"{label}长度无效")
    return normalized


def _required_int(value: dict[str, Any], key: str, label: str) -> int:
    raw = value.get(key)
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise _invalid("UNPACK_TEMPLATE_FILE_INVALID", f"{label} 必须是整数")
    return raw


def _optional_text(value: object, limit: int) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()[:limit]


def _required_site_key(manifest: dict[str, Any], payload: dict[str, Any]) -> str:
    site_key = _required_text(manifest, "site_key", 32, "站点类型").upper()
    payload_key = payload.get("site_key")
    if isinstance(payload_key, str) and payload_key.strip().upper() != site_key:
        raise _invalid("UNPACK_TEMPLATE_FILE_INVALID", "manifest 与 payload 的站点类型不一致")
    try:
        SiteKind(site_key)
    except ValueError as exc:
        raise _invalid(
            "UNPACK_TEMPLATE_SITE_UNSUPPORTED",
            f"模板绑定的站点类型不受支持：{site_key}，请升级到更新版本",
        ) from exc
    return site_key


def _clamp_name(value: str) -> str:
    return unicodedata.normalize("NFC", value.strip())[:120]


def _invalid(code: str, detail: str) -> ApplicationError:
    return ApplicationError(
        code=code,
        status=422,
        title="模板文件无法处理",
        detail=detail,
    )


# --------------------------------------------------------------------- 内置安装


def install_builtin_templates(
    session_factory: sessionmaker[Session],
    root: Path,
    *,
    app_version_value: str | None = None,
) -> int:
    """把镜像内置模板安装/升级到库里；任何异常都 fail-soft。"""

    import logging

    logger = logging.getLogger("packbreaker.recovery")
    index_path = root / "index.json"
    if not index_path.is_file():
        return 0
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("内置模板索引无法读取，已跳过：%s", index_path)
        return 0
    entries = index.get("templates")
    if not isinstance(entries, list):
        logger.warning("内置模板索引格式无效，已跳过：%s", index_path)
        return 0

    installed = 0
    for entry in entries:
        try:
            if not isinstance(entry, dict):
                continue
            builtin_key = _required_text(entry, "builtin_key", 120, "内置模板标识")
            version = _required_int(entry, "version", "内置模板版本")
            if version < 1:
                continue
            min_app_version = entry.get("min_app_version")
            if (
                isinstance(min_app_version, str)
                and app_version_value is not None
                and _version_tuple(app_version_value) < _version_tuple(min_app_version)
            ):
                logger.info("内置模板 %s 需要更高版本，已跳过", builtin_key)
                continue
            file_name = _required_text(entry, "file", 255, "内置模板文件")
            file_path = (root / file_name).resolve()
            if root.resolve() not in file_path.parents:
                logger.warning("内置模板路径越界，已跳过：%s", file_name)
                continue
            raw = file_path.read_bytes()
            expected = entry.get("sha256")
            digest = hashlib.sha256(raw).hexdigest()
            if isinstance(expected, str) and expected and digest != expected:
                logger.warning("内置模板摘要不匹配，已跳过：%s", file_name)
                continue
            parsed = parse_template_archive(raw)
            with session_factory() as session:
                existing = session.scalar(
                    select(UnpackTemplate).where(UnpackTemplate.builtin_key == builtin_key)
                )
                if existing is not None and (existing.builtin_version or 0) >= version:
                    continue
            import_template_file(
                session_factory,
                parsed,
                origin=ORIGIN_BUILTIN,
                builtin_key=builtin_key,
                builtin_version=version,
            )
            installed += 1
            logger.info("内置模板已安装：%s v%s", builtin_key, version)
        except Exception as exc:  # noqa: BLE001 - 单条失败不能影响启动
            logger.warning("内置模板安装失败，已跳过：%s", exc)
            continue
    return installed


def _version_tuple(value: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in value.strip().split("."):
        digits = "".join(char for char in chunk if char.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)
