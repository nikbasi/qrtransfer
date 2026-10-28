"""Systematic random-linear fountain code over GF(2).

The stream is cut into k equal blocks. Packet `seq < k` carries block `seq`
unchanged (so a clean read needs no decoding work). Packet `seq >= k` carries
the XOR of a pseudo-random subset of blocks; the subset is derived from
SHAKE-128(file_id || seq), so both sides compute it without sending it.

Any k linearly independent packets rebuild the stream. With dense random
subsets, k + 2 received packets succeed with ~75% probability and
k + 10 with >99.9%, no matter which ones went missing.
"""
import hashlib

import numpy as np


def coefficients(file_id: bytes, seq: int, k: int) -> int:
    """Bitmask of the source blocks XORed into packet `seq`."""
    if seq < k:
        return 1 << seq
    raw = hashlib.shake_128(file_id + seq.to_bytes(4, "big")).digest((k + 7) // 8)
    mask = int.from_bytes(raw, "little") & ((1 << k) - 1)
    return mask or 1 << (seq % k)


def split(stream: bytes, block_size: int) -> np.ndarray:
    k = max(1, -(-len(stream) // block_size))
    buf = np.zeros(k * block_size, dtype=np.uint8)
    buf[:len(stream)] = np.frombuffer(stream, dtype=np.uint8)
    return buf.reshape(k, block_size)


def encode_block(blocks: np.ndarray, file_id: bytes, seq: int) -> bytes:
    k = len(blocks)
    mask = coefficients(file_id, seq, k)
    out = np.zeros(blocks.shape[1], dtype=np.uint8)
    while mask:
        low = mask & -mask
        out ^= blocks[low.bit_length() - 1]
        mask ^= low
    return out.tobytes()


class Decoder:
    """Online Gaussian elimination: feed packets in any order, in any amount."""

    def __init__(self, file_id: bytes, k: int, block_size: int):
        self.file_id, self.k, self.block_size = file_id, k, block_size
        self.rows = {}  # pivot bit -> (mask, data); each row's lowest set bit is its pivot
        self.seen = set()

    @property
    def rank(self) -> int:
        return len(self.rows)

    @property
    def complete(self) -> bool:
        return self.rank == self.k

    def add(self, seq: int, payload: bytes) -> bool:
        """Returns True if the packet carried new information."""
        if seq in self.seen or self.complete:
            return False
        self.seen.add(seq)
        mask = coefficients(self.file_id, seq, self.k)
        data = np.frombuffer(payload, dtype=np.uint8).copy()
        while mask:
            pivot = (mask & -mask).bit_length() - 1
            row = self.rows.get(pivot)
            if row is None:
                self.rows[pivot] = (mask, data)
                return True
            mask ^= row[0]
            data ^= row[1]
        return False

    def missing_source_blocks(self) -> list[int]:
        """Source block indices not yet received directly (useful as a hint to the user)."""
        return [i for i in range(self.k) if i not in self.seen]

    def result(self) -> bytes:
        if not self.complete:
            raise RuntimeError(f"need {self.k - self.rank} more independent packets")
        solved = [None] * self.k
        for pivot in range(self.k - 1, -1, -1):
            mask, data = self.rows[pivot]
            data = data.copy()
            mask ^= 1 << pivot
            while mask:
                low = mask & -mask
                data ^= solved[low.bit_length() - 1]
                mask ^= low
            solved[pivot] = data
        return np.concatenate(solved).tobytes()
