import os
import random

import pytest

import qrdecode
import qrencode
from qrencode import Layout, encode
from simulate import HEAVY_CROSSTALK, VIDEO, fake_shot


def text(n=30_000, seed=1):
    rng = random.Random(seed)
    words = [rng.randbytes(rng.randint(2, 9)).hex() for _ in range(2000)]
    return " ".join(rng.choice(words) for _ in range(n // 8)).encode()[:n]


@pytest.mark.parametrize("password", [None, "correct horse"])
def test_container_roundtrip(password):
    data = text()
    stream, _ = qrencode.pack(data, "a.txt", password)
    assert len(stream) < len(data) * 0.8
    assert qrdecode.unpack(stream, password) == (data, "a.txt")


def test_wrong_password_and_tamper_are_rejected():
    stream, _ = qrencode.pack(b"hello" * 100, "x", "pw")
    with pytest.raises(qrdecode.WrongPassword):
        qrdecode.unpack(stream, "nope")
    bad = bytearray(stream)
    bad[-1] ^= 1
    with pytest.raises(qrdecode.WrongPassword):  # GCM tag fails: same error as a wrong password
        qrdecode.unpack(bytes(bad), "pw")


def test_unencrypted_corruption_detected():
    stream, _ = qrencode.pack(os.urandom(1000), "x")
    bad = bytearray(stream)
    bad[-1] ^= 1
    with pytest.raises(Exception):
        qrdecode.unpack(bytes(bad))


def test_prng_matches_reference_values():
    # Same numbers as web/js/format.js; if this changes, the web scanner breaks.
    rng = qrencode._mulberry32(0x12345678)
    assert [next(rng) for _ in range(3)] == [455919406, 4042750857, 4036713555]


@pytest.mark.parametrize("loss", [0.0, 0.1, 0.25])
def test_fountain_recovers_from_any_loss(loss):
    rng = random.Random(42)
    stream = os.urandom(20_000)
    fid, bs = b"abcd", 500
    blocks = qrencode.split(stream, bs)
    k = len(blocks)
    seqs = [s for s in range(int(k * 1.6)) if rng.random() >= loss]
    rng.shuffle(seqs)
    dec = qrdecode.FountainDecoder(fid, len(stream), bs)
    for s in seqs:
        dec.add(s, qrencode.encode_block(blocks, fid, s))
        if dec.complete:
            break
    assert dec.complete
    assert dec.stream() == stream


def decode_pages(pages, shot=fake_shot, skip=(), min_per_page=None):
    asm = qrdecode.Assembler()
    for i, page in enumerate(pages):
        if i in skip:
            continue
        codes = qrdecode.read_codes(shot(page, i))
        if min_per_page is not None:
            assert len(codes) >= min_per_page, f"page {i}: only {len(codes)} codes read"
        asm.feed(codes)
    assert asm.finished(), asm.status()
    [dec] = asm.decoders.values()
    return dec.stream()


@pytest.mark.parametrize("color", [False, True], ids=["bw", "color"])
def test_photos_end_to_end_with_missing_page(color):
    layout = Layout(color=color)
    data = random.Random(7).randbytes(15_000 * (3 if color else 1))  # incompressible: several pages
    res = encode(data, "doc.bin", layout, redundancy=0.5, password="pw")
    assert len(res.pages) >= 4
    # each run encrypts with a fresh random salt, so the code patterns differ; a marginal code may go unread
    stream = decode_pages(res.pages, skip={1}, min_per_page=layout.per_page // 2)
    assert qrdecode.unpack(stream, "pw") == (data, "doc.bin")


def test_color_survives_heavy_crosstalk():
    layout = Layout(color=True)
    res = encode(random.Random(3).randbytes(10_000), "x", layout)
    img = fake_shot(res.pages[0], 0, crosstalk=HEAVY_CROSSTALK)
    assert len(qrdecode.read_codes(img)) == layout.per_page


@pytest.mark.parametrize("color", [False, True], ids=["bw", "color"])
def test_live_preset_reads_from_1080p_frames(color):
    layout = Layout(color=color, **qrencode.PRESETS["live"])
    res = encode(random.Random(5).randbytes(4000), "x", layout)
    for i, page in enumerate(res.pages[:2]):
        assert len(qrdecode.read_codes(fake_shot(page, i, size=VIDEO))) == layout.per_page
