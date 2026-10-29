"""The web scanner's format.js must decode exactly what qrencode.py produces."""
import hashlib
import json
import random
import shutil
import subprocess
from pathlib import Path

import pytest

import qrencode

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def packets_for(data, name, password, block_size=300):
    stream, method = qrencode.pack(data, name, password)
    fid = hashlib.sha256(stream).digest()[:4]
    blocks = qrencode.split(stream, block_size)
    k = len(blocks)
    pkts = [qrencode.make_packet(fid, len(stream), block_size, s, qrencode.encode_block(blocks, fid, s))
            for s in range(int(k * 1.6) + 4)]
    return pkts, method, k


def run_node(tmp_path, packets, password):
    f = tmp_path / "in.json"
    f.write_text(json.dumps({"packets": [p.hex() for p in packets], "password": password}))
    r = subprocess.run(["node", str(ROOT / "tests/js/decode_packets.mjs"), str(f)], capture_output=True, text=True)
    return json.loads(r.stdout.strip().splitlines()[-1]) if r.stdout.strip() else {"error": r.stderr}


@pytest.mark.parametrize("password", [None, "pässwörd"])
@pytest.mark.parametrize("kind", ["text", "random"])
def test_node_decodes_python_packets(tmp_path, password, kind):
    rng = random.Random(9)
    data = (" ".join(rng.choice(["alpha", "beta", "gamma", "delta"]) for _ in range(8000)).encode()
            if kind == "text" else rng.randbytes(9000))
    pkts, method, k = packets_for(data, "file ü.txt", password)
    rng.shuffle(pkts)
    pkts = pkts[len(pkts) // 4:]  # lose a quarter of them, in random order
    out = run_node(tmp_path, pkts, password)
    assert out.get("error") is None, out
    assert out["name"] == "file ü.txt"
    assert out["sha256"] == hashlib.sha256(data).hexdigest()


def test_node_deflate_path(tmp_path, monkeypatch):
    monkeypatch.setattr(qrencode, "zstandard", None)  # forces deflate
    data = b"deflate me " * 2000
    pkts, method, _ = packets_for(data, "d.txt", None)
    assert method == "deflate"
    assert run_node(tmp_path, pkts, None)["sha256"] == hashlib.sha256(data).hexdigest()


def test_node_wrong_password(tmp_path):
    pkts, _, _ = packets_for(b"secret" * 100, "s.txt", "right")
    assert "WrongPassword" in run_node(tmp_path, pkts, "wrong")["error"]
