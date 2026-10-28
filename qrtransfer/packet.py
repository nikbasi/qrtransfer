"""Binary packet carried by one QR code.

    version      1
    file_id      4   first bytes of SHA-256(stream): keeps codes from different files apart
    stream_len   4   total stream length in bytes (gives k and the padding to strip)
    block_size   2
    seq          4   packet number (see fountain.py)
    payload      block_size
    crc32        4   over everything above

QR codes already carry Reed-Solomon error correction; the CRC is a second,
independent check that rejects the rare misread a QR decoder lets through.
"""
import struct
import zlib
from dataclasses import dataclass

VERSION = 1
_HEAD = struct.Struct(">B4sIHI")
OVERHEAD = _HEAD.size + 4


@dataclass(frozen=True)
class Packet:
    file_id: bytes
    stream_len: int
    block_size: int
    seq: int
    payload: bytes

    @property
    def k(self) -> int:
        return max(1, -(-self.stream_len // self.block_size))

    def to_bytes(self) -> bytes:
        body = _HEAD.pack(VERSION, self.file_id, self.stream_len, self.block_size, self.seq) + self.payload
        return body + zlib.crc32(body).to_bytes(4, "big")

    @classmethod
    def from_bytes(cls, raw: bytes) -> "Packet":
        if len(raw) < OVERHEAD:
            raise ValueError("too short")
        body, crc = raw[:-4], raw[-4:]
        if zlib.crc32(body).to_bytes(4, "big") != crc:
            raise ValueError("CRC mismatch")
        version, file_id, stream_len, block_size, seq = _HEAD.unpack_from(body)
        if version != VERSION:
            raise ValueError(f"unsupported packet version {version}")
        payload = body[_HEAD.size:]
        if len(payload) != block_size:
            raise ValueError("payload length mismatch")
        return cls(file_id, stream_len, block_size, seq, payload)
