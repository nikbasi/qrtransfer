"""The slideshow (viewer.html) draws QR codes in the browser: every layout must show exactly the packets it claims."""
import json
import random
import shutil
import subprocess

import numpy as np
import pytest
from PIL import Image

import qrdecode
import qrencode
from qrencode import build_packets, viewer_html

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent


def render(tmp_path, html, hashes, width=1920, height=1080):
    f = tmp_path / "viewer.html"
    f.write_text(html)
    out = tmp_path / "shots"
    out.mkdir(exist_ok=True)
    args = {"file": str(f), "outDir": str(out), "width": width, "height": height, "shots": [{"hash": h} for h in hashes]}
    r = subprocess.run(["node", str(ROOT / "tests/js/render_viewer.mjs"), json.dumps(args)], capture_output=True, text=True)
    assert r.stdout.strip(), r.stderr
    res = json.loads(r.stdout.strip().splitlines()[-1])
    assert res["errors"] == [], res["errors"]
    return [np.asarray(Image.open(out / f"shot_{i}.png").convert("RGB")) for i in range(res["n"])], res["info"]


def seqs_on_screen(img):
    seqs = set()
    for raw in qrdecode.read_codes(img):
        _, _, _, seq, _ = qrdecode.parse_packet(raw)
        seqs.add(seq)
    return seqs


@pytest.fixture(scope="module")
def pk():
    data = random.Random(8).randbytes(14_000)
    return build_packets(data, "v.bin", 15, "M", multiple=qrencode.PACKET_MULTIPLE)


@pytest.mark.parametrize("color", [False, True], ids=["bw", "color"])
@pytest.mark.parametrize("layout", qrencode.SLIDESHOW_LAYOUTS)
def test_every_layout_shows_the_right_packets(tmp_path, pk, layout, color):
    html = viewer_html(pk, layout=6, color=False)
    at = 7 * 36 % pk.total
    imgs, info = render(tmp_path, html, [f"layout={layout}&color={int(color)}&paused=1&hide=1&at={at}"])
    P = info[0]["P"]
    assert P == layout * (3 if color else 1)
    expected = {(at + i) % pk.total for i in range(P)}
    assert seqs_on_screen(imgs[0]) == expected


def test_portrait_screen_swaps_the_grid(tmp_path, pk):
    imgs, info = render(tmp_path, viewer_html(pk), ["layout=6&color=0&paused=1&hide=1&at=0"], width=540, height=960)
    assert seqs_on_screen(imgs[0]) == set(range(info[0]["P"]))


def test_pool_is_a_multiple_of_every_page_size(pk):
    assert pk.total % qrencode.PACKET_MULTIPLE == 0
    assert all(qrencode.PACKET_MULTIPLE % (n * c) == 0 for n in qrencode.SLIDESHOW_LAYOUTS for c in (1, 3))


@pytest.mark.parametrize("color", [False, True], ids=["bw", "color"])
def test_slideshow_filmed_by_phone_is_read_by_scanner(tmp_path, color):
    """viewer.html -> screenshots -> simulated 1080p camera video -> the real scanner page -> original file."""
    import hashlib

    from PIL import Image
    from simulate import VIDEO, fake_shot
    from test_browser import browser, serve

    data = random.Random(21).randbytes(9000)  # incompressible
    pk = build_packets(data, "slide.bin", 15, "M", multiple=qrencode.PACKET_MULTIPLE)
    per_page = 6 * (3 if color else 1)
    hashes = [f"layout=6&color={int(color)}&paused=1&hide=1&at={i * per_page}" for i in range(pk.total // per_page)]
    imgs, _ = render(tmp_path, viewer_html(pk), hashes)
    video = tmp_path / "cam.mjpeg"
    with open(video, "wb") as f:
        for loop in range(2):
            for i, img in enumerate(imgs):
                for j in range(8):  # ~0.25 s of video per page
                    Image.fromarray(fake_shot(Image.fromarray(img), loop * 1000 + i * 10 + j, size=VIDEO)).save(f, "JPEG", quality=85)
    with serve() as url:
        out = browser({"url": url, "video": str(video)})
    assert out.get("error") is None, out
    assert out["sha256"] == hashlib.sha256(data).hexdigest()
