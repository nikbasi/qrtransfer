"""End to end in a real browser: qrencode.py pages -> simulated camera -> web/ -> original file."""
import functools
import hashlib
import http.server
import json
import random
import shutil
import subprocess
import threading
from pathlib import Path

import pytest
from PIL import Image

import qrencode
from simulate import PHOTO, VIDEO, fake_shot

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


@pytest.fixture(scope="module")
def server():
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(ROOT / "web"))
    handler.log_message = lambda *a: None
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}/"
    httpd.shutdown()


def browser(args) -> dict:
    r = subprocess.run(["node", str(ROOT / "tests/js/browser_scan.mjs"), json.dumps(args)],
                       capture_output=True, text=True, timeout=300)
    assert r.stdout.strip(), r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def write_mjpeg(path: Path, pages, frames_per_page=6, loops=2):
    """Fake camera feed: the slideshow filmed at 1080p, each page held for a few frames."""
    with open(path, "wb") as f:
        seed = 0
        for _ in range(loops):
            for page in pages:
                for _ in range(frames_per_page):
                    Image.fromarray(fake_shot(page, seed, size=VIDEO)).save(f, "JPEG", quality=85)
                    seed += 1


def sample(n, seed):
    rng = random.Random(seed)
    words = ["photo", "screen", "fountain", "code", "camera", "phone", "page", "block", "data", "file"]
    return " ".join(rng.choice(words) + str(rng.randint(0, 999)) for _ in range(n)).encode()


@pytest.mark.parametrize("color,password", [(False, None), (True, "s3cret pass")], ids=["bw", "color+password"])
def test_live_camera_scan(tmp_path, server, color, password):
    data = sample(2500, 1)
    layout = qrencode.Layout(color=color, **qrencode.PRESETS["live"])
    res = qrencode.encode(data, "notes.txt", layout, password=password)
    assert len(res.pages) >= 2
    video = tmp_path / "cam.mjpeg"
    write_mjpeg(video, res.pages)
    out = browser({"url": server, "video": str(video), "password": password})
    assert out.get("error") is None, out
    assert out["name"] == "notes.txt"
    assert out["sha256"] == hashlib.sha256(data).hexdigest()


def test_photo_upload_color(tmp_path, server):
    data = random.Random(4).randbytes(20_000)
    res = qrencode.encode(data, "blob.bin", qrencode.Layout(color=True), password="pw")
    photos = []
    for i, page in enumerate(res.pages):
        p = tmp_path / f"photo{i}.jpg"
        Image.fromarray(fake_shot(page, 100 + i, size=PHOTO)).save(p, quality=85)
        photos.append(str(p))
    out = browser({"url": server, "photos": photos, "password": "pw"})
    assert out.get("error") is None, out
    assert out["sha256"] == hashlib.sha256(data).hexdigest()


def test_wrong_password_then_right(tmp_path, server):
    data = b"top secret " * 50
    res = qrencode.encode(data, "s.txt", qrencode.Layout(), password="right")
    p = tmp_path / "p.jpg"
    Image.fromarray(fake_shot(res.pages[0], 7)).save(p, quality=85)
    out = browser({"url": server, "photos": [str(p)], "password": "wrong", "timeoutMs": 20000})
    assert "password rejected" in out["error"]  # the dialog came back after a wrong password


def test_camera_picker_switches_and_keeps_scanning(tmp_path, server):
    data = random.Random(11).randbytes(6000)  # incompressible, so scanning outlasts the picker check
    res = qrencode.encode(data, "cam.bin", qrencode.Layout(**qrencode.PRESETS["live"]))
    video = tmp_path / "cam.mjpeg"
    write_mjpeg(video, res.pages)
    out = browser({"url": server, "video": str(video), "switchCamera": True})
    assert out.get("error") is None, out
    assert out["cameras"] == ["Back Camera", "Back Ultra Wide Camera"]
    assert out["openedDevices"] == [None, "back-ultra"]  # default back camera first, then the one picked
    assert out["sha256"] == hashlib.sha256(data).hexdigest()
