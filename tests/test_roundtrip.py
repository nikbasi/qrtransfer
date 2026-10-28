import io
import os
import random

import cv2
import numpy as np
import pytest
from PIL import Image

from qrtransfer import container, fountain
from qrtransfer.decode import Assembler, read_codes
from qrtransfer.encode import Layout, encode


def sample_text(n=30_000, seed=1):
    rng = random.Random(seed)
    words = ("the data is sent through a photo of the screen and every code carries part of the file "
             "with error correction so that missing or blurry codes can be rebuilt from the others").split()
    return " ".join(rng.choice(words) for _ in range(n // 5)).encode()[:n]


def fake_photo(page: Image.Image, seed: int) -> np.ndarray:
    """Page shown on a screen, shot by a 12 MP phone: tilt, blur, uneven light, noise, JPEG."""
    rng = np.random.default_rng(seed)
    src = np.asarray(page.convert("L"), dtype=np.float32)
    h, w = src.shape
    W, H = 4032, 3024
    sx, sy = W * 0.08, H * 0.12
    jitter = lambda: rng.uniform(-0.05, 0.05) * W
    dst = np.float32([[sx + jitter(), sy + jitter()], [W - sx + jitter(), sy + jitter()],
                      [W - sx + jitter(), H - sy + jitter()], [sx + jitter(), H - sy + jitter()]])
    M = cv2.getPerspectiveTransform(np.float32([[0, 0], [w, 0], [w, h], [0, h]]), dst)
    shot = cv2.warpPerspective(src * 0.75 + 30, M, (W, H), borderValue=15)  # screen is never pure black/white
    shot = cv2.GaussianBlur(shot, (0, 0), 2.0)
    yy, xx = np.mgrid[0:H, 0:W]
    shot *= 0.7 + 0.3 * (xx / W)  # light falloff across the screen
    shot += rng.normal(0, 6, shot.shape)
    buf = io.BytesIO()
    Image.fromarray(np.clip(shot, 0, 255).astype(np.uint8)).save(buf, "JPEG", quality=75)
    return np.asarray(Image.open(buf))


def test_container_roundtrip():
    data = sample_text()
    blob, method = container.pack(data, "a.txt")
    assert len(blob) < len(data) / 3
    assert container.unpack(blob) == (data, "a.txt")


def test_container_detects_corruption():
    blob, _ = container.pack(os.urandom(1000), "x")
    bad = bytearray(blob)
    bad[-1] ^= 1
    with pytest.raises(Exception):
        container.unpack(bytes(bad))


@pytest.mark.parametrize("loss", [0.0, 0.1, 0.2])
def test_fountain_recovers_from_any_loss(loss):
    rng = random.Random(42)
    stream = os.urandom(20_000)
    fid, bs = b"abcd", 500
    blocks = fountain.split(stream, bs)
    k = len(blocks)
    seqs = [s for s in range(int(k * 1.4)) if rng.random() >= loss]
    rng.shuffle(seqs)
    dec = fountain.Decoder(fid, k, bs)
    for s in seqs:
        dec.add(s, fountain.encode_block(blocks, fid, s))
        if dec.complete:
            break
    assert dec.complete
    assert dec.result()[:len(stream)] == stream


def test_photos_end_to_end_with_missing_page():
    data = random.Random(7).randbytes(15_000)  # incompressible, so it spans several pages
    res = encode(data, "doc.bin", Layout(), redundancy=0.5)
    assert len(res.pages) >= 4
    skip = 1  # never photograph page 2; the repair codes on other pages cover it
    asm = Assembler()
    for i, page in enumerate(res.pages):
        if i == skip:
            continue
        codes = read_codes(fake_photo(page, i))
        assert len(codes) >= Layout().per_page - 1, f"page {i}: only {len(codes)} codes read"
        asm.feed(codes)
    assert asm.finished(), asm.status()
    [(_, out, name)] = list(asm.results())
    assert (out, name) == (data, "doc.bin")
