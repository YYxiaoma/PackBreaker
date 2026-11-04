from __future__ import annotations

import asyncio
import json
import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.application.errors import ApplicationError
from backend.app.application.sites import SiteService
from backend.app.domain.candidate_scoring import score_candidate
from backend.app.domain.media_matching import (
    EpisodeIdentity,
    EpisodeKind,
    ExternalMediaId,
    MediaDescriptor,
    MediaFileSummary,
    parse_media_name,
)
from backend.app.domain.site_adapter import TorrentDetails
from backend.app.infrastructure.persistence.database import begin_immediate_write
from backend.app.infrastructure.persistence.models import (
    UnpackTemplate,
    UnpackTemplateImportJob,
    UnpackTemplateSeed,
    UnpackTemplateVersion,
    UnpackTorrentMetadata,
    new_uuid,
    utc_now,
)

_REMOTE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_SITE_PREFIX_RE = re.compile(
    r"^\s*\[(?:HDFans|HDTIME|M[\s_-]*TEAM|HHClub|UBits|U2|PTT(?:ime)?|KeepFrds|"
    r"Spring\s*Sunday|Audiences|PTerClub|CarPT|Rousi(?:\s+Pro)?|BTSCHOOL|HDDolby|聆音\s*Club)\]\s*",
    re.IGNORECASE,
)
_COMMENT_SUFFIX_RE = re.compile(
    r"\s*(?:用户评论|用户评价|评论|user\s+comments?)\s*$", re.IGNORECASE
)
_YEAR_RANGE_RE = re.compile(r"(?<!\d)(?:19|20)\d{2}\s*[-–—]\s*(?:19|20)\d{2}(?!\d)")
_COLLECTION_RELEASE_RE = re.compile(
    r"(?:\b(?:collection|trilogy|saga|box\s*set|film\s+collection)\b|合集|三部曲|全集|系列|"
    r"(?:19|20)\d{2}\s*[-–—]\s*(?:19|20)\d{2})",
    re.IGNORECASE,
)


def _clean_mapping_media_name(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value)
    normalized = _SITE_PREFIX_RE.sub("", normalized)
    normalized = _COMMENT_SUFFIX_RE.sub("", normalized)
    normalized = re.sub(r"\s*\[\s*\d+(?:\.\d+)?%\s*\]\s*$", "", normalized)
    return re.sub(r"(?<=[A-Za-z0-9])['’](?=[A-Za-z0-9])", "", normalized)


def _parse_mapping_media_name(value: str) -> MediaDescriptor:
    """Normalize site labels and PT annotations before comparing movie titles."""
    return parse_media_name(_clean_mapping_media_name(value))


def _parse_mapping_collection_name(value: str) -> MediaDescriptor:
    normalized = _YEAR_RANGE_RE.sub(" ", _clean_mapping_media_name(value))
    return parse_media_name(normalized)


def _mapping_name_sequence(value: str) -> tuple[str, ...]:
    """Preserve release-token order while ignoring a localized title prefix."""
    normalized = _clean_mapping_media_name(value).casefold()
    tokens = tuple(re.findall(r"[a-z0-9]+|[\u3400-\u9fff]+", normalized))
    english_tokens = tuple(token for token in tokens if re.fullmatch(r"[a-z0-9]+", token))
    return english_tokens if len(english_tokens) >= 2 else tokens


def _collection_token_variants(descriptor: MediaDescriptor) -> list[set[str]]:
    """Compare bilingual collection roots by their shared English alias when present."""
    variants: list[set[str]] = []
    for tokens in (descriptor.title_tokens, *descriptor.alias_tokens):
        if not tokens:
            continue
        english_tokens = {token for token in tokens if re.fullmatch(r"[a-z0-9]+", token)}
        variant = english_tokens or set(tokens)
        if variant and variant not in variants:
            variants.append(variant)
    return variants


def _is_collection_release_name(value: str) -> bool:
    return bool(_COLLECTION_RELEASE_RE.search(_clean_mapping_media_name(value)))


def _descriptor_title_similarity(left: MediaDescriptor, right: MediaDescriptor) -> float:
    left_variants = [set(tokens) for tokens in (left.title_tokens, *left.alias_tokens) if tokens]
    right_variants = [set(tokens) for tokens in (right.title_tokens, *right.alias_tokens) if tokens]
    similarities: list[float] = []
    for left_tokens in left_variants:
        for right_tokens in right_variants:
            if not left_tokens or not right_tokens:
                continue
            overlap = len(left_tokens & right_tokens)
            similarities.append(2 * overlap / (len(left_tokens) + len(right_tokens)))
            # Bilingual folder names can contain a full English title plus a
            # localized title/version token (for example, "哥斯拉-1.0.Godzilla.Minus.One").
            # Treat a complete alias of at least three tokens as a strong match.
            if min(len(left_tokens), len(right_tokens)) >= 3 and (
                left_tokens <= right_tokens or right_tokens <= left_tokens
            ):
                similarities.append(1.0)
    return max(similarities, default=0.0)


@dataclass(frozen=True, slots=True)
class UnpackTemplateView:
    id: str
    name: str
    description: str | None
    current_version: int
    status: str
    expected_seed_count: int
    seed_count: int
    mapped_media_count: int
    site_keys: tuple[str, ...]
    site_ids: tuple[str, ...]
    content_digest: str
    source_provenance: dict[str, Any]
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class UnpackTemplateImportJobView:
    id: str
    template_id: str
    template_version: int
    status: str
    total_count: int
    processed_count: int
    success_count: int
    failure_count: int
    errors: tuple[dict[str, Any], ...]
    created_at: datetime
    updated_at: datetime


class UnpackTemplateService:
    """持久化实例公共模板，并在后台通过现有站点凭证采集种子元数据。"""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        data_dir: Path | None = None,
        mapping_root: Path | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._data_dir = data_dir.resolve() if data_dir is not None else None
        self._mapping_root = mapping_root.resolve() if mapping_root is not None else None
        self._movie_directory_cache: dict[str, tuple[tuple[str, str, str | None], ...]] = {}

    def _movie_directories_for_root(self, root: Path) -> tuple[tuple[str, str, str | None], ...]:
        cache_key = str(root)
        cached = self._movie_directory_cache.get(cache_key)
        if cached is not None:
            return cached

        video_extensions = {
            ".mkv",
            ".mp4",
            ".m4v",
            ".avi",
            ".mov",
            ".m2ts",
            ".ts",
            ".wmv",
            ".webm",
            ".mpg",
            ".mpeg",
            ".flv",
            ".vob",
        }
        directories: list[tuple[str, str, str | None]] = []
        for current, child_directories, filenames in os.walk(root, topdown=True, followlinks=False):
            current_path = Path(current)
            relative = current_path.relative_to(root)
            depth = 0 if relative == Path(".") else len(relative.parts)
            child_directories[:] = [
                name for name in child_directories if not (current_path / name).is_symlink()
            ]
            if depth >= 4:
                child_directories[:] = []
            if depth == 0:
                continue
            video_files = sorted(
                filename
                for filename in filenames
                if Path(filename).suffix.casefold() in video_extensions
            )
            if not video_files:
                continue
            is_collection_container = _is_collection_release_name(current_path.name) and (
                len(video_files) > 1 or child_directories
            )
            if not is_collection_container:
                directories.append((relative.as_posix(), current_path.name, None))

            # A video filename may refine a normal movie directory, but never
            # let files directly inside a multi-film collection make the collection
            # root look like one movie. The directory mapping remains the preferred
            # root-level relation when its title is sufficiently specific.
            if not is_collection_container:
                directory_descriptor = _parse_mapping_media_name(current_path.name)
                for filename in video_files:
                    media_stem = Path(filename).stem
                    if not media_stem or media_stem == current_path.name:
                        continue
                    file_descriptor = _parse_mapping_media_name(media_stem)
                    if _descriptor_title_similarity(directory_descriptor, file_descriptor) < 0.80:
                        directories.append((relative.as_posix(), media_stem, filename))

        result = tuple(sorted(directories, key=lambda item: (item[0], item[1], item[2] or "")))
        self._movie_directory_cache[cache_key] = result
        return result

    def _infer_collection_source_mappings(
        self,
        root: Path,
        root_name: str,
        display_name: str,
    ) -> list[dict[str, Any]]:
        source_descriptor = _parse_mapping_collection_name(display_name)
        source_variants = _collection_token_variants(source_descriptor)
        if not source_variants:
            return []

        children_by_parent: dict[str, dict[tuple[str, str | None], str]] = {}
        for relative_directory, media_title, relative_file_name in self._movie_directories_for_root(
            root
        ):
            relative = PurePosixPath(relative_directory)
            if _is_collection_release_name(media_title):
                continue
            if _parse_mapping_media_name(media_title).year is None:
                continue
            if relative_file_name is None:
                if len(relative.parts) < 2 or media_title != relative.name:
                    continue
                if not root.joinpath(*relative.parts).is_dir():
                    continue
                parent_relative = relative.parent.as_posix()
            else:
                if not relative.parts or "/" in relative_file_name or "\\" in relative_file_name:
                    continue
                parent_path = root.joinpath(*relative.parts)
                file_path = parent_path / relative_file_name
                if not parent_path.is_dir() or file_path.is_symlink() or not file_path.is_file():
                    continue
                parent_relative = relative.as_posix()
            children_by_parent.setdefault(parent_relative, {})[
                (relative_directory, relative_file_name)
            ] = media_title

        ranked: list[
            tuple[
                tuple[int, float, float, int],
                str,
                dict[tuple[str, str | None], str],
                bool,
                float,
                float,
            ]
        ] = []
        for parent_relative, children in children_by_parent.items():
            if len(children) < 2:
                continue
            parent_name = PurePosixPath(parent_relative).name
            parent_descriptor = _parse_mapping_collection_name(parent_name)
            parent_variants = _collection_token_variants(parent_descriptor)
            if not parent_variants:
                continue
            exact = any(
                source_tokens == parent_tokens
                for source_tokens in source_variants
                for parent_tokens in parent_variants
            )
            coverage = max(
                len(source_tokens & parent_tokens) / len(source_tokens)
                for source_tokens in source_variants
                for parent_tokens in parent_variants
            )
            similarity = max(
                2 * len(source_tokens & parent_tokens) / (len(source_tokens) + len(parent_tokens))
                for source_tokens in source_variants
                for parent_tokens in parent_variants
            )
            if not exact and coverage < 0.80 and similarity < 0.80:
                continue
            subset_extras = [
                len(parent_tokens) - len(source_tokens)
                for source_tokens in source_variants
                for parent_tokens in parent_variants
                if source_tokens <= parent_tokens
            ]
            extra_tokens = min(subset_extras) if subset_extras else 10_000
            rank = (int(exact), round(coverage, 4), round(similarity, 4), -extra_tokens)
            ranked.append((rank, parent_relative, children, exact, coverage, similarity))

        if not ranked:
            return []
        ranked.sort(key=lambda item: item[0], reverse=True)
        best = ranked[0]
        if len(ranked) > 1 and ranked[1][0] == best[0]:
            # Two collection roots are equally plausible; do not duplicate or guess.
            return []
        _, _parent_relative, children, exact, coverage, similarity = best
        confidence = 10_000 if exact else min(9_900, round(max(coverage, similarity) * 10_000))
        mappings: list[dict[str, Any]] = []
        for (relative_directory, relative_file_name), media_title in sorted(
            children.items(), key=lambda item: (item[0][0], item[0][1] or "", item[1])
        ):
            mapping: dict[str, Any] = {
                "collection_root_name": root_name,
                "relative_directory": relative_directory,
                "media_title": media_title,
                "confidence_bps": confidence,
                "source_execution_id": None,
                "mapping_status": "SUGGESTED",
            }
            if relative_file_name is not None:
                mapping["relative_file_name"] = relative_file_name
            mappings.append(mapping)
        return mappings

    def _infer_source_mappings(
        self,
        source_provenance: dict[str, Any],
        display_name: str,
    ) -> list[dict[str, Any]]:
        if self._data_dir is None:
            return []
        root_name = source_provenance.get("collection_root_name")
        relative_root = source_provenance.get("collection_root_relative_path")
        if (
            not isinstance(root_name, str)
            or not root_name
            or not isinstance(relative_root, str)
            or not relative_root
        ):
            return []
        relative_path = PurePosixPath(relative_root.replace("\\", "/"))
        if relative_path.is_absolute() or any(
            part in {"", ".", ".."} for part in relative_path.parts
        ):
            return []
        if self._mapping_root is not None:
            # An explicitly configured mapping root is read-only and must match
            # the collection name recorded in the template provenance.
            root = self._mapping_root
            if root.name != root_name:
                return []
        else:
            root = (self._data_dir / Path(*relative_path.parts)).resolve()
            if not root.is_relative_to(self._data_dir):
                return []
        if not root.is_dir():
            return []

        if _is_collection_release_name(display_name):
            # A collection seed may legitimately relate to several detailed movie
            # directories. If the root has no unambiguous child movie directories,
            # keep it unmapped rather than treating the whole pack as one film.
            return self._infer_collection_source_mappings(root, root_name, display_name)

        source_descriptor = _parse_mapping_media_name(display_name)
        if not source_descriptor.title_tokens and not source_descriptor.alias_tokens:
            return []
        source_sequence = _mapping_name_sequence(display_name)
        ranked: list[tuple[float, float, str, str, bool, str | None]] = []
        for relative_directory, media_title, relative_file_name in self._movie_directories_for_root(
            root
        ):
            directory_descriptor = _parse_mapping_media_name(media_title)
            if (
                source_descriptor.year is not None
                and directory_descriptor.year is not None
                and source_descriptor.year != directory_descriptor.year
            ):
                continue
            title_similarity = _descriptor_title_similarity(source_descriptor, directory_descriptor)
            if title_similarity < 0.90:
                continue
            score = score_candidate(source_descriptor, directory_descriptor)
            if score.rejected:
                continue
            confidence = score.score
            if (
                source_descriptor.year is not None
                and directory_descriptor.year == source_descriptor.year
            ):
                confidence = min(100.0, confidence + 2.0)
            elif source_descriptor.year is not None and directory_descriptor.year is None:
                confidence = max(0.0, confidence - 10.0)
            sequence_match = source_sequence == _mapping_name_sequence(media_title)
            ranked.append(
                (
                    confidence,
                    title_similarity,
                    relative_directory,
                    media_title,
                    sequence_match,
                    relative_file_name,
                )
            )

        ranked.sort(
            key=lambda item: (item[0], item[1], item[4], item[2], item[5] or ""),
            reverse=True,
        )
        if not ranked:
            return []
        exact_matches = {
            (item[2], item[3], item[5]): item
            for item in ranked
            if item[4] and item[0] >= 90.0 and item[1] >= 0.90
        }
        if len(exact_matches) == 1:
            # Exact normalized release-token order can disambiguate otherwise
            # identical title/year scores, but only when that exact path is unique.
            best = next(iter(exact_matches.values()))
        else:
            best = ranked[0]
            if best[0] < 90.0 or best[1] < 0.90:
                return []
            if len(ranked) > 1:
                second = ranked[1]
                if (best[2], best[5]) != (second[2], second[5]) and (
                    best[0] - second[0] < 5.0 or best[1] - second[1] < 0.04
                ):
                    # 多个目录都很像时不猜，保留为未映射以便后续人工确认。
                    return []
        mapping: dict[str, Any] = {
            "collection_root_name": root_name,
            "relative_directory": best[2],
            "media_title": best[3],
            "confidence_bps": min(10_000, round(best[0] * 100)),
            "source_execution_id": None,
            "mapping_status": "SUGGESTED",
        }
        if best[5] is not None:
            mapping["relative_file_name"] = best[5]
        return [mapping]

    def list_templates(self, site_service: SiteService) -> tuple[UnpackTemplateView, ...]:
        with self._session_factory() as session:
            templates = session.scalars(
                select(UnpackTemplate).order_by(
                    UnpackTemplate.updated_at.desc(), UnpackTemplate.name
                )
            ).all()
            snapshots: list[tuple[UnpackTemplate, UnpackTemplateVersion | None, int]] = []
            for template in templates:
                version = session.scalar(
                    select(UnpackTemplateVersion)
                    .where(UnpackTemplateVersion.template_id == template.id)
                    .where(UnpackTemplateVersion.version == template.current_version)
                )
                mapped_paths: set[tuple[str, str, str | None]] = set()
                if version is not None:
                    mapping_rules_rows = session.scalars(
                        select(UnpackTemplateSeed.mapping_rules).where(
                            UnpackTemplateSeed.template_version_id == version.id
                        )
                    ).all()
                    for rules in mapping_rules_rows:
                        source_mappings = (
                            rules.get("source_mappings") if isinstance(rules, dict) else None
                        )
                        if not isinstance(source_mappings, list):
                            continue
                        for mapping in source_mappings:
                            if not isinstance(mapping, dict):
                                continue
                            root_name = mapping.get("collection_root_name")
                            relative_directory = mapping.get("relative_directory")
                            relative_file_name = mapping.get("relative_file_name")
                            if isinstance(root_name, str) and isinstance(relative_directory, str):
                                mapped_paths.add(
                                    (
                                        root_name,
                                        relative_directory,
                                        relative_file_name
                                        if isinstance(relative_file_name, str)
                                        else None,
                                    )
                                )
                snapshots.append((template, version, len(mapped_paths)))

        result: list[UnpackTemplateView] = []
        for template, version, mapped_media_count in snapshots:
            if version is None:
                continue
            site_keys = tuple(
                sorted({item for item in version.site_keys if isinstance(item, str) and item})
            )
            site_ids = (
                tuple(
                    dict.fromkeys(
                        reference.config_id
                        for reference in site_service.config_references_for_site_keys(site_keys)
                        if reference.enabled
                    )
                )
                if len(site_keys) == 1
                else ()
            )
            result.append(
                UnpackTemplateView(
                    id=template.id,
                    name=template.name,
                    description=template.description,
                    current_version=version.version,
                    status=version.status,
                    expected_seed_count=version.expected_seed_count,
                    seed_count=version.seed_count,
                    mapped_media_count=mapped_media_count,
                    site_keys=site_keys,
                    site_ids=site_ids,
                    content_digest=version.content_digest,
                    source_provenance=dict(template.source_provenance or {}),
                    created_at=template.created_at,
                    updated_at=template.updated_at,
                )
            )
        return tuple(result)

    def create_import_job(
        self,
        *,
        site_service: SiteService,
        site_config_id: str,
        torrent_ids: tuple[str, ...],
        name: str | None = None,
        description: str | None = None,
        template_id: str | None = None,
        source_label: str | None = None,
        seed_mappings: tuple[dict[str, Any], ...] = (),
    ) -> UnpackTemplateImportJobView:
        normalized_ids = _normalize_torrent_ids(torrent_ids)
        normalized_mappings = _normalize_seed_mappings(
            seed_mappings, allowed_torrent_ids=frozenset(normalized_ids)
        )
        binding = site_service.adapter_for_config(site_config_id)
        now = utc_now()
        safe_provenance = {
            "source": "site_detail_api",
            "site_key": binding.site_id,
            **(
                {"source_label": source_label.strip()[:200]}
                if source_label and source_label.strip()
                else {}
            ),
        }
        with self._session_factory() as session:
            begin_immediate_write(session)
            if template_id is None:
                normalized_name = (name or "").strip()
                if not normalized_name or len(normalized_name) > 120:
                    raise self._invalid("新模板必须提供 1 到 120 个字符的名称")
                if session.scalar(
                    select(UnpackTemplate.id).where(UnpackTemplate.name == normalized_name)
                ):
                    raise self._conflict("已存在同名模板，请选择现有模板追加种子或更换名称")
                template = UnpackTemplate(
                    id=new_uuid(),
                    name=normalized_name,
                    description=(
                        description.strip()[:2000] if description and description.strip() else None
                    ),
                    current_version=1,
                    source_provenance=safe_provenance,
                    created_at=now,
                    updated_at=now,
                )
                version_number = 1
                version_id = new_uuid()
                version = UnpackTemplateVersion(
                    id=version_id,
                    template_id=template.id,
                    version=version_number,
                    schema_version=1,
                    status="IMPORTING",
                    expected_seed_count=len(normalized_ids),
                    seed_count=0,
                    site_keys=[binding.site_id],
                    content_digest="",
                    source_provenance=safe_provenance,
                    created_at=now,
                )
                session.add_all([template, version])
                new_ids = normalized_ids
            else:
                existing_template = session.get(UnpackTemplate, template_id)
                if existing_template is None:
                    raise self._not_found()
                template = existing_template
                current = session.scalar(
                    select(UnpackTemplateVersion)
                    .where(UnpackTemplateVersion.template_id == template.id)
                    .where(UnpackTemplateVersion.version == template.current_version)
                )
                if current is None or current.status == "IMPORTING":
                    raise self._conflict("模板当前版本仍在导入中，不能并行追加")
                bound_site_keys = {
                    item for item in (current.site_keys or []) if isinstance(item, str) and item
                }
                if not bound_site_keys:
                    provenance_site_key = dict(current.source_provenance or {}).get("site_key")
                    if isinstance(provenance_site_key, str) and provenance_site_key:
                        bound_site_keys.add(provenance_site_key)
                if bound_site_keys != {binding.site_id}:
                    raise self._conflict("官方模板已绑定其他站点，不能跨站追加种子")
                existing_rows = session.execute(
                    select(UnpackTemplateSeed, UnpackTorrentMetadata)
                    .join(
                        UnpackTorrentMetadata,
                        UnpackTorrentMetadata.id == UnpackTemplateSeed.metadata_id,
                    )
                    .where(UnpackTemplateSeed.template_version_id == current.id)
                ).all()
                if any(metadata.site_key != binding.site_id for _seed, metadata in existing_rows):
                    raise self._conflict("模板现有种子包含其他站点，拒绝跨站追加")
                existing_keys = {
                    (metadata.site_key, metadata.remote_torrent_id)
                    for _seed, metadata in existing_rows
                }
                mapping_ids = {item["torrent_id"] for item in normalized_mappings}
                new_ids = tuple(
                    torrent_id
                    for torrent_id in normalized_ids
                    if (binding.site_id, torrent_id) not in existing_keys
                    or torrent_id in mapping_ids
                )
                if not new_ids:
                    raise self._conflict("提交的种子均已存在于当前模板版本，无需重复导入")
                new_seed_count = sum(
                    (binding.site_id, torrent_id) not in existing_keys for torrent_id in new_ids
                )
                version_number = current.version + 1
                version_id = new_uuid()
                site_keys = [binding.site_id]
                version = UnpackTemplateVersion(
                    id=version_id,
                    template_id=template.id,
                    version=version_number,
                    schema_version=1,
                    status="IMPORTING",
                    expected_seed_count=max(
                        current.expected_seed_count, current.seed_count + new_seed_count
                    ),
                    seed_count=current.seed_count,
                    site_keys=site_keys,
                    content_digest="",
                    source_provenance={**dict(current.source_provenance or {}), **safe_provenance},
                    created_at=now,
                )
                session.add(version)
                session.flush()
                for seed, metadata in existing_rows:
                    inferred_mappings = self._infer_source_mappings(
                        dict(current.source_provenance or {}), metadata.display_name
                    )
                    session.add(
                        UnpackTemplateSeed(
                            id=new_uuid(),
                            template_version_id=version_id,
                            metadata_id=seed.metadata_id,
                            mapping_rules=_merge_seed_mapping_rules(
                                seed.mapping_rules, inferred_mappings
                            ),
                            sort_order=seed.sort_order,
                            created_at=now,
                        )
                    )
                template.current_version = version_number
                template.source_provenance = {
                    **dict(template.source_provenance or {}),
                    **safe_provenance,
                }
                template.updated_at = now

            # Materialize template/version and any copied seeds before inserting the job FK.
            # The models intentionally avoid ORM relationships, so table-level FK ordering alone
            # is not guaranteed by the unit of work.
            session.flush()
            job = UnpackTemplateImportJob(
                id=new_uuid(),
                template_version_id=version_id,
                site_config_id=site_config_id,
                site_key=binding.site_id,
                torrent_ids=list(new_ids),
                seed_mappings=list(normalized_mappings),
                next_index=0,
                success_count=0,
                failure_count=0,
                errors=[],
                status="PENDING",
                created_at=now,
                updated_at=now,
            )
            session.add(job)
            session.commit()
            return self._job_view(session, job)

    def get_import_job(self, job_id: str) -> UnpackTemplateImportJobView:
        with self._session_factory() as session:
            job = session.get(UnpackTemplateImportJob, job_id)
            if job is None:
                raise self._not_found()
            return self._job_view(session, job)

    async def process_next_import_item(self, site_service: SiteService) -> bool:
        with self._session_factory() as session:
            begin_immediate_write(session)
            job = session.scalar(
                select(UnpackTemplateImportJob)
                .where(UnpackTemplateImportJob.status.in_(("PENDING", "RUNNING")))
                .order_by(UnpackTemplateImportJob.created_at, UnpackTemplateImportJob.id)
                .limit(1)
            )
            if job is None:
                return False
            if job.next_index >= len(job.torrent_ids):
                self._finish_job(session, job)
                session.commit()
                return True
            job.status = "RUNNING"
            job.updated_at = utc_now()
            job_id = job.id
            site_config_id = job.site_config_id
            site_key = job.site_key
            torrent_id = job.torrent_ids[job.next_index]
            job_seed_mappings = [
                dict(item) for item in (job.seed_mappings or []) if isinstance(item, dict)
            ]
            version_id = job.template_version_id
            version_record = session.get(UnpackTemplateVersion, version_id)
            version_source_provenance = (
                dict(version_record.source_provenance or {}) if version_record is not None else {}
            )
            index = job.next_index
            session.commit()

        metadata_payload: dict[str, Any] | None = None
        failure_code: str | None = None
        retry_after_seconds: float | None = None
        try:
            binding = site_service.adapter_for_config(site_config_id)
            if binding.site_id != site_key:
                raise ValueError("site adapter identity changed")
            # SiteService binds enabled adapters through ReliableSiteAdapter, which
            # already owns the HTTP timeout, capability-based rate interval, retry
            # deadline and attempt limit. A shorter outer timeout can cancel a valid
            # M-Team 90-second wait before the adapter's bounded retry policy completes.
            details = await binding.adapter.fetch_details(torrent_id)
            if details.site_id != site_key or details.torrent_id != torrent_id:
                raise ValueError("site detail identity mismatch")
            metadata_payload = _metadata_payload(details, site_key)
        except Exception as exc:
            code = getattr(exc, "code", None)
            failure_code = (
                code
                if isinstance(code, str) and re.fullmatch(r"[A-Z0-9_]{1,96}", code)
                else "UNPACK_TEMPLATE_IMPORT_FAILED"
            )
            retry_after = getattr(exc, "retry_after_seconds", None)
            if isinstance(retry_after, (int, float)) and retry_after >= 0:
                retry_after_seconds = float(retry_after)

        if failure_code in {"SITE_RATE_LIMITED", "SITE_CIRCUIT_OPEN"}:
            # Preserve this exact item and resume it after the site's advertised wait.
            # Advancing the cursor here would turn temporary rate limiting into hundreds
            # of permanent failures and could leave the template incomplete unnecessarily.
            delay = retry_after_seconds if retry_after_seconds and retry_after_seconds > 0 else 30.0
            await asyncio.sleep(min(3600.0, max(0.05, delay)))
            return True

        source_mappings = _source_mappings_for_torrent(job_seed_mappings, torrent_id)
        if metadata_payload is not None and not source_mappings:
            display_name = metadata_payload.get("display_name")
            if isinstance(display_name, str):
                source_mappings = self._infer_source_mappings(
                    version_source_provenance, display_name
                )

        with self._session_factory() as session:
            begin_immediate_write(session)
            job = session.get(UnpackTemplateImportJob, job_id)
            version = session.get(UnpackTemplateVersion, version_id)
            if job is None or version is None:
                return True
            if job.next_index != index:
                return True
            now = utc_now()
            if metadata_payload is not None:
                metadata = session.scalar(
                    select(UnpackTorrentMetadata)
                    .where(UnpackTorrentMetadata.site_key == site_key)
                    .where(UnpackTorrentMetadata.remote_torrent_id == torrent_id)
                )
                if metadata is None:
                    metadata = UnpackTorrentMetadata(
                        id=new_uuid(),
                        site_key=site_key,
                        remote_torrent_id=torrent_id,
                        display_name=metadata_payload["display_name"],
                        descriptor=metadata_payload["descriptor"],
                        total_size=metadata_payload["total_size"],
                        file_summary=metadata_payload["file_summary"],
                        external_ids=metadata_payload["external_ids"],
                        info_hash=None,
                        metadata_digest=metadata_payload["metadata_digest"],
                        fetched_at=metadata_payload["fetched_at"],
                        verification_status="DETAILS_VERIFIED",
                        source_provenance={"source": "site_detail_api", "site_key": site_key},
                        created_at=now,
                        updated_at=now,
                    )
                    session.add(metadata)
                    session.flush()
                else:
                    metadata.display_name = metadata_payload["display_name"]
                    incoming_files = [
                        dict(item)
                        for item in metadata_payload["file_summary"]
                        if isinstance(item, dict)
                    ]
                    existing_files = [
                        dict(item)
                        for item in (metadata.file_summary or [])
                        if isinstance(item, dict)
                    ]
                    incoming_has_paths = any(
                        isinstance(item.get("path"), str) and item["path"]
                        for item in incoming_files
                    )
                    existing_has_paths = any(
                        isinstance(item.get("path"), str) and item["path"]
                        for item in existing_files
                    )
                    incoming_descriptor = dict(metadata_payload["descriptor"])
                    existing_descriptor = dict(metadata.descriptor or {})
                    preserve_existing_files = existing_has_paths and not incoming_has_paths
                    if (
                        not incoming_descriptor.get("files") or preserve_existing_files
                    ) and existing_descriptor.get("files"):
                        incoming_descriptor["files"] = existing_descriptor["files"]
                    metadata.descriptor = incoming_descriptor
                    if metadata_payload["total_size"] is not None and (
                        not existing_has_paths or incoming_has_paths or metadata.total_size is None
                    ):
                        metadata.total_size = metadata_payload["total_size"]
                    if incoming_files and (
                        not existing_files or incoming_has_paths or not existing_has_paths
                    ):
                        metadata.file_summary = incoming_files
                    metadata.external_ids = metadata_payload["external_ids"]
                    metadata.metadata_digest = _digest(
                        {
                            "site_key": site_key,
                            "remote_torrent_id": torrent_id,
                            "display_name": metadata.display_name,
                            "descriptor": metadata.descriptor,
                            "total_size": metadata.total_size,
                            "file_summary": metadata.file_summary,
                            "external_ids": metadata.external_ids,
                        }
                    )
                    metadata.fetched_at = metadata_payload["fetched_at"]
                    metadata.verification_status = "DETAILS_VERIFIED"
                    metadata.source_provenance = {
                        **dict(metadata.source_provenance or {}),
                        "details_source": "site_detail_api",
                        "site_key": site_key,
                    }
                    metadata.updated_at = now
                existing_seed = session.scalar(
                    select(UnpackTemplateSeed)
                    .where(UnpackTemplateSeed.template_version_id == version.id)
                    .where(UnpackTemplateSeed.metadata_id == metadata.id)
                )
                if existing_seed is None:
                    session.add(
                        UnpackTemplateSeed(
                            id=new_uuid(),
                            template_version_id=version.id,
                            metadata_id=metadata.id,
                            mapping_rules=_merge_seed_mapping_rules({}, source_mappings),
                            sort_order=version.seed_count,
                            created_at=now,
                        )
                    )
                elif source_mappings:
                    existing_seed.mapping_rules = _merge_seed_mapping_rules(
                        existing_seed.mapping_rules, source_mappings
                    )
                job.success_count += 1
            else:
                job.failure_count += 1
                errors = list(job.errors or [])
                errors.append(
                    {
                        "torrent_id": torrent_id,
                        "code": failure_code or "UNPACK_TEMPLATE_IMPORT_FAILED",
                        "message": "获取站点种子详情失败；请检查站点连接与种子 ID 后重试",
                    }
                )
                job.errors = errors[-100:]
            job.next_index += 1
            job.updated_at = now
            # The runtime session factory disables autoflush. Flush the new seed before counting it.
            session.flush()
            version.seed_count = int(
                session.scalar(
                    select(func.count(UnpackTemplateSeed.id)).where(
                        UnpackTemplateSeed.template_version_id == version.id
                    )
                )
                or 0
            )
            if job.next_index >= len(job.torrent_ids):
                self._finish_job(session, job)
            session.commit()
        return True

    def _finish_job(self, session: Session, job: UnpackTemplateImportJob) -> None:
        version = session.get(UnpackTemplateVersion, job.template_version_id)
        if version is None:
            return
        seeds = session.execute(
            select(UnpackTemplateSeed, UnpackTorrentMetadata)
            .join(UnpackTorrentMetadata, UnpackTorrentMetadata.id == UnpackTemplateSeed.metadata_id)
            .where(UnpackTemplateSeed.template_version_id == version.id)
            .order_by(UnpackTemplateSeed.sort_order, UnpackTemplateSeed.id)
        ).all()
        version.seed_count = len(seeds)
        version.site_keys = sorted({metadata.site_key for _seed, metadata in seeds})
        digest_payload = [
            {
                "site_key": metadata.site_key,
                "remote_torrent_id": metadata.remote_torrent_id,
                "metadata_digest": metadata.metadata_digest,
                "mapping_rules": dict(seed.mapping_rules or {}),
            }
            for seed, metadata in seeds
        ]
        version.content_digest = _digest(digest_payload)
        version.status = (
            "READY"
            if job.failure_count == 0
            and version.seed_count >= version.expected_seed_count
            and len(version.site_keys) == 1
            and version.site_keys[0] == job.site_key
            else "INCOMPLETE"
        )
        job.status = "COMPLETED" if version.status == "READY" else "COMPLETED_WITH_ERRORS"
        template = session.get(UnpackTemplate, version.template_id)
        if template is not None:
            template.updated_at = utc_now()

    @staticmethod
    def _job_view(session: Session, job: UnpackTemplateImportJob) -> UnpackTemplateImportJobView:
        version = session.get(UnpackTemplateVersion, job.template_version_id)
        if version is None:
            raise UnpackTemplateService._not_found()
        return UnpackTemplateImportJobView(
            id=job.id,
            template_id=version.template_id,
            template_version=version.version,
            status=job.status,
            total_count=len(job.torrent_ids),
            processed_count=job.next_index,
            success_count=job.success_count,
            failure_count=job.failure_count,
            errors=tuple(dict(item) for item in (job.errors or []) if isinstance(item, dict)),
            created_at=job.created_at,
            updated_at=job.updated_at,
        )

    @staticmethod
    def _invalid(detail: str) -> ApplicationError:
        return ApplicationError(
            code="UNPACK_TEMPLATE_INVALID",
            status=422,
            title="模板配置无效",
            detail=detail,
        )

    @staticmethod
    def _conflict(detail: str) -> ApplicationError:
        return ApplicationError(
            code="UNPACK_TEMPLATE_CONFLICT",
            status=409,
            title="模板状态冲突",
            detail=detail,
        )

    @staticmethod
    def _not_found() -> ApplicationError:
        return ApplicationError(
            code="UNPACK_TEMPLATE_NOT_FOUND",
            status=404,
            title="模板不存在",
            detail="未找到指定模板或导入任务",
        )


def _normalize_seed_mappings(
    values: tuple[dict[str, Any], ...], *, allowed_torrent_ids: frozenset[str]
) -> tuple[dict[str, Any], ...]:
    if len(values) > 5000:
        raise UnpackTemplateService._invalid("单次模板导入最多支持 5000 条影片目录映射")
    normalized: dict[tuple[str, str, str, str | None], dict[str, Any]] = {}
    for value in values:
        if not isinstance(value, dict):
            raise UnpackTemplateService._invalid("影片目录映射格式无效")
        torrent_id = value.get("torrent_id")
        if not isinstance(torrent_id, str) or torrent_id.strip() not in allowed_torrent_ids:
            raise UnpackTemplateService._invalid("影片目录映射必须关联本次导入的站点种子 ID")
        torrent_id = torrent_id.strip()
        root_raw = value.get("collection_root_name")
        if not isinstance(root_raw, str):
            raise UnpackTemplateService._invalid("影片目录映射缺少集合种子根目录名")
        root_name = unicodedata.normalize("NFC", root_raw.strip())
        if (
            not root_name
            or len(root_name) > 255
            or root_name in {".", ".."}
            or "/" in root_name
            or "\\" in root_name
            or "\x00" in root_name
        ):
            raise UnpackTemplateService._invalid("集合种子根目录名必须是安全的单级目录名")
        relative_raw = value.get("relative_directory")
        if not isinstance(relative_raw, str):
            raise UnpackTemplateService._invalid("影片目录映射缺少根目录下的相对影片目录")
        relative_value = unicodedata.normalize("NFC", relative_raw.strip().replace("\\", "/"))
        relative_path = PurePosixPath(relative_value)
        if (
            not relative_value
            or "\x00" in relative_value
            or relative_path.is_absolute()
            or re.match(r"^[A-Za-z]:", relative_value)
            or relative_path.as_posix() in {"", "."}
            or any(part in {".", ".."} for part in relative_value.split("/"))
            or len(relative_path.as_posix()) > 1024
        ):
            raise UnpackTemplateService._invalid("影片目录映射必须是集合种子根目录下的安全相对路径")
        relative_directory = relative_path.as_posix()
        title_raw = value.get("media_title")
        if not isinstance(title_raw, str):
            raise UnpackTemplateService._invalid("影片目录映射缺少影片名称")
        media_title = unicodedata.normalize("NFC", title_raw.strip())
        if not media_title or len(media_title) > 512 or "\x00" in media_title:
            raise UnpackTemplateService._invalid("影片名称必须为 1 到 512 个字符")
        confidence = value.get("confidence_bps", 0)
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, int)
            or not 0 <= confidence <= 10_000
        ):
            raise UnpackTemplateService._invalid("影片目录映射置信度必须位于 0 到 10000")
        relative_file_raw = value.get("relative_file_name")
        relative_file_name: str | None = None
        if relative_file_raw is not None:
            if not isinstance(relative_file_raw, str):
                raise UnpackTemplateService._invalid("影片文件映射的文件名无效")
            relative_file_name = unicodedata.normalize("NFC", relative_file_raw.strip())
            if (
                not relative_file_name
                or len(relative_file_name) > 255
                or "/" in relative_file_name
                or "\\" in relative_file_name
                or "\x00" in relative_file_name
                or relative_file_name in {".", ".."}
            ):
                raise UnpackTemplateService._invalid("影片文件映射只能包含安全的单级文件名")
        source_execution_id = value.get("source_execution_id")
        if source_execution_id is not None and (
            not isinstance(source_execution_id, str)
            or not source_execution_id.strip()
            or len(source_execution_id.strip()) > 36
            or "\x00" in source_execution_id
        ):
            raise UnpackTemplateService._invalid("影片目录映射的来源执行 ID 无效")
        key = (torrent_id, root_name, relative_directory, relative_file_name)
        item = {
            "torrent_id": torrent_id,
            "collection_root_name": root_name,
            "relative_directory": relative_directory,
            "media_title": media_title,
            "confidence_bps": confidence,
            "source_execution_id": (
                source_execution_id.strip() if isinstance(source_execution_id, str) else None
            ),
        }
        if relative_file_name is not None:
            item["relative_file_name"] = relative_file_name
        previous = normalized.get(key)
        if previous is None or item["confidence_bps"] > previous["confidence_bps"]:
            normalized[key] = item
    return tuple(normalized.values())


def _source_mappings_for_torrent(
    values: list[dict[str, Any]], torrent_id: str
) -> list[dict[str, Any]]:
    mappings: list[dict[str, Any]] = []
    for item in values:
        if (
            item.get("torrent_id") != torrent_id
            or not isinstance(item.get("collection_root_name"), str)
            or not isinstance(item.get("relative_directory"), str)
            or not isinstance(item.get("media_title"), str)
        ):
            continue
        mapping: dict[str, Any] = {
            "collection_root_name": item["collection_root_name"],
            "relative_directory": item["relative_directory"],
            "media_title": item["media_title"],
            "confidence_bps": item["confidence_bps"],
            "source_execution_id": item.get("source_execution_id"),
            "mapping_status": "SUGGESTED",
        }
        if isinstance(item.get("relative_file_name"), str):
            mapping["relative_file_name"] = item["relative_file_name"]
        mappings.append(mapping)
    return mappings


def _merge_seed_mapping_rules(
    existing_rules: object, additions: list[dict[str, Any]]
) -> dict[str, Any]:
    rules = dict(existing_rules) if isinstance(existing_rules, dict) else {}
    raw_mappings = rules.get("source_mappings")
    mappings: dict[tuple[str, str, str | None], dict[str, Any]] = {}
    if isinstance(raw_mappings, list):
        for item in raw_mappings:
            if not isinstance(item, dict):
                continue
            root_name = item.get("collection_root_name")
            relative_directory = item.get("relative_directory")
            relative_file_name = item.get("relative_file_name")
            if isinstance(root_name, str) and isinstance(relative_directory, str):
                mappings[
                    (
                        root_name,
                        relative_directory,
                        relative_file_name if isinstance(relative_file_name, str) else None,
                    )
                ] = dict(item)
    for item in additions:
        relative_file_name = item.get("relative_file_name")
        key = (
            item["collection_root_name"],
            item["relative_directory"],
            relative_file_name if isinstance(relative_file_name, str) else None,
        )
        previous = mappings.get(key)
        if previous is None or int(item.get("confidence_bps", 0)) > int(
            previous.get("confidence_bps", 0)
        ):
            mappings[key] = dict(item)
    if mappings:
        rules["source_mappings"] = [
            mappings[key]
            for key in sorted(mappings, key=lambda value: (value[0], value[1], value[2] or ""))
        ]
    return rules


def _normalize_torrent_ids(values: tuple[str, ...]) -> tuple[str, ...]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = value.strip()
        if not _REMOTE_ID_RE.fullmatch(item):
            raise UnpackTemplateService._invalid("种子 ID 只能包含字母、数字、下划线和连字符")
        if item not in seen:
            seen.add(item)
            normalized.append(item)
    if not normalized:
        raise UnpackTemplateService._invalid("至少提供一个有效的站点种子 ID")
    if len(normalized) > 1000:
        raise UnpackTemplateService._invalid("单次模板导入最多支持 1000 个种子 ID")
    return tuple(normalized)


def _metadata_payload(details: TorrentDetails, site_key: str) -> dict[str, Any]:
    candidate = details.candidate
    descriptor = _descriptor_payload(candidate.descriptor)
    files = [
        {"basename": item.basename, "length": item.length}
        for item in (candidate.file_summary or candidate.descriptor.files)
    ]
    external_ids = [
        {"namespace": item.namespace, "value": item.value}
        for item in candidate.descriptor.external_ids
    ]
    payload = {
        "site_key": site_key,
        "remote_torrent_id": details.torrent_id,
        "display_name": candidate.display_name,
        "descriptor": descriptor,
        "total_size": candidate.total_size,
        "file_summary": files,
        "external_ids": external_ids,
    }
    return {
        **payload,
        "metadata_digest": _digest(payload),
        "fetched_at": utc_now(),
    }


def _descriptor_payload(descriptor: MediaDescriptor) -> dict[str, Any]:
    episode = descriptor.episode
    return {
        "raw_name": descriptor.raw_name,
        "title_tokens": list(descriptor.title_tokens),
        "aliases": [list(value) for value in descriptor.alias_tokens],
        "year": descriptor.year,
        "episode": None
        if episode is None
        else {
            "kind": episode.kind.value,
            "season": episode.season,
            "start": episode.start,
            "end": episode.end,
        },
        "resolution": descriptor.resolution,
        "release_source": descriptor.release_source,
        "codec": descriptor.codec,
        "hdr": descriptor.hdr,
        "audio": descriptor.audio,
        "language": descriptor.language,
        "release_group": descriptor.release_group,
        "version": descriptor.version,
        "external_ids": [
            {"namespace": item.namespace, "value": item.value} for item in descriptor.external_ids
        ],
        "total_size": descriptor.total_size,
        "files": [{"basename": item.basename, "length": item.length} for item in descriptor.files],
    }


def descriptor_from_payload(
    payload: object, *, display_name: str, total_size: int | None
) -> MediaDescriptor:
    if not isinstance(payload, dict):
        return parse_media_name(display_name, total_size=total_size)
    try:
        raw_episode = payload.get("episode")
        episode = None
        if isinstance(raw_episode, dict):
            episode = EpisodeIdentity(
                kind=EpisodeKind(str(raw_episode["kind"])),
                season=_optional_int(raw_episode.get("season")),
                start=_optional_int(raw_episode.get("start")),
                end=_optional_int(raw_episode.get("end")),
            )
        external_ids = tuple(
            ExternalMediaId(str(item["namespace"]), str(item["value"]))
            for item in payload.get("external_ids", [])
            if isinstance(item, dict) and item.get("namespace") and item.get("value")
        )
        files = tuple(
            MediaFileSummary(str(item["basename"]), int(item["length"]))
            for item in payload.get("files", [])
            if isinstance(item, dict) and isinstance(item.get("basename"), str)
        )
        return MediaDescriptor(
            raw_name=str(payload.get("raw_name") or display_name),
            title_tokens=tuple(str(item) for item in payload.get("title_tokens", [])),
            alias_tokens=tuple(
                tuple(str(token) for token in alias)
                for alias in payload.get("aliases", [])
                if isinstance(alias, list)
            ),
            year=_optional_int(payload.get("year")),
            episode=episode,
            resolution=_optional_str(payload.get("resolution")),
            release_source=_optional_str(payload.get("release_source")),
            codec=_optional_str(payload.get("codec")),
            hdr=_optional_str(payload.get("hdr")),
            audio=_optional_str(payload.get("audio")),
            language=_optional_str(payload.get("language")),
            release_group=_optional_str(payload.get("release_group")),
            version=_optional_str(payload.get("version")),
            external_ids=external_ids,
            total_size=_optional_int(payload.get("total_size"))
            if payload.get("total_size") is not None
            else total_size,
            files=files,
        )
    except (KeyError, TypeError, ValueError):
        return parse_media_name(display_name, total_size=total_size)


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _digest(value: object) -> str:
    return sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
