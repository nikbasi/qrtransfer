"""Photos / video frames -> original file."""
from pathlib import Path

import numpy as np
import zxingcpp
from PIL import Image, ImageOps

from . import container
from .fountain import Decoder
from .packet import Packet

VIDEO_EXT = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"}


def read_codes(img: np.ndarray) -> list[bytes]:
    """All QR payloads found in a grayscale image. Tries a few variants because
    phone photos vary a lot in sharpness, size and lighting."""
    found = {}
    variants = [img]
    h, w = img.shape
    if max(h, w) > 2500:
        variants.append(np.asarray(Image.fromarray(img).reduce(2)))
    variants.append(np.asarray(ImageOps.equalize(Image.fromarray(img))))
    for v in variants:
        for binarizer in (zxingcpp.Binarizer.LocalAverage, zxingcpp.Binarizer.GlobalHistogram):
            for r in zxingcpp.read_barcodes(v, formats=zxingcpp.BarcodeFormat.QRCode, binarizer=binarizer):
                if r.valid:
                    found[r.bytes] = None
    return list(found)


def load_image(path: Path) -> np.ndarray:
    img = ImageOps.exif_transpose(Image.open(path))
    return np.asarray(img.convert("L"))


def iter_video_frames(path: Path, step: int = 3):
    import cv2  # only needed for video input

    cap = cv2.VideoCapture(str(path))
    i = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if i % step == 0:
            yield f"{path.name}#{i}", cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        i += 1
    cap.release()


class Assembler:
    """Collects packets from any number of images; tracks one or more files by file_id."""

    def __init__(self):
        self.decoders: dict[bytes, Decoder] = {}
        self.stream_len: dict[bytes, int] = {}
        self.rejected = 0

    def feed(self, raw_codes: list[bytes]) -> int:
        """Returns how many codes carried new information."""
        new = 0
        for raw in raw_codes:
            try:
                pkt = Packet.from_bytes(raw)
            except ValueError:
                self.rejected += 1  # not ours, or failed the CRC
                continue
            dec = self.decoders.get(pkt.file_id)
            if dec is None:
                dec = self.decoders[pkt.file_id] = Decoder(pkt.file_id, pkt.k, pkt.block_size)
                self.stream_len[pkt.file_id] = pkt.stream_len
            new += dec.add(pkt.seq, pkt.payload)
        return new

    def finished(self) -> bool:
        return bool(self.decoders) and all(d.complete for d in self.decoders.values())

    def results(self):
        """Yield (file_id, data, name) for each complete file. container.unpack verifies SHA-256."""
        for fid, dec in self.decoders.items():
            if dec.complete:
                data, name = container.unpack(dec.result()[:self.stream_len[fid]])
                yield fid, data, name

    def status(self) -> str:
        parts = []
        for fid, d in self.decoders.items():
            if d.complete:
                parts.append(f"{fid.hex()}: complete")
            else:
                parts.append(f"{fid.hex()}: {d.rank}/{d.k} (need {d.k - d.rank} more codes)")
        return "; ".join(parts) or "no valid codes yet"


def decode_inputs(paths: list[Path], log=print) -> Assembler:
    asm = Assembler()
    for path in paths:
        sources = iter_video_frames(path) if path.suffix.lower() in VIDEO_EXT else [(path.name, load_image(path))]
        for label, img in sources:
            codes = read_codes(img)
            new = asm.feed(codes)
            if codes or path.suffix.lower() not in VIDEO_EXT:
                log(f"{label}: {len(codes)} codes read, {new} new  ->  {asm.status()}")
            if asm.finished():
                return asm
    return asm
