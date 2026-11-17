"""从 Transmission 已做种任务反向生成 .pbtemplate 模板文件。

适用场景：拆包不是在当前实例完成的（数据库里没有执行记录可导出），但下载器里
已经有拆包后的种子在保种。此时可以按「大包目录 + 站点」从下载器还原模板。

种子（站点）ID 只从 torrent 的 comment 里的详情页链接解析，并且要求该链接的域名
属于目标站点 —— 这样不会把「从 A 站转载、comment 保留 B 站链接」的种子误算成 A 站。

用法（需要 PACKBREAKER_CONFIG_DIR / PACKBREAKER_DATA_DIR）：

    uv run python scripts/build_template_from_transmission.py \
        --collection 周星驰.Stephen.Chow.1988-2017.BluRay.1080p.x265.10bit.2Audio.MNHD-FRDS \
        --site KEEPFRDS \
        --name "周星驰合集 1988-2017 1080p x265" \
        --output resources/official_templates/stephen-chow-keepfrds.pbtemplate
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, cast
from urllib.parse import urlparse

from backend.app.application.downloaders import DownloaderService
from backend.app.application.secrets import SecretStore
from backend.app.application.unpack_template_transfer import (
    TEMPLATE_FILE_KIND,
    TEMPLATE_FILE_SCHEMA_VERSION,
    _build_archive,
    parse_template_archive,
)
from backend.app.application.unpack_templates import _clean_mapping_media_name
from backend.app.config import AppSettings
from backend.app.domain.site_config import SiteKind
from backend.app.domain.verification import DownloaderKind
from backend.app.infrastructure.persistence.database import (
    create_session_factory,
    create_sqlite_engine,
)
from backend.app.infrastructure.persistence.models import utc_now
from backend.app.infrastructure.security import MasterKeyFile, SecretCipher
from backend.app.versioning import app_version

# 各站点的域名片段：用于从 comment/source 判断种子归属。
SITE_DOMAIN_TOKENS: dict[SiteKind, tuple[str, ...]] = {
    SiteKind.MTEAM: ("m-team",),
    SiteKind.HDTIME: ("hdtime",),
    SiteKind.HHCLUB: ("hhanclub", "hhclub"),
    SiteKind.KEEPFRDS: ("keepfrds",),
    SiteKind.HDHOME: ("hdhome",),
    SiteKind.UBITS: ("ubits",),
    SiteKind.HDFANS: ("hdfans",),
    SiteKind.BTSCHOOL: ("btschool",),
    SiteKind.PTTIME: ("pttime",),
    SiteKind.ROUSI_PRO: ("rousi",),
    SiteKind.LINGYIN_CLUB: ("soulvoice",),
    SiteKind.PTERCLUB: ("pterclub", "pter.club"),
    SiteKind.AUDIENCES: ("audiences",),
    SiteKind.SPRING_SUNDAY: ("springsunday",),
    SiteKind.HDDOLBY: ("hddolby",),
    SiteKind.U2: ("dmhy",),
    SiteKind.TANGPT: ("tangpt",),
    SiteKind.CARPT: ("carpt",),
}

TORRENT_ID_RE = re.compile(r"[?&]id=(\d+)")
VIDEO_SUFFIXES = (".mkv", ".mp4", ".ts", ".m2ts", ".avi", ".rmvb", ".wmv", ".mov")


@dataclass(slots=True)
class Candidate:
    movie: str
    torrent_id: str
    display_name: str
    total_size: int
    files: tuple[dict[str, Any], ...]
    # 影片目录相对大包根目录的前缀（大包里有子合集时非空，例如“电锯惊魂合集...”）
    relative_prefix: str = ""


@dataclass(slots=True)
class BuildStats:
    accepted: list[Candidate] = field(default_factory=list)
    missing_id: list[str] = field(default_factory=list)
    other_site: list[str] = field(default_factory=list)
    duplicated: list[str] = field(default_factory=list)


def kind_of_text(text: str) -> SiteKind | None:
    lowered = text.lower()
    for kind, tokens in SITE_DOMAIN_TOKENS.items():
        if any(token in lowered for token in tokens):
            return kind
    return None


def comment_site(comment: str) -> SiteKind | None:
    """只认 comment 里真正出现的链接域名；没有链接就返回 None。"""

    match = re.search(r"https?://[^\s]+", comment)
    if match is None:
        return None
    host = urlparse(match.group(0)).netloc.lower()
    for kind, tokens in SITE_DOMAIN_TOKENS.items():
        if any(token in host for token in tokens):
            return kind
    return None


def torrent_id_of(comment: str) -> str | None:
    match = TORRENT_ID_RE.search(comment)
    return match.group(1) if match else None


def main_file_size(files: tuple[dict[str, Any], ...], movie: str) -> tuple[int, str]:
    """返回（主视频大小, 主视频文件名）；按 torrent 内片名匹配优先，否则取最大视频。"""

    videos = [item for item in files if str(item.get("name", "")).lower().endswith(VIDEO_SUFFIXES)]
    if not videos:
        return 0, ""
    target = _clean_mapping_media_name(movie).lower()
    for item in videos:
        stem = PurePosixPath(str(item["name"])).stem.lower()
        if target and target in _clean_mapping_media_name(stem).lower():
            return int(item["length"]), PurePosixPath(str(item["name"])).name
    biggest = max(videos, key=lambda item: int(item["length"]))
    return int(biggest["length"]), PurePosixPath(str(biggest["name"])).name


def build_seed(candidate: Candidate, collection_root: str) -> dict[str, Any]:
    """把下载器里的种子翻译成模板 seed。

    两种形态必须分开处理：
    - 多文件种子：torrent 名就是影片目录，主视频在目录里；
    - 单文件种子：torrent 名就是 .mkv 本身，它所在目录才是「影片目录」。
    否则单文件种子会把 ``relative_directory`` 写成 ``xxx.mkv``，匹配和落位都找不到目录。
    """

    movie_directory = candidate.movie
    if movie_directory.lower().endswith(VIDEO_SUFFIXES):
        file_name = movie_directory
        relative_directory = candidate.relative_prefix or collection_root
        media_title = _clean_mapping_media_name(
            PurePosixPath(relative_directory).name or collection_root
        )
    else:
        relative_directory = (
            f"{candidate.relative_prefix}/{movie_directory}"
            if candidate.relative_prefix
            else movie_directory
        )
        _, file_name = main_file_size(candidate.files, movie_directory)
        media_title = _clean_mapping_media_name(movie_directory)
    seen: set[str] = set()
    summary: list[dict[str, Any]] = []
    for item in candidate.files:
        basename = PurePosixPath(str(item.get("name", ""))).name
        length = int(item.get("length") or 0)
        if not basename or length <= 0 or basename in seen:
            continue
        seen.add(basename)
        summary.append({"basename": basename, "length": length})
    return {
        "remote_torrent_id": candidate.torrent_id,
        "display_name": candidate.display_name,
        "total_size": candidate.total_size,
        "file_summary": summary,
        "mappings": [
            {
                "collection_root_name": collection_root,
                "relative_directory": relative_directory,
                "relative_file_name": file_name or None,
                "media_title": media_title,
                "confidence_bps": 10_000,
            }
        ],
    }


def _transmission_downloader_id(service: DownloaderService) -> str:
    for view in service.list_downloaders():
        if view.type is DownloaderKind.TRANSMISSION:
            return view.id
    raise SystemExit("未找到 Transmission 下载器配置")


async def collect(
    settings: AppSettings,
    collection: str,
    site: SiteKind,
    downloader_id: str | None,
) -> tuple[BuildStats, int]:
    engine = create_sqlite_engine(settings.database_path)
    session_factory = create_session_factory(engine)
    cipher = SecretCipher(MasterKeyFile.load_or_create(settings.resolved_secret_key_file))
    service = DownloaderService(
        session_factory, SecretStore(session_factory, cipher), data_root=settings.data_dir
    )
    resolved_id = downloader_id or _transmission_downloader_id(service)
    # 绑定上暴露的是写协议，实际对象带 RPC 入口；这里直接取原始适配器。
    adapter = cast(Any, service.transmission_write_binding(resolved_id).adapter)
    result, _ = await adapter._rpc(
        "torrent_get",
        {
            "fields": [
                "name",
                "total_size",
                "download_dir",
                "comment",
                "source",
                "files",
            ]
        },
    )
    # 大包可能有子合集：既收根目录下的种子，也收子目录里的种子（记下相对前缀）。
    rows: list[tuple[dict[str, Any], str]] = []
    for item in result.get("torrents") or []:
        parts = [part for part in (item.get("download_dir") or "").split("/") if part]
        if collection not in parts:
            continue
        tail = parts[parts.index(collection) + 1 :]
        rows.append((item, "/".join(tail)))
    stats = BuildStats()
    by_movie: dict[str, Candidate] = {}
    for item, relative_prefix in rows:
        movie = str(item.get("name") or "").strip()
        if not movie:
            continue
        comment = str(item.get("comment") or "")
        source_kind = kind_of_text(str(item.get("source") or ""))
        linked_kind = comment_site(comment)
        torrent_id = torrent_id_of(comment)
        if linked_kind is not None:
            if linked_kind is not site or torrent_id is None:
                stats.other_site.append(movie)
                continue
        elif source_kind is site:
            # 归属正确但 comment 不带详情链接：拿不到站点 ID，只能跳过。
            stats.missing_id.append(movie)
            continue
        else:
            stats.other_site.append(movie)
            continue
        files = tuple(item.get("files") or ())
        if movie in by_movie:
            stats.duplicated.append(movie)
            continue
        by_movie[movie] = Candidate(
            movie=movie,
            torrent_id=torrent_id,
            display_name=movie,
            total_size=int(item.get("total_size") or 0),
            files=files,
            relative_prefix=relative_prefix,
        )
    stats.accepted = [by_movie[key] for key in sorted(by_movie)]
    return stats, len(rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="从 Transmission 反向生成拆包模板文件")
    parser.add_argument("--collection", required=True, help="大包目录名（下载目录的最后一级）")
    parser.add_argument("--site", required=True, help="模板绑定的站点类型，例如 KEEPFRDS")
    parser.add_argument("--name", required=True, help="模板名称")
    parser.add_argument("--description", default=None, help="模板描述")
    parser.add_argument("--output", required=True, help="输出的 .pbtemplate 路径")
    parser.add_argument(
        "--downloader-id", default=None, help="Transmission 下载器 ID（默认自动选择）"
    )
    parser.add_argument("--min-seeds", type=int, default=1, help="低于该种子数视为失败")
    parser.add_argument("--dry-run", action="store_true", help="只统计不写文件")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    settings = AppSettings()
    site = SiteKind(args.site.strip().upper())
    stats, total = asyncio.run(collect(settings, args.collection.strip(), site, args.downloader_id))
    print(f"下载目录内种子：{total}")
    print(f"可生成种子：{len(stats.accepted)}")
    print(f"缺站点 ID（comment 无详情链接）：{len(set(stats.missing_id))}")
    print(f"非目标站点 / 链接指向别站：{len(set(stats.other_site))}")
    if stats.missing_id:
        for movie in sorted(set(stats.missing_id))[:10]:
            print("   缺 ID:", movie[:70])
    if len(stats.accepted) < args.min_seeds:
        raise SystemExit(f"可生成种子数 {len(stats.accepted)} 低于阈值 {args.min_seeds}")

    payload = {
        "schema_version": TEMPLATE_FILE_SCHEMA_VERSION,
        "site_key": site.value,
        "seeds": [build_seed(item, args.collection.strip()) for item in stats.accepted],
        "file_summary_source": "transmission",
    }
    payload_bytes = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    manifest = {
        "schema_version": TEMPLATE_FILE_SCHEMA_VERSION,
        "kind": TEMPLATE_FILE_KIND,
        "name": args.name.strip(),
        "description": args.description,
        "site_key": site.value,
        "seed_count": len(stats.accepted),
        "mapped_media_count": len(stats.accepted),
        "payload_sha256": hashlib.sha256(payload_bytes).hexdigest(),
        "exported_at": utc_now().isoformat(),
        "exporter_version": "1.1.6",
        "source": {"definition_name": args.collection.strip()},
    }
    archive = _build_archive(manifest, payload_bytes)
    # 用导入侧同一套解析器自检，避免写出一个自己都读不进的文件。
    parsed = parse_template_archive(archive)
    print(f"自检通过：{parsed.site_key} / {len(parsed.seeds)} 条种子 / 名称「{parsed.name}」")

    if args.dry_run:
        print("dry-run：未写入文件")
        return
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(archive)
    digest = hashlib.sha256(archive).hexdigest()
    print(f"已写出：{output}（{len(archive)} 字节）")
    print()
    print("index.json 条目：")
    slug = _clean_mapping_media_name(args.name).lower()[:40]
    print(
        json.dumps(
            {
                "builtin_key": f"{site.value.lower()}-{slug}",
                "name": args.name.strip(),
                "description": args.description,
                "site_key": site.value,
                "version": 1,
                "file": output.name,
                "sha256": digest,
                "seed_count": len(stats.accepted),
                "min_app_version": app_version(),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
