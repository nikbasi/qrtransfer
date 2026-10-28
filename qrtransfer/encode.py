"""File -> pages (PNG), each holding a grid of QR codes."""
import hashlib
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import segno
from PIL import Image, ImageDraw, ImageFont

from . import container, fountain
from .packet import OVERHEAD, Packet

QUIET = 4          # quiet-zone modules around each QR (the spec minimum)
CAPTION_H = 0.045  # fraction of page height reserved for the caption


def qr_capacity(version: int, ecc: str) -> int:
    """Largest byte-mode payload for a QR version/ECC level."""
    lo, hi = 1, 3000
    while lo < hi:
        mid = (lo + hi + 1) // 2
        try:
            segno.make(b"\0" * mid, version=version, error=ecc, mode="byte", boost_error=False)
            lo = mid
        except segno.DataOverflowError:
            hi = mid - 1
    return lo


@dataclass
class Layout:
    width: int = 1920
    height: int = 1080
    cols: int = 3
    rows: int = 2
    version: int = 20
    ecc: str = "M"

    @property
    def per_page(self) -> int:
        return self.cols * self.rows


@dataclass
class EncodeResult:
    pages: list
    method: str
    original_size: int
    stream_size: int
    k: int
    packets: int


def _render_qr(data: bytes, version: int, ecc: str, module_px: int) -> Image.Image:
    qr = segno.make(data, version=version, error=ecc, mode="byte", boost_error=False)
    m = np.array(qr.matrix, dtype=bool)
    m = np.pad(m, QUIET, constant_values=False)
    img = np.where(m, 0, 255).astype(np.uint8)
    img = img.repeat(module_px, axis=0).repeat(module_px, axis=1)
    return Image.fromarray(img, "L")


def _font(size: int):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def encode(data: bytes, name: str, layout: Layout, redundancy: float = 0.15) -> EncodeResult:
    stream, method = container.pack(data, name)
    file_id = hashlib.sha256(stream).digest()[:4]
    block_size = qr_capacity(layout.version, layout.ecc) - OVERHEAD
    blocks = fountain.split(stream, block_size)
    k = len(blocks)

    # Systematic packets first, then repair packets; fill the last page completely
    # since empty slots would be wasted redundancy.
    total = k + max(2, math.ceil(k * redundancy))
    total = math.ceil(total / layout.per_page) * layout.per_page
    packets = [
        Packet(file_id, len(stream), block_size, seq, fountain.encode_block(blocks, file_id, seq)).to_bytes()
        for seq in range(total)
    ]

    qr_modules = 17 + 4 * layout.version + 2 * QUIET
    caption_h = int(layout.height * CAPTION_H)
    cell_w = layout.width // layout.cols
    cell_h = (layout.height - caption_h) // layout.rows
    module_px = min(cell_w, cell_h) // qr_modules
    if module_px < 2:
        raise ValueError("page too small for this grid/QR version; use fewer codes or a lower version")

    font = _font(max(12, int(caption_h * 0.6)))
    n_pages = total // layout.per_page
    pages = []
    for p in range(n_pages):
        page = Image.new("L", (layout.width, layout.height), 255)
        chunk = packets[p * layout.per_page:(p + 1) * layout.per_page]
        for i, raw in enumerate(chunk):
            qr = _render_qr(raw, layout.version, layout.ecc, module_px)
            r, c = divmod(i, layout.cols)
            x = c * cell_w + (cell_w - qr.width) // 2
            y = caption_h + r * cell_h + (cell_h - qr.height) // 2
            page.paste(qr, (x, y))
        caption = f"{name}  |  page {p + 1}/{n_pages}  |  any {k}+ codes rebuild the file  |  id {file_id.hex()}"
        ImageDraw.Draw(page).text((layout.width // 2, caption_h // 2), caption, fill=0, font=font, anchor="mm")
        pages.append(page)

    return EncodeResult(pages, method, len(data), len(stream), k, total)


def write_viewer(out_dir: Path, n_pages: int, interval_ms: int) -> Path:
    """Fullscreen slideshow: point a phone camera (video) at it, or step through with arrow keys."""
    files = ",".join(f'"page_{i:04d}.png"' for i in range(1, n_pages + 1))
    html = f"""<!doctype html><meta charset="utf-8"><title>qrtransfer</title>
<style>html,body{{margin:0;height:100%;background:#fff}}img{{width:100vw;height:100vh;object-fit:contain;image-rendering:pixelated}}</style>
<img id="p"><script>
const pages=[{files}];let i=0,auto=true;const img=document.getElementById('p');
const show=()=>img.src=pages[i];show();
setInterval(()=>{{if(auto){{i=(i+1)%pages.length;show();}}}},{interval_ms});
addEventListener('keydown',e=>{{if(e.key==='ArrowRight')i=(i+1)%pages.length;else if(e.key==='ArrowLeft')i=(i-1+pages.length)%pages.length;else if(e.key===' ')auto=!auto;else return;show();}});
addEventListener('click',()=>document.documentElement.requestFullscreen?.());
</script>"""
    path = out_dir / "viewer.html"
    path.write_text(html)
    return path
