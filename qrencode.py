#!/usr/bin/env python3
"""qrencode: turn a file into pages of QR codes that a phone can scan back.

    python qrencode.py report.pdf                 # pages + slideshow for photos
    python qrencode.py report.pdf --live          # big codes, made for scanning the slideshow live
    python qrencode.py report.pdf --color -p      # 3x denser colour codes, password protected
    echo "secret note" | python qrencode.py - -p  # encode text from stdin

Output: <name>_qr/viewer.html (open full screen) and page_NNNN.png.
Decode with the web scanner (web/) or qrdecode.py. Format: FORMAT.md.

Requires: pip install segno pillow numpy cryptography zstandard
"""
import argparse
import base64
import getpass
import hashlib
import io
import math
import os
import struct
import sys
import zlib
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import segno
from PIL import Image, ImageDraw, ImageFont

try:
    import zstandard
except ImportError:  # optional: deflate is used instead
    zstandard = None

# ---------------------------------------------------------------- container

MAGIC = b"QTF2"
FLAG_ENCRYPTED = 1
STORE, DEFLATE, ZSTD = 0, 1, 2
METHOD_NAMES = {STORE: "store", DEFLATE: "deflate", ZSTD: "zstd"}
PBKDF2_ITERATIONS = 600_000
_INNER = struct.Struct(">BI32sB")  # method, original size, sha256, name length


def _deflate(data: bytes) -> bytes:
    c = zlib.compressobj(9, zlib.DEFLATED, -15, 9)  # raw deflate, as DecompressionStream("deflate-raw")
    return c.compress(data) + c.flush()


def derive_key(password: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations, 32)


def pack(data: bytes, name: str, password: str | None = None) -> tuple[bytes, str]:
    """Return (stream bytes, compression method name)."""
    method, payload = STORE, data
    candidates = [(DEFLATE, _deflate(data))]
    if zstandard is not None:
        candidates.append((ZSTD, zstandard.ZstdCompressor(level=19).compress(data)))
    for m, out in candidates:
        if len(out) < len(payload):
            method, payload = m, out
    name_b = name.encode("utf-8")[:255]
    inner = _INNER.pack(method, len(data), hashlib.sha256(data).digest(), len(name_b)) + name_b + payload
    if not password:
        return MAGIC + bytes([0]) + inner, METHOD_NAMES[method]

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    salt, iv = os.urandom(16), os.urandom(12)
    header = MAGIC + bytes([FLAG_ENCRYPTED]) + struct.pack(">I", PBKDF2_ITERATIONS) + salt + iv
    key = derive_key(password, salt, PBKDF2_ITERATIONS)
    return header + AESGCM(key).encrypt(iv, inner, header), METHOD_NAMES[method]


# ---------------------------------------------------------------- fountain code


def _mulberry32(seed: int):
    state = seed & 0xFFFFFFFF
    while True:
        state = (state + 0x6D2B79F5) & 0xFFFFFFFF
        t = state
        t = ((t ^ (t >> 15)) * (t | 1)) & 0xFFFFFFFF
        t ^= (t + (((t ^ (t >> 7)) * (t | 61)) & 0xFFFFFFFF)) & 0xFFFFFFFF
        yield (t ^ (t >> 14)) & 0xFFFFFFFF


def coefficients(file_id: bytes, seq: int, k: int) -> int:
    """Bitmask (bit i = source block i) of the blocks XORed into packet `seq`."""
    if seq < k:
        return 1 << seq
    seed = int.from_bytes(file_id, "big") ^ (((seq + 1) * 0x9E3779B9) & 0xFFFFFFFF)
    rng = _mulberry32(seed)
    mask = 0
    for w in range((k + 31) // 32):
        mask |= next(rng) << (32 * w)
    mask &= (1 << k) - 1
    return mask or 1 << (seq % k)


def split(stream: bytes, block_size: int) -> np.ndarray:
    k = max(1, -(-len(stream) // block_size))
    buf = np.zeros(k * block_size, dtype=np.uint8)
    buf[:len(stream)] = np.frombuffer(stream, dtype=np.uint8)
    return buf.reshape(k, block_size)


def encode_block(blocks: np.ndarray, file_id: bytes, seq: int) -> bytes:
    mask = coefficients(file_id, seq, len(blocks))
    out = np.zeros(blocks.shape[1], dtype=np.uint8)
    while mask:
        low = mask & -mask
        out ^= blocks[low.bit_length() - 1]
        mask ^= low
    return out.tobytes()


# ---------------------------------------------------------------- packets

PACKET_VERSION = 2
PACKET_HEAD = struct.Struct(">B4sIHI")  # version, file_id, stream_len, block_size, seq
PACKET_OVERHEAD = PACKET_HEAD.size + 4  # + crc32


def make_packet(file_id: bytes, stream_len: int, block_size: int, seq: int, payload: bytes) -> bytes:
    body = PACKET_HEAD.pack(PACKET_VERSION, file_id, stream_len, block_size, seq) + payload
    return body + zlib.crc32(body).to_bytes(4, "big")


# ---------------------------------------------------------------- rendering

QUIET = 4  # quiet-zone modules around each QR


@dataclass
class Layout:
    width: int = 1920
    height: int = 1080
    cols: int = 3
    rows: int = 2
    version: int = 20
    ecc: str = "M"
    color: bool = False  # 3 QR codes per slot, one per RGB channel

    @property
    def slots(self) -> int:
        return self.cols * self.rows

    @property
    def per_page(self) -> int:
        return self.slots * (3 if self.color else 1)


PRESETS = {
    # for photos: dense pages, a 12 MP photo resolves small modules
    "photo": dict(cols=3, rows=2, version=20, ecc="M"),
    # for live scanning: a 1080p video frame needs bigger modules
    "live": dict(cols=3, rows=2, version=15, ecc="M"),
}


def qr_capacity(version: int, ecc: str) -> int:
    """Largest byte-mode payload for a QR version / ECC level."""
    lo, hi = 1, 3000
    while lo < hi:
        mid = (lo + hi + 1) // 2
        try:
            segno.make(b"\0" * mid, version=version, error=ecc, mode="byte", boost_error=False)
            lo = mid
        except segno.DataOverflowError:
            hi = mid - 1
    return lo


def _modules(data: bytes, version: int, ecc: str) -> np.ndarray:
    """Boolean module matrix (True = dark) including the quiet zone."""
    qr = segno.make(data, version=version, error=ecc, mode="byte", boost_error=False)
    return np.pad(np.array(qr.matrix, dtype=bool), QUIET, constant_values=False)


def _render_slot(payloads: list[bytes], layout: Layout, module_px: int) -> Image.Image:
    layers = [_modules(p, layout.version, layout.ecc) for p in payloads]
    if layout.color:  # channel c is dark where layer c is dark
        img, mode = np.stack([np.where(m, 0, 255) for m in layers], axis=-1).astype(np.uint8), "RGB"
    else:
        img, mode = np.where(layers[0], 0, 255).astype(np.uint8), "L"
    img = img.repeat(module_px, axis=0).repeat(module_px, axis=1)
    return Image.fromarray(img, mode)


def _font(size: int):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


@dataclass
class EncodeResult:
    pages: list
    method: str
    original_size: int
    stream_size: int
    k: int
    packets: int
    file_id: bytes


def encode(data: bytes, name: str, layout: Layout, redundancy: float = 0.3,
           password: str | None = None) -> EncodeResult:
    stream, method = pack(data, name, password)
    file_id = hashlib.sha256(stream).digest()[:4]
    block_size = qr_capacity(layout.version, layout.ecc) - PACKET_OVERHEAD
    blocks = split(stream, block_size)
    k = len(blocks)

    # data packets first, then repair packets; fill the last page (empty slots would be wasted)
    total = k + max(2, math.ceil(k * redundancy))
    total = math.ceil(total / layout.per_page) * layout.per_page
    packets = [make_packet(file_id, len(stream), block_size, seq, encode_block(blocks, file_id, seq))
               for seq in range(total)]

    qr_modules = 17 + 4 * layout.version + 2 * QUIET
    caption_h = int(layout.height * 0.045)
    cell_w = layout.width // layout.cols
    cell_h = (layout.height - caption_h) // layout.rows
    module_px = min(cell_w, cell_h) // qr_modules
    if module_px < 2:
        raise SystemExit("page too small for this grid / QR version: use fewer codes or a lower --qr-version")

    font = _font(max(12, int(caption_h * 0.6)))
    n_pages = total // layout.per_page
    per_slot = layout.per_page // layout.slots
    pages = []
    for p in range(n_pages):
        page = Image.new("RGB" if layout.color else "L", (layout.width, layout.height), "white")
        chunk = packets[p * layout.per_page:(p + 1) * layout.per_page]
        for i in range(layout.slots):
            qr = _render_slot(chunk[i * per_slot:(i + 1) * per_slot], layout, module_px)
            r, c = divmod(i, layout.cols)
            page.paste(qr, (c * cell_w + (cell_w - qr.width) // 2,
                            caption_h + r * cell_h + (cell_h - qr.height) // 2))
        lock = "  |  password" if password else ""
        caption = f"page {p + 1}/{n_pages}  |  any {k}+ codes rebuild the file{lock}  |  id {file_id.hex()}"
        ImageDraw.Draw(page).text((layout.width // 2, caption_h // 2), caption, fill="black", font=font, anchor="mm")
        pages.append(page)

    return EncodeResult(pages, method, len(data), len(stream), k, total, file_id)


def viewer_html(pages: list, interval_ms: int) -> str:
    """Self-contained full-screen slideshow (pages embedded), so one file is all you need to open."""
    urls = []
    for page in pages:
        buf = io.BytesIO()
        page.save(buf, "PNG", optimize=True)
        urls.append("data:image/png;base64," + base64.b64encode(buf.getvalue()).decode())
    pages_js = ",".join(f'"{u}"' for u in urls)
    return f"""<!doctype html><meta charset="utf-8"><title>qrencode slideshow</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>html,body{{margin:0;height:100%;background:#fff;overflow:hidden;font:14px system-ui}}
img{{width:100vw;height:100vh;object-fit:contain;image-rendering:pixelated}}
#hud{{position:fixed;right:8px;bottom:6px;color:#888}}</style>
<img id="p" alt=""><div id="hud"></div><script>
const pages=[{pages_js}];let i=0,auto=pages.length>1,ms={interval_ms};
const img=document.getElementById('p'),hud=document.getElementById('hud');
const show=()=>{{img.src=pages[i];hud.textContent=(i+1)+'/'+pages.length+(auto?'':' (paused)')+'  space: pause, arrows: step, +/-: speed, click: full screen';}};
show();let timer=setInterval(tick,ms);function tick(){{if(auto){{i=(i+1)%pages.length;show();}}}}
addEventListener('keydown',e=>{{
 if(e.key==='ArrowRight')i=(i+1)%pages.length;else if(e.key==='ArrowLeft')i=(i-1+pages.length)%pages.length;
 else if(e.key===' ')auto=!auto;else if(e.key==='+'||e.key==='-'){{ms=Math.max(100,ms*(e.key==='+'?0.8:1.25));clearInterval(timer);timer=setInterval(tick,ms);}}
 else return;show();}});
addEventListener('click',()=>document.documentElement.requestFullscreen?.());
</script>"""


# ---------------------------------------------------------------- CLI


def ask_password() -> str:
    while True:
        pw = getpass.getpass("Password: ")
        if not pw:
            print("empty password, try again", file=sys.stderr)
        elif getpass.getpass("Repeat password: ") == pw:
            return pw
        else:
            print("passwords differ, try again", file=sys.stderr)


def main(argv=None):
    p = argparse.ArgumentParser(description="Encode a file into pages of QR codes for scanning with a phone.")
    p.add_argument("file", help="file to encode, or - to read from stdin")
    p.add_argument("-o", "--out", help="output folder (default: <name>_qr)")
    p.add_argument("-p", "--password", action="store_true", help="encrypt; asks for a password")
    p.add_argument("--live", action="store_true", help="big codes for scanning the slideshow live (default: photo)")
    p.add_argument("--color", action="store_true", help="stack 3 QR codes per slot in R/G/B (3x data)")
    p.add_argument("--name", help="file name stored in the codes (default: the input name)")
    p.add_argument("--width", type=int, default=1920)
    p.add_argument("--height", type=int, default=1080)
    p.add_argument("--cols", type=int)
    p.add_argument("--rows", type=int)
    p.add_argument("--qr-version", type=int, help="1-40; higher = denser codes, harder to scan")
    p.add_argument("--ecc", choices="LMQH", help="QR error-correction level")
    p.add_argument("--redundancy", type=float, default=0.3, help="extra repair codes, as a fraction of data codes")
    p.add_argument("--interval", type=int, help="slideshow milliseconds per page")
    a = p.parse_args(argv)

    if a.file == "-":
        data, name = sys.stdin.buffer.read(), a.name or "message.txt"
    else:
        src = Path(a.file)
        data, name = src.read_bytes(), a.name or src.name
    if a.live and a.color:
        print("note: colour codes decode ~10x slower per frame, so live scanning is usually faster in "
              "black and white; colour pays off for photos", file=sys.stderr)
    password = ask_password() if a.password else None

    preset = dict(PRESETS["live" if a.live else "photo"])
    for key, val in (("cols", a.cols), ("rows", a.rows), ("version", a.qr_version), ("ecc", a.ecc)):
        if val is not None:
            preset[key] = val
    layout = Layout(a.width, a.height, color=a.color, **preset)
    res = encode(data, name, layout, a.redundancy, password)

    out = Path(a.out or f"{Path(name).stem}_qr")
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("page_*.png"):
        old.unlink()
    for i, page in enumerate(res.pages, 1):
        page.save(out / f"page_{i:04d}.png", optimize=True)
    interval = a.interval or (400 if a.live else 1500)
    (out / "viewer.html").write_text(viewer_html(res.pages, interval))

    enc = ", encrypted" if password else ""
    print(f"{name}: {res.original_size} B -> {res.stream_size} B ({res.method}{enc})")
    print(f"{res.k} data codes + {res.packets - res.k} repair codes on {len(res.pages)} page(s); "
          f"any {res.k} codes (+ a couple) rebuild the file")
    print(f"open {out / 'viewer.html'} full screen and scan it")


if __name__ == "__main__":
    main()
