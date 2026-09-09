from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from typing import TypeAlias, cast

from backend.app.domain.errors import DomainViolation, ErrorCode
from backend.app.domain.torrent import PieceLayer, TorrentFile, TorrentKind, TorrentMeta

_V1_HASH_BYTES = 20
_V2_HASH_BYTES = 32
_MAX_SIGNED_INTEGER = (1 << 63) - 1
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:$")


@dataclass(frozen=True, slots=True)
class BencodeLimits:
    max_payload_bytes: int = 20 * 1024 * 1024
    max_depth: int = 64
    max_string_bytes: int = 16 * 1024 * 1024
    max_items: int = 250_000
    max_files: int = 100_000
    max_total_declared_bytes: int = 1 << 60


BencodeValue: TypeAlias = int | bytes | list["BencodeNode"] | dict[bytes, "BencodeNode"]


@dataclass(frozen=True, slots=True)
class BencodeNode:
    value: BencodeValue
    start: int
    end: int


class _Decoder:
    def __init__(self, payload: bytes, limits: BencodeLimits) -> None:
        if len(payload) > limits.max_payload_bytes:
            raise _violation(ErrorCode.TORRENT_LIMIT_EXCEEDED, "torrent payload 超过解析上限")
        self.payload = payload
        self.limits = limits
        self.offset = 0
        self.items = 0

    def decode(self) -> BencodeNode:
        node = self._parse(depth=0)
        if self.offset != len(self.payload):
            raise _invalid("bencode 根对象后存在多余字节")
        return node

    def _parse(self, *, depth: int) -> BencodeNode:
        if depth > self.limits.max_depth:
            raise _violation(ErrorCode.TORRENT_LIMIT_EXCEEDED, "bencode 嵌套深度超过上限")
        self.items += 1
        if self.items > self.limits.max_items:
            raise _violation(ErrorCode.TORRENT_LIMIT_EXCEEDED, "bencode 元素数量超过上限")
        if self.offset >= len(self.payload):
            raise _invalid("bencode 意外结束")

        marker = self.payload[self.offset]
        if marker == ord("i"):
            return self._integer()
        if marker == ord("l"):
            return self._list(depth)
        if marker == ord("d"):
            return self._dictionary(depth)
        if ord("0") <= marker <= ord("9"):
            return self._bytes()
        raise _invalid("bencode 类型标记无效")

    def _integer(self) -> BencodeNode:
        start = self.offset
        self.offset += 1
        end = self.payload.find(b"e", self.offset)
        if end < 0:
            raise _invalid("bencode 整数缺少终止符")
        raw = self.payload[self.offset : end]
        if not raw or raw == b"-0" or raw.startswith(b"+"):
            raise _invalid("bencode 整数格式无效")
        digits = raw[1:] if raw.startswith(b"-") else raw
        if not digits.isdigit() or (len(digits) > 1 and digits.startswith(b"0")):
            raise _invalid("bencode 整数不是规范十进制")
        value = int(raw)
        if abs(value) > _MAX_SIGNED_INTEGER:
            raise _invalid("bencode 整数超出 64-bit 安全范围")
        self.offset = end + 1
        return BencodeNode(value, start, self.offset)

    def _bytes(self) -> BencodeNode:
        start = self.offset
        colon = self.payload.find(b":", self.offset)
        if colon < 0:
            raise _invalid("bencode 字符串缺少长度分隔符")
        raw_length = self.payload[self.offset : colon]
        if not raw_length or not raw_length.isdigit():
            raise _invalid("bencode 字符串长度无效")
        if len(raw_length) > 1 and raw_length.startswith(b"0"):
            raise _invalid("bencode 字符串长度不是规范十进制")
        length = int(raw_length)
        if length > self.limits.max_string_bytes:
            raise _violation(ErrorCode.TORRENT_LIMIT_EXCEEDED, "bencode 字符串超过长度上限")
        content_start = colon + 1
        content_end = content_start + length
        if content_end > len(self.payload):
            raise _invalid("bencode 字符串长度越过 payload")
        self.offset = content_end
        return BencodeNode(self.payload[content_start:content_end], start, content_end)

    def _list(self, depth: int) -> BencodeNode:
        start = self.offset
        self.offset += 1
        values: list[BencodeNode] = []
        while True:
            if self.offset >= len(self.payload):
                raise _invalid("bencode list 缺少终止符")
            if self.payload[self.offset] == ord("e"):
                self.offset += 1
                return BencodeNode(values, start, self.offset)
            values.append(self._parse(depth=depth + 1))

    def _dictionary(self, depth: int) -> BencodeNode:
        start = self.offset
        self.offset += 1
        values: dict[bytes, BencodeNode] = {}
        previous_key: bytes | None = None
        while True:
            if self.offset >= len(self.payload):
                raise _invalid("bencode dictionary 缺少终止符")
            if self.payload[self.offset] == ord("e"):
                self.offset += 1
                return BencodeNode(values, start, self.offset)
            key_node = self._bytes()
            key = cast(bytes, key_node.value)
            if previous_key is not None and key <= previous_key:
                raise _invalid("bencode dictionary key 重复或未按字节序递增")
            previous_key = key
            values[key] = self._parse(depth=depth + 1)


def parse_torrent(payload: bytes, *, limits: BencodeLimits | None = None) -> TorrentMeta:
    resolved_limits = limits or BencodeLimits()
    root = _Decoder(payload, resolved_limits).decode()
    top = _dict(root, "torrent 根对象")
    info_node = _required(top, b"info", "torrent 缺少 info dictionary")
    info = _dict(info_node, "torrent info")

    name_node = info.get(b"name.utf-8") or info.get(b"name")
    if name_node is None:
        raise _invalid("torrent info 缺少 name")
    name_raw = _bytes_value(name_node, "torrent name")
    display_name = _safe_segment(name_raw, first=True)

    piece_length = _positive_int(
        _required(info, b"piece length", "缺少 piece length"), "piece length"
    )
    if piece_length > (1 << 31):
        raise _invalid("piece length 超出安全范围")

    meta_version = _optional_int(info.get(b"meta version"), "meta version")
    has_v2 = meta_version == 2 or b"file tree" in info
    has_v1 = b"pieces" in info or b"files" in info or b"length" in info
    if meta_version not in {None, 2}:
        raise _invalid("不支持的 torrent meta version")
    if not has_v1 and not has_v2:
        raise _invalid("torrent info 不包含 v1 或 v2 文件声明")
    if has_v2 and meta_version != 2:
        raise _invalid("v2 file tree 必须声明 meta version=2")
    if has_v2 and (piece_length < 16 * 1024 or piece_length & (piece_length - 1)):
        raise _invalid("v2 piece length 必须是至少 16 KiB 的 2 次幂")

    v1_files: tuple[TorrentFile, ...] = ()
    v1_hashes: tuple[bytes, ...] = ()
    if has_v1:
        v1_files, v1_hashes = _parse_v1(info, display_name, name_raw, piece_length, resolved_limits)

    v2_files: tuple[TorrentFile, ...] = ()
    layers: tuple[PieceLayer, ...] = ()
    if has_v2:
        v2_files = _parse_v2(info, display_name, name_raw, resolved_limits)
        layers = _parse_piece_layers(top.get(b"piece layers"), v2_files, piece_length)

    if has_v1 and has_v2:
        _validate_hybrid(v1_files, v2_files)
        files = v2_files
        kind = TorrentKind.HYBRID
    elif has_v2:
        files = v2_files
        kind = TorrentKind.V2
    else:
        files = v1_files
        kind = TorrentKind.V1

    raw_info = payload[info_node.start : info_node.end]
    private_value = _optional_int(info.get(b"private"), "private")
    if private_value not in {None, 0, 1}:
        raise _invalid("private 字段只能是 0 或 1")
    source = _optional_text(info.get(b"source"), "source")
    return TorrentMeta(
        torrent_kind=kind,
        v1_info_hash=hashlib.sha1(raw_info).hexdigest() if has_v1 else None,
        v2_info_hash=hashlib.sha256(raw_info).hexdigest() if has_v2 else None,
        piece_length=piece_length,
        files=files,
        v1_piece_hashes=v1_hashes,
        v2_piece_layers=layers,
        private=private_value == 1,
        source=source,
        display_name=display_name,
        metainfo_digest=hashlib.sha256(payload).hexdigest(),
        info_span=(info_node.start, info_node.end),
    )


def _parse_v1(
    info: dict[bytes, BencodeNode],
    display_name: str,
    name_raw: bytes,
    piece_length: int,
    limits: BencodeLimits,
) -> tuple[tuple[TorrentFile, ...], tuple[bytes, ...]]:
    has_length = b"length" in info
    has_files = b"files" in info
    if has_length == has_files:
        raise _invalid("v1 torrent 必须且只能声明 length 或 files")

    files: list[TorrentFile] = []
    if has_length:
        length = _nonnegative_int(info[b"length"], "length")
        files.append(
            TorrentFile(
                path=display_name,
                raw_path_hex=(name_raw.hex(),),
                length=length,
                padding=False,
                zero_length=length == 0,
                order=0,
            )
        )
    else:
        entries = _list(info[b"files"], "files")
        if not entries or len(entries) > limits.max_files:
            raise _violation(ErrorCode.TORRENT_LIMIT_EXCEEDED, "v1 文件数量无效或超过上限")
        for order, entry_node in enumerate(entries):
            entry = _dict(entry_node, "v1 file")
            length = _nonnegative_int(_required(entry, b"length", "v1 file 缺少 length"), "length")
            path_node = entry.get(b"path.utf-8") or entry.get(b"path")
            if path_node is None:
                raise _invalid("v1 file 缺少 path")
            raw_segments = tuple(
                _bytes_value(segment, "v1 path segment") for segment in _list(path_node, "v1 path")
            )
            segments = _safe_path(raw_segments, root=(display_name, name_raw))
            attr = _optional_bytes(entry.get(b"attr"), "attr") or b""
            files.append(
                TorrentFile(
                    path="/".join(segments),
                    raw_path_hex=(name_raw.hex(), *(segment.hex() for segment in raw_segments)),
                    length=length,
                    padding=b"p" in attr,
                    zero_length=length == 0,
                    order=order,
                )
            )

    _validate_total(files, limits)
    pieces = _bytes_value(_required(info, b"pieces", "v1 torrent 缺少 pieces"), "pieces")
    if len(pieces) % _V1_HASH_BYTES:
        raise _invalid("v1 pieces 长度必须是 20 字节的整数倍")
    hashes = tuple(
        pieces[offset : offset + _V1_HASH_BYTES] for offset in range(0, len(pieces), _V1_HASH_BYTES)
    )
    total = sum(file.length for file in files)
    expected = (total + piece_length - 1) // piece_length if total else 0
    if len(hashes) != expected:
        raise _invalid("v1 piece hash 数量与声明总长度不一致")
    return tuple(files), hashes


def _parse_v2(
    info: dict[bytes, BencodeNode],
    display_name: str,
    name_raw: bytes,
    limits: BencodeLimits,
) -> tuple[TorrentFile, ...]:
    tree_node = _required(info, b"file tree", "v2 torrent 缺少 file tree")
    tree = _dict(tree_node, "file tree")
    files: list[TorrentFile] = []

    def walk(branch: dict[bytes, BencodeNode], raw_prefix: tuple[bytes, ...]) -> None:
        leaf = branch.get(b"")
        if leaf is not None:
            if len(branch) != 1 or not raw_prefix:
                raise _invalid("v2 file tree 叶节点结构无效")
            attributes = _dict(leaf, "v2 file attributes")
            length = _nonnegative_int(
                _required(attributes, b"length", "v2 file 缺少 length"), "length"
            )
            root = _optional_bytes(attributes.get(b"pieces root"), "pieces root")
            if length > 0 and (root is None or len(root) != _V2_HASH_BYTES):
                raise _invalid("非空 v2 file 必须包含 32 字节 pieces root")
            if length == 0 and root is not None:
                raise _invalid("零长度 v2 file 不应包含 pieces root")
            attr = _optional_bytes(attributes.get(b"attr"), "attr") or b""
            segments = _safe_path(raw_prefix, root=(display_name, name_raw))
            if len(files) >= limits.max_files:
                raise _violation(ErrorCode.TORRENT_LIMIT_EXCEEDED, "v2 文件数量超过上限")
            files.append(
                TorrentFile(
                    path="/".join(segments),
                    raw_path_hex=(name_raw.hex(), *(segment.hex() for segment in raw_prefix)),
                    length=length,
                    padding=b"p" in attr,
                    zero_length=length == 0,
                    order=len(files),
                    pieces_root=root,
                )
            )
            return
        for key, node in branch.items():
            if key == b"":
                continue
            child = _dict(node, "v2 file tree branch")
            walk(child, (*raw_prefix, key))

    walk(tree, ())
    if not files:
        raise _invalid("v2 file tree 不能为空")
    _validate_total(files, limits)
    return tuple(files)


def _parse_piece_layers(
    node: BencodeNode | None,
    files: tuple[TorrentFile, ...],
    piece_length: int,
) -> tuple[PieceLayer, ...]:
    if node is None:
        layer_map: dict[bytes, BencodeNode] = {}
    else:
        layer_map = _dict(node, "piece layers")
    known_roots = {file.pieces_root for file in files if file.pieces_root is not None}
    layers: list[PieceLayer] = []
    for root, value_node in layer_map.items():
        if len(root) != _V2_HASH_BYTES or root not in known_roots:
            raise _invalid("piece layers 包含未知 pieces root")
        raw = _bytes_value(value_node, "piece layer")
        if not raw or len(raw) % _V2_HASH_BYTES:
            raise _invalid("piece layer 必须由 32 字节 hash 组成")
        hashes = tuple(
            raw[offset : offset + _V2_HASH_BYTES] for offset in range(0, len(raw), _V2_HASH_BYTES)
        )
        layers.append(PieceLayer(root, hashes))

    layer_roots = {layer.pieces_root for layer in layers}
    for file in files:
        if file.length > piece_length and file.pieces_root not in layer_roots:
            raise _invalid("大于 piece length 的 v2 file 缺少 piece layer")
    return tuple(layers)


def _validate_hybrid(v1_files: tuple[TorrentFile, ...], v2_files: tuple[TorrentFile, ...]) -> None:
    left = tuple((file.path, file.length, file.padding) for file in v1_files)
    right = tuple((file.path, file.length, file.padding) for file in v2_files)
    if left != right:
        raise _violation(
            ErrorCode.TORRENT_HYBRID_INCONSISTENT,
            "hybrid torrent 的 v1/v2 文件路径、顺序、长度或 padding 声明不一致",
        )


def _validate_total(files: list[TorrentFile], limits: BencodeLimits) -> None:
    total = 0
    for file in files:
        total += file.length
        if total > limits.max_total_declared_bytes:
            raise _violation(ErrorCode.TORRENT_LIMIT_EXCEEDED, "torrent 声明总大小超过上限")


def _safe_path(raw_segments: tuple[bytes, ...], *, root: tuple[str, bytes]) -> tuple[str, ...]:
    if not raw_segments:
        raise _violation(ErrorCode.UNSAFE_TORRENT_PATH, "torrent 文件路径不能为空")
    first_name, first_raw = root
    segments = [first_name]
    for index, raw in enumerate(raw_segments, start=1):
        segments.append(_safe_segment(raw, first=index == 0))
    if _WINDOWS_DRIVE.fullmatch(first_name) or first_name.startswith(("/", "\\")):
        raise _violation(ErrorCode.UNSAFE_TORRENT_PATH, "torrent 根名称不能是绝对路径")
    if first_raw in {b".", b".."}:
        raise _violation(ErrorCode.UNSAFE_TORRENT_PATH, "torrent 根名称包含危险路径段")
    return tuple(segments)


def _safe_segment(raw: bytes, *, first: bool = False) -> str:
    try:
        value = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise _violation(ErrorCode.UNSAFE_TORRENT_PATH, "torrent 路径包含无效 UTF-8") from exc
    value = unicodedata.normalize("NFC", value)
    if not value or value in {".", ".."} or "\x00" in value:
        raise _violation(ErrorCode.UNSAFE_TORRENT_PATH, "torrent 路径包含危险空段或跳转段")
    if "/" in value or "\\" in value:
        raise _violation(ErrorCode.UNSAFE_TORRENT_PATH, "torrent 路径段包含目录分隔符")
    if first and (_WINDOWS_DRIVE.fullmatch(value) or value.startswith(("/", "\\"))):
        raise _violation(ErrorCode.UNSAFE_TORRENT_PATH, "torrent 根名称不能是绝对路径")
    return value


def _required(values: dict[bytes, BencodeNode], key: bytes, message: str) -> BencodeNode:
    node = values.get(key)
    if node is None:
        raise _invalid(message)
    return node


def _dict(node: BencodeNode, label: str) -> dict[bytes, BencodeNode]:
    if not isinstance(node.value, dict):
        raise _invalid(f"{label} 必须是 dictionary")
    return node.value


def _list(node: BencodeNode, label: str) -> list[BencodeNode]:
    if not isinstance(node.value, list):
        raise _invalid(f"{label} 必须是 list")
    return node.value


def _bytes_value(node: BencodeNode, label: str) -> bytes:
    if not isinstance(node.value, bytes):
        raise _invalid(f"{label} 必须是 byte string")
    return node.value


def _optional_bytes(node: BencodeNode | None, label: str) -> bytes | None:
    return None if node is None else _bytes_value(node, label)


def _nonnegative_int(node: BencodeNode, label: str) -> int:
    if not isinstance(node.value, int) or isinstance(node.value, bool) or node.value < 0:
        raise _invalid(f"{label} 必须是非负整数")
    return node.value


def _positive_int(node: BencodeNode, label: str) -> int:
    value = _nonnegative_int(node, label)
    if value == 0:
        raise _invalid(f"{label} 必须大于 0")
    return value


def _optional_int(node: BencodeNode | None, label: str) -> int | None:
    if node is None:
        return None
    if not isinstance(node.value, int) or isinstance(node.value, bool):
        raise _invalid(f"{label} 必须是整数")
    return node.value


def _optional_text(node: BencodeNode | None, label: str) -> str | None:
    if node is None:
        return None
    raw = _bytes_value(node, label)
    try:
        return unicodedata.normalize("NFC", raw.decode("utf-8", errors="strict"))
    except UnicodeDecodeError as exc:
        raise _invalid(f"{label} 包含无效 UTF-8") from exc


def _invalid(message: str) -> DomainViolation:
    return _violation(ErrorCode.TORRENT_META_INVALID, message)


def _violation(code: ErrorCode, message: str) -> DomainViolation:
    return DomainViolation(code, message)
