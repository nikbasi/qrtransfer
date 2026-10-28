"""File container: picks the best compressor and wraps the result with metadata.

Layout (big-endian):
    magic      4  b"QTF1"
    method     1  compression method id
    orig_size  8  size of the original file
    sha256    32  hash of the original file (end-to-end check)
    name_len   1
    name       name_len bytes (UTF-8, truncated to 255 bytes)
    payload    rest
"""
import bz2
import hashlib
import lzma
import struct
import zlib

try:
    import zstandard
except ImportError:  # zstd is optional
    zstandard = None

MAGIC = b"QTF1"
_HEAD = struct.Struct(">4sBQ32sB")

STORE, ZLIB, BZ2, LZMA, ZSTD = range(5)
METHOD_NAMES = {STORE: "store", ZLIB: "zlib", BZ2: "bz2", LZMA: "lzma", ZSTD: "zstd"}

_LZMA_FILTERS = [{"id": lzma.FILTER_LZMA2, "preset": 9 | lzma.PRESET_EXTREME}]


def _compressors():
    yield ZLIB, lambda d: zlib.compress(d, 9)
    yield BZ2, lambda d: bz2.compress(d, 9)
    yield LZMA, lambda d: lzma.compress(d, format=lzma.FORMAT_RAW, filters=_LZMA_FILTERS)
    if zstandard is not None:
        yield ZSTD, lambda d: zstandard.ZstdCompressor(level=22).compress(d)


def _decompress(method, payload):
    if method == STORE:
        return payload
    if method == ZLIB:
        return zlib.decompress(payload)
    if method == BZ2:
        return bz2.decompress(payload)
    if method == LZMA:
        return lzma.decompress(payload, format=lzma.FORMAT_RAW, filters=_LZMA_FILTERS)
    if method == ZSTD:
        if zstandard is None:
            raise RuntimeError("file was compressed with zstd; install the 'zstandard' package")
        return zstandard.ZstdDecompressor().decompress(payload)
    raise ValueError(f"unknown compression method {method}")


def pack(data: bytes, name: str) -> tuple[bytes, str]:
    """Return (container bytes, name of the compression method chosen)."""
    best_method, best = STORE, data
    for method, compress in _compressors():
        out = compress(data)
        if len(out) < len(best):
            best_method, best = method, out
    name_b = name.encode("utf-8")[:255]
    head = _HEAD.pack(MAGIC, best_method, len(data), hashlib.sha256(data).digest(), len(name_b))
    return head + name_b + best, METHOD_NAMES[best_method]


def unpack(blob: bytes) -> tuple[bytes, str]:
    """Return (original data, file name). Raises ValueError if the hash does not match."""
    magic, method, size, digest, name_len = _HEAD.unpack_from(blob)
    if magic != MAGIC:
        raise ValueError("not a qrtransfer container")
    off = _HEAD.size
    name = blob[off:off + name_len].decode("utf-8", errors="replace")
    data = _decompress(method, blob[off + name_len:])
    if len(data) != size or hashlib.sha256(data).digest() != digest:
        raise ValueError("SHA-256 mismatch: reconstructed file is corrupt")
    return data, name
