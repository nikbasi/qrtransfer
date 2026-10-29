"""The encoder must not leave anything behind: no files written, page only served to this computer."""
import http.client
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import urllib.request
from pathlib import Path

import pytest

import qrencode

ROOT = Path(__file__).resolve().parent.parent
WRITE_FLAGS = ("O_WRONLY", "O_RDWR", "O_CREAT", "O_APPEND", "O_TRUNC", "O_TMPFILE")
# terminals, null device and process info are not files on disk
HARMLESS = ("/dev/", "/proc/")


def tree(*roots):
    return sorted(str(p) for r in roots for p in Path(r).rglob("*"))


@pytest.mark.skipif(shutil.which("strace") is None or shutil.which("node") is None, reason="needs strace and node")
def test_nothing_is_written_while_encoding_and_serving(tmp_path):
    work, home, tmp = tmp_path / "work", tmp_path / "home", tmp_path / "tmp"
    for d in (work, home, tmp):
        d.mkdir()
    secret = work / "secret.bin"
    secret.write_bytes(os.urandom(5000))
    before = tree(work, home, tmp)

    env = {**os.environ, "HOME": str(home), "TMPDIR": str(tmp), "PYTHONDONTWRITEBYTECODE": "1"}
    log = tmp_path / "strace.log"  # outside work/home/tmp so it does not count itself
    trace = "openat,open,creat,mkdir,mkdirat,rename,renameat,renameat2,unlink,unlinkat,truncate,link,symlink,memfd_create"
    cmd = ["strace", "-f", "-qq", "-e", f"trace={trace}", "-o", str(log),
           sys.executable, "-B", str(ROOT / "qrencode.py"), str(secret), "--live", "-p", "--no-browser", "--timeout", "0.4"]
    proc = subprocess.Popen(cmd, cwd=work, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    proc.stdin.write("hunter2\nhunter2\n")  # password and confirmation (no terminal in the test)
    proc.stdin.flush()
    url = None
    for line in proc.stdout:
        if m := re.search(r"slideshow: (http://\S+)", line):
            url = m.group(1)
            break
    assert url, proc.stderr.read()

    # served to this computer with no-store, and it really works in a browser
    r = subprocess.run(["node", str(ROOT / "tests/js/check_url.mjs"), url], capture_output=True, text=True, timeout=120)
    page = json.loads(r.stdout.strip().splitlines()[-1])
    assert page["status"] == 200 and page["errors"] == [], page
    assert "no-store" in page["cache"]
    assert "codes/page" in page["info"]

    proc.wait(timeout=60)  # --timeout ends it by itself
    assert "nothing was written to disk" in proc.stdout.read()

    writes = []
    for line in log.read_text().splitlines():
        if any(f in line for f in WRITE_FLAGS) or re.search(r"\b(mkdir|mkdirat|rename\w*|unlink\w*|truncate|link|symlink|memfd_create)\(", line):
            m = re.search(r'"([^"]*)"', line)
            if m and m.group(1).startswith(HARMLESS):
                continue
            if "ENOENT" in line or "EEXIST" in line:  # probes that failed
                continue
            writes.append(line)
    assert writes == [], "files touched for writing:\n" + "\n".join(writes[:10])
    assert tree(work, home, tmp) == before


def test_other_computers_and_web_pages_cannot_fetch_it():
    srv = qrencode.MemoryServer(b"<html>secret</html>")
    try:
        host, port = "127.0.0.1", srv.httpd.server_port
        path = "/" + srv.token

        def get(path, host_header):
            c = http.client.HTTPConnection(host, port, timeout=5)
            c.putrequest("GET", path, skip_host=True)
            c.putheader("Host", host_header)
            c.endheaders()
            r = c.getresponse()
            return r.status, r.read()

        assert get(path, f"127.0.0.1:{port}") == (200, b"<html>secret</html>")
        assert get(path, f"localhost:{port}")[0] == 200
        assert get(path, "evil.example.com")[0] == 404          # DNS rebinding from a web page
        assert get("/", f"127.0.0.1:{port}")[0] == 404          # no token, no page
        assert get("/wrong-token", f"127.0.0.1:{port}")[0] == 404
        assert srv.httpd.server_address[0] == "127.0.0.1"       # not reachable from other machines
    finally:
        srv.close()


def test_saving_to_disk_is_explicit(tmp_path, monkeypatch):
    src = tmp_path / "f.txt"
    src.write_text("hello " * 50)
    qrencode.main([str(src), "-o", str(tmp_path / "out")])
    assert (tmp_path / "out" / "viewer.html").exists()
    with pytest.raises(SystemExit):  # --png writes files, so it must say where
        qrencode.main([str(src), "--png"])


def test_private_window_command():
    which = lambda name: f"/usr/bin/{name}" if name in ("firefox", "chromium") else None
    assert qrencode.private_browser_command("http://x", which, lambda p: False) == ["/usr/bin/chromium", "--incognito", "http://x"]
    assert qrencode.private_browser_command("http://x", lambda n: "/usr/bin/firefox" if n == "firefox" else None,
                                            lambda p: False) == ["/usr/bin/firefox", "--private-window", "http://x"]
    mac = lambda p: p.endswith("Microsoft Edge")
    assert qrencode.private_browser_command("http://x", lambda n: None, mac)[1] == "--inprivate"
    assert qrencode.private_browser_command("http://x", lambda n: None, lambda p: False) is None
