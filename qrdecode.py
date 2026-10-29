#!/usr/bin/env python3
"""qrdecode: desktop decoder for qrencode pages (photos or videos, in any order).

    python qrdecode.py IMG_*.jpg            # asks for the password if the file is encrypted
    python qrdecode.py clip.mp4 -o out/

The web scanner (web/) does the same on a phone. This script is also the
reference the tests check the web scanner against.

Requires: pip install segno pillow numpy cryptography zstandard zxing-cpp opencv-python-headless
"""
import argparse
import getpass
import hashlib
import struct
import sys
import zlib
from pathlib import Path

import cv2
import numpy as np
import zxingcpp
from PIL import Image, ImageOps

import qrencode as fmt

VIDEO_EXT = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"}


class WrongPassword(Exception):
    pass


# ---------------------------------------------------------------- container


def is_encrypted(stream: bytes) -> bool:
    return stream[4] & fmt.FLAG_ENCRYPTED != 0


def unpack(stream: bytes, password: str | None = None) -> tuple[bytes, str]:
    """Return (original data, file name). Raises ValueError if anything does not verify."""
    if stream[:4] != fmt.MAGIC:
        raise ValueError("not a qrencode stream")
    if is_encrypted(stream):
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        (iterations,) = struct.unpack_from(">I", stream, 5)
        salt, iv = stream[9:25], stream[25:37]
        key = fmt.derive_key(password or "", salt, iterations)
        try:
            inner = AESGCM(key).decrypt(iv, stream[37:], stream[:37])
        except InvalidTag:
            raise WrongPassword("wrong password") from None
    else:
        inner = stream[5:]
    method, size, digest, name_len = fmt._INNER.unpack_from(inner)
    off = fmt._INNER.size
    name = inner[off:off + name_len].decode("utf-8", errors="replace")
    payload = inner[off + name_len:]
    if method == fmt.STORE:
        data = payload
    elif method == fmt.DEFLATE:
        data = zlib.decompress(payload, -15)
    elif method == fmt.ZSTD:
        import zstandard

        data = zstandard.ZstdDecompressor().decompress(payload, max_output_size=size)
    else:
        raise ValueError(f"unknown compression method {method}")
    if len(data) != size or hashlib.sha256(data).digest() != digest:
        raise ValueError("SHA-256 mismatch: reconstructed file is corrupt")
    return data, name


# ---------------------------------------------------------------- packets + fountain decoding


def parse_packet(raw: bytes):
    """-> (file_id, stream_len, block_size, seq, payload); ValueError if not a valid packet."""
    if len(raw) < fmt.PACKET_OVERHEAD:
        raise ValueError("too short")
    body, crc = raw[:-4], raw[-4:]
    if zlib.crc32(body).to_bytes(4, "big") != crc:
        raise ValueError("CRC mismatch")
    version, file_id, stream_len, block_size, seq = fmt.PACKET_HEAD.unpack_from(body)
    payload = body[fmt.PACKET_HEAD.size:]
    if version != fmt.PACKET_VERSION or len(payload) != block_size:
        raise ValueError("unsupported packet")
    return file_id, stream_len, block_size, seq, payload


class FountainDecoder:
    """Online Gaussian elimination over GF(2): feed packets in any order and amount."""

    def __init__(self, file_id: bytes, stream_len: int, block_size: int):
        self.file_id, self.stream_len, self.block_size = file_id, stream_len, block_size
        self.k = max(1, -(-stream_len // block_size))
        self.rows = {}  # pivot bit -> (mask, data); a row's lowest set bit is its pivot
        self.seen = set()

    @property
    def rank(self) -> int:
        return len(self.rows)

    @property
    def complete(self) -> bool:
        return self.rank == self.k

    def add(self, seq: int, payload: bytes) -> bool:
        """True if the packet carried new information."""
        if seq in self.seen or self.complete:
            return False
        self.seen.add(seq)
        mask = fmt.coefficients(self.file_id, seq, self.k)
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

    def stream(self) -> bytes:
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
        return np.concatenate(solved).tobytes()[:self.stream_len]


class Assembler:
    """Collects packets from any number of images; keeps files apart by file id."""

    def __init__(self):
        self.decoders: dict[bytes, FountainDecoder] = {}
        self.rejected = 0

    def feed(self, raw_codes) -> int:
        new = 0
        for raw in raw_codes:
            try:
                file_id, stream_len, block_size, seq, payload = parse_packet(raw)
            except ValueError:
                self.rejected += 1  # not ours, or failed the CRC
                continue
            dec = self.decoders.get(file_id)
            if dec is None:
                dec = self.decoders[file_id] = FountainDecoder(file_id, stream_len, block_size)
            new += dec.add(seq, payload)
        return new

    def finished(self) -> bool:
        return bool(self.decoders) and all(d.complete for d in self.decoders.values())

    def status(self) -> str:
        parts = [f"{fid.hex()}: complete" if d.complete else f"{fid.hex()}: {d.rank}/{d.k} codes"
                 for fid, d in self.decoders.items()]
        return "; ".join(parts) or "no valid codes yet"


# ---------------------------------------------------------------- reading images

CROSSTALK_LEVELS = (0.0, 0.25, 0.45)


def is_colorful(rgb: np.ndarray) -> bool:
    small = rgb[::8, ::8].astype(np.int16)
    return np.mean(small.max(axis=-1) - small.min(axis=-1) > 60) > 0.02


def crosstalk_matrix(a: float) -> np.ndarray:
    """observed = C @ true. Green leaks into both neighbours; red and blue leak mostly into green."""
    return np.array([[1 - a, 0.75 * a, 0.25 * a],
                     [0.5 * a, 1 - a, 0.5 * a],
                     [0.25 * a, 0.75 * a, 1 - a]], dtype=np.float32)


def normalise(rgb: np.ndarray, win: int) -> np.ndarray:
    """Per channel, scale between the local darkest and brightest pixels (removes white balance, light falloff)."""
    f = rgb.astype(np.float32)
    kernel = np.ones((win, win), np.uint8)
    hi = cv2.blur(cv2.dilate(f, kernel), (win, win))
    lo = cv2.blur(cv2.erode(f, kernel), (win, win))
    return np.clip((f - lo) / np.maximum(hi - lo, 25.0), 0.0, 1.0)


def color_layers(rgb: np.ndarray):
    """Colour photo -> candidate black-and-white images, one per QR layer and crosstalk guess."""
    h, w = rgb.shape[:2]
    if max(h, w) > 3000:
        s = 3000 / max(h, w)
        rgb = cv2.resize(rgb, (round(w * s), round(h * s)), interpolation=cv2.INTER_AREA)
    for c in range(3):
        yield np.ascontiguousarray(rgb[..., c])
    for win in (max(15, max(rgb.shape) // 64), max(31, max(rgb.shape) // 24)):
        norm = normalise(rgb, win | 1)
        for a in CROSSTALK_LEVELS:
            unmixed = norm @ np.linalg.inv(crosstalk_matrix(a)).T
            for c in range(3):
                yield np.where(unmixed[..., c] > 0.5, 255, 0).astype(np.uint8)


def _scan(img: np.ndarray, found: dict, binarizers=(zxingcpp.Binarizer.LocalAverage,)):
    for binarizer in binarizers:
        for r in zxingcpp.read_barcodes(img, formats=zxingcpp.BarcodeFormat.QRCode, binarizer=binarizer):
            if r.valid:
                found[r.bytes] = None


def read_codes(img: np.ndarray) -> list[bytes]:
    """All QR payloads in a grayscale or RGB image."""
    found = {}
    if img.ndim == 3 and is_colorful(img):
        for layer in color_layers(img):
            _scan(layer, found)
        return list(found)
    gray = np.asarray(Image.fromarray(img).convert("L")) if img.ndim == 3 else img
    variants = [gray, np.asarray(ImageOps.equalize(Image.fromarray(gray)))]
    if max(gray.shape) > 2500:
        variants.append(np.asarray(Image.fromarray(gray).reduce(2)))
    for v in variants:
        _scan(v, found, (zxingcpp.Binarizer.LocalAverage, zxingcpp.Binarizer.GlobalHistogram))
    return list(found)


def iter_frames(path: Path, step: int = 3):
    if path.suffix.lower() not in VIDEO_EXT:
        yield path.name, np.asarray(ImageOps.exif_transpose(Image.open(path)).convert("RGB"))
        return
    cap = cv2.VideoCapture(str(path))
    i = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if i % step == 0:
            yield f"{path.name}#{i}", cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        i += 1
    cap.release()


def main(argv=None):
    p = argparse.ArgumentParser(description="Decode qrencode photos or videos back into the original file.")
    p.add_argument("inputs", nargs="+", help="images or videos, in any order")
    p.add_argument("-o", "--out", help="output folder (default: current)")
    a = p.parse_args(argv)

    asm = Assembler()
    for path in map(Path, a.inputs):
        for label, img in iter_frames(path):
            codes = read_codes(img)
            new = asm.feed(codes)
            if codes:
                print(f"{label}: {len(codes)} codes, {new} new -> {asm.status()}")
            if asm.finished():
                break
        if asm.finished():
            break
    if not asm.finished():
        sys.exit(f"incomplete: {asm.status()}. Add more photos and run again with all of them.")

    out_dir = Path(a.out or ".")
    out_dir.mkdir(parents=True, exist_ok=True)
    for dec in asm.decoders.values():
        stream = dec.stream()
        password = getpass.getpass("Password: ") if is_encrypted(stream) else None
        try:
            data, name = unpack(stream, password)
        except WrongPassword:
            sys.exit("wrong password")
        dest = out_dir / Path(name).name
        dest.write_bytes(data)
        print(f"OK  {dest}  ({len(data)} B, verified)")


if __name__ == "__main__":
    main()
