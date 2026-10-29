#!/usr/bin/env python3
"""qrencode: turn a file into pages of QR codes that a phone can scan back.

    python qrencode.py report.pdf                 # slideshow for photos
    python qrencode.py report.pdf --live          # made for scanning the slideshow live
    python qrencode.py report.pdf --color -p      # start in colour mode, password protected
    echo "secret note" | python qrencode.py - -p  # encode text from stdin

Nothing is written to disk: the slideshow is built in memory and served only to this
computer (127.0.0.1) for a private browser window. Press Ctrl+C when you are done.
The bar on top of the slideshow switches the layout (1, 2, 6 or 12 codes per page),
colour and speed while you scan, without losing progress.
Add -o FOLDER to save viewer.html (and --png page images) instead.
Decode with the web scanner (web/) or qrdecode.py. Format: FORMAT.md.

Requires: pip install segno pillow numpy cryptography zstandard
"""
import argparse
import base64
import getpass
import hashlib
import html
import http.server
import io
import math
import os
import secrets
import shutil
import struct
import subprocess
import sys
import threading
import time
import zlib
from dataclasses import dataclass, field
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


def _matrix(data: bytes, version: int, ecc: str) -> np.ndarray:
    """Boolean module matrix (True = dark), without quiet zone."""
    qr = segno.make(data, version=version, error=ecc, mode="byte", boost_error=False)
    return np.array(qr.matrix, dtype=bool)


def _modules(data: bytes, version: int, ecc: str) -> np.ndarray:
    """Boolean module matrix (True = dark) including the quiet zone."""
    return np.pad(_matrix(data, version, ecc), QUIET, constant_values=False)


def _render_slot(payloads: list[bytes], layout: Layout, module_px: int) -> Image.Image:
    layers = [_modules(p, layout.version, layout.ecc) for p in payloads]
    while layout.color and len(layers) < 3:  # partly filled last slot: missing layers stay light
        layers.append(np.zeros_like(layers[0]))
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
class Packets:
    """The encoded file as QR payloads, independent of how they are laid out on screen."""
    raw: list
    method: str
    original_size: int
    stream_size: int
    k: int
    file_id: bytes
    block_size: int
    version: int
    ecc: str

    @property
    def total(self) -> int:
        return len(self.raw)


def build_packets(data: bytes, name: str, version: int, ecc: str, redundancy: float = 0.3,
                  password: str | None = None, multiple: int = 1) -> Packets:
    """Compress, optionally encrypt, and cut into packets; the count is rounded up to a multiple of `multiple`."""
    stream, method = pack(data, name, password)
    file_id = hashlib.sha256(stream).digest()[:4]
    block_size = qr_capacity(version, ecc) - PACKET_OVERHEAD
    blocks = split(stream, block_size)
    k = len(blocks)
    # data packets first, then repair packets; round up so the last page is full (empty slots are wasted)
    total = math.ceil((k + max(2, math.ceil(k * redundancy))) / multiple) * multiple
    raw = [make_packet(file_id, len(stream), block_size, seq, encode_block(blocks, file_id, seq))
           for seq in range(total)]
    return Packets(raw, method, len(data), len(stream), k, file_id, block_size, version, ecc)


@dataclass
class EncodeResult:
    pages: list
    method: str
    original_size: int
    stream_size: int
    k: int
    packets: int
    file_id: bytes
    raw: list = field(default_factory=list)
    block_size: int = 0


def render_pages(pk: Packets, layout: Layout, encrypted: bool = False) -> list:
    qr_modules = 17 + 4 * layout.version + 2 * QUIET
    caption_h = int(layout.height * 0.045)
    cell_w = layout.width // layout.cols
    cell_h = (layout.height - caption_h) // layout.rows
    module_px = min(cell_w, cell_h) // qr_modules
    if module_px < 2:
        raise SystemExit("page too small for this grid / QR version: use fewer codes or a lower --qr-version")

    font = _font(max(12, int(caption_h * 0.6)))
    n_pages = math.ceil(pk.total / layout.per_page)
    per_slot = layout.per_page // layout.slots
    pages = []
    for p in range(n_pages):
        page = Image.new("RGB" if layout.color else "L", (layout.width, layout.height), "white")
        chunk = pk.raw[p * layout.per_page:(p + 1) * layout.per_page]
        for i in range(layout.slots):
            payloads = chunk[i * per_slot:(i + 1) * per_slot]
            if not payloads:
                break
            qr = _render_slot(payloads, layout, module_px)
            r, c = divmod(i, layout.cols)
            page.paste(qr, (c * cell_w + (cell_w - qr.width) // 2,
                            caption_h + r * cell_h + (cell_h - qr.height) // 2))
        lock = "  |  password" if encrypted else ""
        caption = f"page {p + 1}/{n_pages}  |  any {pk.k}+ codes rebuild the file{lock}  |  id {pk.file_id.hex()}"
        ImageDraw.Draw(page).text((layout.width // 2, caption_h // 2), caption, fill="black", font=font, anchor="mm")
        pages.append(page)
    return pages


def encode(data: bytes, name: str, layout: Layout, redundancy: float = 0.3,
           password: str | None = None) -> EncodeResult:
    """File -> rendered pages for one fixed layout."""
    pk = build_packets(data, name, layout.version, layout.ecc, redundancy, password, multiple=layout.per_page)
    pages = render_pages(pk, layout, password is not None)
    return EncodeResult(pages, pk.method, pk.original_size, pk.stream_size, pk.k, pk.total, pk.file_id,
                        pk.raw, pk.block_size)


# ---------------------------------------------------------------- slideshow

# Page sizes the slideshow offers: codes per slot-grid cell count. Every size and colour mode shows
# consecutive packets of the same stream, so switching mid-scan keeps whatever the phone already read.
SLIDESHOW_LAYOUTS = (1, 2, 6, 12)
PACKET_MULTIPLE = 36  # divisible by every page size above, times 3 for colour

VIEWER_TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
html,body{margin:0;height:100%;background:#fff;color:#111;font:14px system-ui,-apple-system,sans-serif;overflow:hidden}
#bar{position:fixed;inset:0 0 auto 0;height:48px;display:flex;align-items:center;gap:6px;padding:0 8px;background:#e9edf1;border-bottom:1px solid #c5ccd3;overflow-x:auto;white-space:nowrap}
#bar button,#bar select{height:34px;padding:0 10px;font:inherit;border:1px solid #9aa4ae;border-radius:6px;background:#fff;color:#111}
#bar button.on{background:#1b6ef3;color:#fff;border-color:#1b6ef3}
#speed{min-width:4.5em;text-align:center}
#info{margin-left:auto;padding-left:10px;color:#445}
canvas{position:fixed;left:0;top:48px;width:100vw;height:calc(100vh - 48px);image-rendering:pixelated;display:block}
body.hide #bar{display:none}
body.hide canvas{top:0;height:100vh}
</style></head><body>
<div id="bar">
 <select id="layout" aria-label="codes per page">
  <option value="1">1 code</option><option value="2">2 codes</option><option value="6">6 codes</option><option value="12">12 codes</option>
 </select>
 <button id="color" title="3 codes in every spot (red, green, blue): 3x the data per page, slower to decode">Color</button>
 <button id="slower" aria-label="slower">&minus;</button><span id="speed"></span><button id="faster" aria-label="faster">+</button>
 <button id="prev" aria-label="previous page">&#9664;</button><button id="play">Pause</button><button id="next" aria-label="next page">&#9654;</button>
 <button id="full">Full screen</button><button id="hide">Hide bar</button>
 <span id="info"></span>
</div>
<canvas id="c"></canvas>
<script>
(() => {
const V = __V__, N = __N__, BLOCK = __BLOCK__, K = __K__;
const S = 17 + 4 * V, Q = 4, T = S + 2 * Q, STRIDE = Math.ceil(S * S / 8);
const bin = atob("__DATA__"), M = new Uint8Array(bin.length);
for (let i = 0; i < bin.length; i++) M[i] = bin.charCodeAt(i);

const LAYOUTS = { 1: [1, 1], 2: [2, 1], 6: [3, 2], 12: [4, 3] };
const $ = (id) => document.getElementById(id);
const canvas = $("c"), ctx = canvas.getContext("2d");
const st = { layout: __LAYOUT__, color: __COLOR__, speed: __SPEED__, playing: true, cursor: 0, hide: false };

const q = new URLSearchParams(location.hash.slice(1));
if (LAYOUTS[q.get("layout")]) st.layout = +q.get("layout");
if (q.has("color")) st.color = q.get("color") === "1";
if (q.has("speed")) st.speed = Math.min(20, Math.max(0.25, +q.get("speed") || st.speed));
if (q.get("paused") === "1") st.playing = false;
if (q.has("at")) st.cursor = Math.max(0, parseInt(q.get("at")) || 0) % N;
if (q.get("hide") === "1") st.hide = true;

const dims = () => { const [c, r] = LAYOUTS[st.layout]; return innerHeight > innerWidth ? [r, c] : [c, r]; };
const perPage = () => Math.min(dims()[0] * dims()[1] * (st.color ? 3 : 1), N);

function matrix(p) {
  const out = new Uint8Array(S * S), base = p * STRIDE;
  for (let i = 0; i < out.length; i++) out[i] = (M[base + (i >> 3)] >> (7 - (i & 7))) & 1;
  return out;
}

function draw() {
  const dpr = devicePixelRatio || 1;
  const W = Math.round(canvas.clientWidth * dpr), H = Math.round(canvas.clientHeight * dpr);
  if (canvas.width !== W || canvas.height !== H) { canvas.width = W; canvas.height = H; }
  ctx.fillStyle = "#fff";
  ctx.fillRect(0, 0, W, H);
  const [cols, rows] = dims(), per = st.color ? 3 : 1, P = perPage();
  const slots = Math.ceil(P / per), cw = Math.floor(W / cols), ch = Math.floor(H / rows);
  const m = Math.max(1, Math.floor(Math.min(cw, ch) / T));
  for (let s = 0; s < slots; s++) {
    const x0 = (s % cols) * cw + Math.floor((cw - T * m) / 2) + Q * m;
    const y0 = Math.floor(s / cols) * ch + Math.floor((ch - T * m) / 2) + Q * m;
    const layers = [];
    for (let l = 0; l < per; l++) layers.push(matrix((st.cursor + s * per + l) % N));
    // a module's colour: channel l is 0 where layer l is dark, 255 where it is light (white = nothing to draw)
    const paths = Array.from({ length: 7 }, () => new Path2D());
    const COLORS = ["#000", "#00f", "#0f0", "#0ff", "#f00", "#f0f", "#ff0"];
    for (let i = 0, y = 0; y < S; y++) {
      for (let x = 0; x < S; x++, i++) {
        const d0 = layers[0][i], d1 = layers[1] ? layers[1][i] : 0, d2 = layers[2] ? layers[2][i] : 0;
        const c = st.color ? ((d0 ? 0 : 4) | (d1 ? 0 : 2) | (d2 ? 0 : 1)) : (d0 ? 0 : 7);
        if (c !== 7) paths[c].rect(x0 + x * m, y0 + y * m, m, m);
      }
    }
    for (let c = 0; c < 7; c++) { ctx.fillStyle = COLORS[c]; ctx.fill(paths[c]); }
  }
  ui();
}

function ui() {
  const P = perPage();
  $("layout").value = st.layout;
  $("color").classList.toggle("on", st.color);
  $("play").textContent = st.playing ? "Pause" : "Play";
  $("speed").textContent = st.speed.toFixed(st.speed < 10 ? 2 : 1).replace(/\.?0+$/, "") + " pg/s";
  $("info").textContent = P + " codes/page · " + (P * BLOCK / 1000).toFixed(1) + " KB/page · loop "
    + (Math.ceil(N / P) / st.speed).toFixed(1) + " s · needs " + K + " of " + N;
  document.body.classList.toggle("hide", st.hide);
  const p = new URLSearchParams({ layout: st.layout, color: +st.color, speed: st.speed });
  if (!st.playing) p.set("paused", "1");
  if (st.hide) p.set("hide", "1");
  try { history.replaceState(null, "", "#" + p); } catch (e) { /* file:// in some browsers */ }
}

let timer = null;
function schedule() {
  clearTimeout(timer);
  if (st.playing) timer = setTimeout(() => { step(1); schedule(); }, 1000 / st.speed);
}
function step(dir) {
  const P = perPage();
  st.cursor = ((st.cursor + dir * P) % N + N) % N;
  draw();
}
const change = (fn) => { fn(); draw(); schedule(); };
const setSpeed = (f) => change(() => { st.speed = Math.min(20, Math.max(0.25, Math.round(st.speed * f * 100) / 100)); });
const fullscreen = () => (document.fullscreenElement ? document.exitFullscreen() : document.documentElement.requestFullscreen?.());

$("layout").onchange = (e) => change(() => { st.layout = +e.target.value; });
$("color").onclick = () => change(() => { st.color = !st.color; });
$("slower").onclick = () => setSpeed(0.8);
$("faster").onclick = () => setSpeed(1.25);
$("prev").onclick = () => { step(-1); schedule(); };
$("next").onclick = () => { step(1); schedule(); };
$("play").onclick = () => change(() => { st.playing = !st.playing; });
$("full").onclick = fullscreen;
$("hide").onclick = () => change(() => { st.hide = true; });
canvas.onclick = () => change(() => { st.hide = !st.hide; });
addEventListener("resize", draw);
addEventListener("keydown", (e) => {
  const k = e.key;
  if (k === " ") change(() => { st.playing = !st.playing; });
  else if (k === "ArrowRight") { step(1); schedule(); }
  else if (k === "ArrowLeft") { step(-1); schedule(); }
  else if (k === "+" || k === "=") setSpeed(1.25);
  else if (k === "-") setSpeed(0.8);
  else if (k === "c") change(() => { st.color = !st.color; });
  else if (k === "h") change(() => { st.hide = !st.hide; });
  else if (k === "f") fullscreen();
  else if ("1234".includes(k)) change(() => { st.layout = [1, 2, 6, 12][+k - 1]; });
});

window.qrViewer = { st, draw, step, perPage, N, K, V };  // used by the tests
draw();
schedule();
})();
</script></body></html>
"""


def viewer_html(pk: Packets, layout: int = 6, color: bool = False, pages_per_s: float = 2.5,
                title: str = "qrtransfer slideshow") -> str:
    """One self-contained page that draws the codes itself, so layout, colour and speed switch live."""
    mats = b"".join(np.packbits(_matrix(p, pk.version, pk.ecc).ravel()).tobytes() for p in pk.raw)
    return (VIEWER_TEMPLATE
            .replace("__TITLE__", html.escape(title))
            .replace("__V__", str(pk.version))
            .replace("__N__", str(pk.total))
            .replace("__BLOCK__", str(pk.block_size))
            .replace("__K__", str(pk.k))
            .replace("__LAYOUT__", str(layout))
            .replace("__COLOR__", "true" if color else "false")
            .replace("__SPEED__", repr(round(pages_per_s, 3)))
            .replace("__DATA__", base64.b64encode(mats).decode()))


def layout_choice(cols: int, rows: int) -> int:
    """Nearest slideshow page size for a cols x rows grid."""
    n = cols * rows
    return 1 if n <= 1 else 2 if n <= 2 else 6 if n <= 6 else 12


# ---------------------------------------------------------------- memory-only serving


class MemoryServer:
    """Serves one page from memory on 127.0.0.1 only, at an unguessable path, and tells the browser not to store it."""

    def __init__(self, page: bytes):
        self.token = secrets.token_urlsafe(16)
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                host_ok = self.headers.get("Host", "") in outer.hosts  # blocks DNS-rebinding from web pages
                if self.path.split("?")[0] != "/" + outer.token or not host_ok:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(page)))
                self.send_header("Cache-Control", "no-store, max-age=0")
                self.send_header("Pragma", "no-cache")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Security-Policy",
                                 "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                                 "img-src data: blob:; connect-src 'none'; base-uri 'none'; form-action 'none'")
                self.end_headers()
                self.wfile.write(page)

            def log_message(self, *args):  # no access log
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        port = self.httpd.server_port
        self.hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        self.url = f"http://127.0.0.1:{port}/{self.token}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


_PRIVATE_BROWSERS = (
    (("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome", "brave-browser"), "--incognito"),
    (("microsoft-edge", "microsoft-edge-stable", "msedge"), "--inprivate"),
    (("firefox",), "--private-window"),
)
_BROWSER_PATHS = (
    ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "--incognito"),
    ("/Applications/Brave Browser.app/Contents/MacOS/Brave Browser", "--incognito"),
    ("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge", "--inprivate"),
    ("/Applications/Firefox.app/Contents/MacOS/firefox", "--private-window"),
    (r"C:\Program Files\Google\Chrome\Application\chrome.exe", "--incognito"),
    (r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe", "--incognito"),
    (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", "--inprivate"),
    (r"C:\Program Files\Mozilla Firefox\firefox.exe", "--private-window"),
)


def private_browser_command(url: str, which=shutil.which, exists=os.path.exists) -> list | None:
    """Command that opens `url` in a private / incognito window of an installed browser, if there is one."""
    for names, flag in _PRIVATE_BROWSERS:
        for name in names:
            if exe := which(name):
                return [exe, flag, url]
    for path, flag in _BROWSER_PATHS:
        if exists(path):
            return [path, flag, url]
    return None


def open_private(url: str) -> bool:
    cmd = private_browser_command(url)
    if cmd is None:
        return False
    subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    return True


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


def main(argv=None, on_ready=None, stop_event=None):
    p = argparse.ArgumentParser(description="Encode a file into a slideshow of QR codes for scanning with a phone. "
                                            "By default nothing is written to disk.")
    p.add_argument("file", help="file to encode, or - to read from stdin")
    p.add_argument("-o", "--out", help="save viewer.html to this folder instead of showing it from memory")
    p.add_argument("-p", "--password", action="store_true", help="encrypt; asks for a password")
    p.add_argument("--live", action="store_true", help="bigger codes for scanning the slideshow live (default: photo)")
    p.add_argument("--color", action="store_true", help="start the slideshow in colour mode (3x data per page)")
    p.add_argument("--name", help="file name stored in the codes (default: the input name)")
    p.add_argument("--cols", type=int, help="start with this many columns of codes (the slideshow can change it)")
    p.add_argument("--rows", type=int, help="start with this many rows of codes")
    p.add_argument("--qr-version", type=int, help="1-40; higher = denser codes, harder to scan (fixed for the whole file)")
    p.add_argument("--ecc", choices="LMQH", help="QR error-correction level")
    p.add_argument("--redundancy", type=float, default=0.3, help="extra repair codes, as a fraction of data codes")
    p.add_argument("--interval", type=int, help="start with this many milliseconds per page")
    p.add_argument("--no-browser", action="store_true", help="only print the address; open it in a private window yourself")
    p.add_argument("--timeout", type=float, default=30, help="stop serving after this many minutes (default 30)")
    p.add_argument("--png", action="store_true", help="with -o: also write the pages as images (page_NNNN.png)")
    p.add_argument("--width", type=int, default=1920, help="page width in pixels for --png")
    p.add_argument("--height", type=int, default=1080, help="page height in pixels for --png")
    a = p.parse_args(argv)
    if a.png and not a.out:
        p.error("--png writes files, so it needs -o FOLDER")

    if a.file == "-":
        data, name = sys.stdin.buffer.read(), a.name or "message.txt"
    else:
        src = Path(a.file)
        data, name = src.read_bytes(), a.name or src.name
    password = ask_password() if a.password else None

    preset = dict(PRESETS["live" if a.live else "photo"])
    for key, val in (("cols", a.cols), ("rows", a.rows), ("version", a.qr_version), ("ecc", a.ecc)):
        if val is not None:
            preset[key] = val
    layout = Layout(a.width, a.height, color=a.color, **preset)
    print("encoding in memory" + ("" if a.out else "; nothing is written to disk") + "...", file=sys.stderr)
    pk = build_packets(data, name, layout.version, layout.ecc, a.redundancy, password, multiple=PACKET_MULTIPLE)
    interval = a.interval or (400 if a.live else 1500)
    page = viewer_html(pk, layout_choice(layout.cols, layout.rows), a.color, 1000 / interval, f"qrtransfer: {name}")
    enc = ", encrypted" if password else ""
    print(f"{name}: {pk.original_size} B -> {pk.stream_size} B ({pk.method}{enc})")
    print(f"{pk.k} data codes + {pk.total - pk.k} repair codes of {pk.block_size} B; "
          f"any {pk.k} (+ a couple) rebuild the file")

    if a.out:  # explicit opt-in to leave files behind
        out = Path(a.out)
        out.mkdir(parents=True, exist_ok=True)
        (out / "viewer.html").write_text(page)
        if a.png:
            for old in out.glob("page_*.png"):
                old.unlink()
            for i, img in enumerate(render_pages(pk, layout, password is not None), 1):
                img.save(out / f"page_{i:04d}.png", optimize=True)
        print(f"saved to {out}/ (this leaves the encoded file on disk); open viewer.html full screen and scan it")
        return

    # memory-only: drop everything but the page itself, then serve it to this computer only
    body = page.encode()
    del data, pk, page, password
    server = MemoryServer(body)
    try:
        print(f"\nslideshow: {server.url}   (reachable from this computer only)")
        if a.no_browser:
            print("open that address in a private / incognito window")
        elif open_private(server.url):
            print("opened in a private window")
        else:
            print("no browser with a private mode found: open the address in a private / incognito window yourself")
        print(f"press Full screen there and scan it. Close the window and press Ctrl+C here when done "
              f"(it stops by itself after {a.timeout:g} min)")
        if on_ready:
            on_ready(server.url)
        deadline = time.monotonic() + a.timeout * 60
        try:
            while time.monotonic() < deadline and not (stop_event and stop_event.is_set()):
                time.sleep(0.2)
        except KeyboardInterrupt:
            pass
    finally:
        server.close()
    print("stopped; nothing was written to disk")


if __name__ == "__main__":
    main()
