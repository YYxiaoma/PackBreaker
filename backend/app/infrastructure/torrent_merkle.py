from __future__ import annotations

import hashlib

V2_BLOCK_SIZE = 16 * 1024
_ZERO_LEAF = bytes(32)


def piece_layer_height(piece_length: int) -> int:
    if piece_length < V2_BLOCK_SIZE or piece_length % V2_BLOCK_SIZE:
        raise ValueError("piece length 必须是 16 KiB 的整数倍")
    blocks = piece_length // V2_BLOCK_SIZE
    if blocks & (blocks - 1):
        raise ValueError("piece length 对应的叶块数量必须是 2 次幂")
    return blocks.bit_length() - 1


def zero_hash(height: int) -> bytes:
    if height < 0:
        raise ValueError("Merkle 高度不能为负数")
    value = _ZERO_LEAF
    for _ in range(height):
        value = hashlib.sha256(value + value).digest()
    return value


def merkle_root(
    hashes: tuple[bytes, ...],
    *,
    pad_hash: bytes = _ZERO_LEAF,
    target_count: int | None = None,
) -> bytes:
    if not hashes:
        raise ValueError("Merkle tree 至少需要一个实际 hash")
    if any(len(value) != 32 for value in hashes) or len(pad_hash) != 32:
        raise ValueError("Merkle hash 必须为 32 字节")

    minimum = len(hashes)
    if target_count is None:
        target_count = 1 << (minimum - 1).bit_length()
    if target_count < minimum or target_count & (target_count - 1):
        raise ValueError("Merkle 目标节点数必须是不小于实际节点数的 2 次幂")

    level = [*hashes, *([pad_hash] * (target_count - minimum))]
    while len(level) > 1:
        level = [
            hashlib.sha256(level[index] + level[index + 1]).digest()
            for index in range(0, len(level), 2)
        ]
    return level[0]


def root_from_piece_layer(
    hashes: tuple[bytes, ...],
    *,
    piece_length: int,
    file_length: int,
) -> bytes:
    if file_length <= piece_length:
        raise ValueError("只有大于 piece length 的文件才使用 piece layer")
    expected = (file_length + piece_length - 1) // piece_length
    if len(hashes) != expected:
        raise ValueError("piece layer hash 数量与文件长度不一致")
    layer_height = piece_layer_height(piece_length)
    target_count = 1 << (expected - 1).bit_length()
    return merkle_root(
        hashes,
        pad_hash=zero_hash(layer_height),
        target_count=target_count,
    )
