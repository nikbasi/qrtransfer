import argparse
import sys
from pathlib import Path

from .decode import decode_inputs
from .encode import Layout, encode, write_viewer


def cmd_encode(a):
    src = Path(a.file)
    layout = Layout(a.width, a.height, a.cols, a.rows, a.qr_version, a.ecc)
    res = encode(src.read_bytes(), src.name, layout, a.redundancy)
    out = Path(a.out or f"{src.stem}_qr")
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("page_*.png"):
        old.unlink()
    for i, page in enumerate(res.pages, 1):
        page.save(out / f"page_{i:04d}.png", optimize=True)
    viewer = write_viewer(out, len(res.pages), a.interval)
    ratio = res.stream_size / max(1, res.original_size)
    print(f"{src.name}: {res.original_size} B -> {res.stream_size} B with {res.method} ({ratio:.1%})")
    print(f"{res.k} data codes + {res.packets - res.k} repair codes on {len(res.pages)} page(s), "
          f"{layout.per_page} codes/page; any {res.k} distinct codes (+ a couple) rebuild the file")
    print(f"pages in {out}/, slideshow: {viewer}")


def cmd_decode(a):
    asm = decode_inputs([Path(p) for p in a.inputs])
    if asm.rejected:
        print(f"ignored {asm.rejected} code(s) that were foreign or failed the CRC check")
    if not asm.finished():
        print(f"incomplete: {asm.status()}. Take more photos (any pages) and run again with all images.")
        sys.exit(1)
    out_dir = Path(a.out or ".")
    out_dir.mkdir(parents=True, exist_ok=True)
    for fid, data, name in asm.results():
        dest = out_dir / Path(name).name
        dest.write_bytes(data)
        print(f"OK  {dest}  ({len(data)} B, SHA-256 verified)")


def main(argv=None):
    p = argparse.ArgumentParser(prog="qrtransfer", description="Send files through photos of QR-code pages.")
    sub = p.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("encode", help="file -> PNG pages of QR codes")
    e.add_argument("file")
    e.add_argument("-o", "--out", help="output folder (default: <name>_qr)")
    e.add_argument("--width", type=int, default=1920)
    e.add_argument("--height", type=int, default=1080)
    e.add_argument("--cols", type=int, default=3)
    e.add_argument("--rows", type=int, default=2)
    e.add_argument("--qr-version", type=int, default=20, help="1-40; higher = denser codes, harder to photograph")
    e.add_argument("--ecc", default="M", choices="LMQH", help="QR error-correction level")
    e.add_argument("--redundancy", type=float, default=0.15, help="extra repair codes as a fraction of data codes")
    e.add_argument("--interval", type=int, default=600, help="slideshow ms per page")
    e.set_defaults(func=cmd_encode)

    d = sub.add_parser("decode", help="photos / videos -> original file")
    d.add_argument("inputs", nargs="+", help="images or videos, in any order")
    d.add_argument("-o", "--out", help="output folder (default: current)")
    d.set_defaults(func=cmd_decode)

    a = p.parse_args(argv)
    a.func(a)
