# qrtransfer

Send a file from a computer to a phone using only the screen and the camera.
`qrencode.py` turns the file into a slideshow of QR codes. Open the web
scanner on the phone and point it at the screen; once enough codes are read,
you get the original file back, bit for bit.

- **Encoder:** `qrencode.py`, a single Python script (installable on its own, see below). It can optionally
  encrypt the file with a password (AES-256-GCM).
- **Scanner:** `web/`, a static website (e.g. scan.basiletti.com). Everything
  is decoded on the phone and nothing is uploaded. It works offline once
  opened.
- **Desktop decoder:** `qrdecode.py`. Decodes photos or videos on a computer;
  the tests also use it as a reference.

## Encode

Install only the encoder (5 small packages; nothing from the website, the desktop decoder or the tests):

```
pip install "git+https://github.com/nikbasi/qrtransfer"     # or: pipx install "git+https://..."
# if the repo is private: git clone https://github.com/nikbasi/qrtransfer && pip install ./qrtransfer
```

This gives you a `qrtransfer` command:

```
qrtransfer notes.txt --live              # for live scanning (recommended)
qrtransfer report.pdf --live -p          # same, asks for a password
qrtransfer report.pdf --color            # dense colour pages, for taking photos
echo "wifi: hunter2" | qrtransfer - -p   # text from stdin
```

This writes `<name>_qr/viewer.html`, a single self-contained file. Open it,
click to go full screen, and scan. In the slideshow: space pauses, the arrow
keys step through pages, and `+`/`-` change the speed.

`qrencode.py` is also a single self-contained script: copy it anywhere, run
`pip install -r requirements.txt`, and use `python qrencode.py ...` instead.

## Scan

Open the scanner on the phone and tap **Start camera**. Hold the phone so the
slideshow fills most of the picture. Phones with several back cameras get a
**Camera** menu (main, ultra-wide, telephoto), and a **Zoom** slider if the
camera supports it. The main camera is usually the best for QR codes; the
choice is remembered. The bar fills as codes come in; any
pages, in any order, and missed frames don't matter. When it's complete,
enter the password if there is one, then **Save**, **Share** or **Copy text**.

There is also **Scan photos**, for pictures taken earlier.

### Which mode

| Mode | Per frame/photo | Scan speed (desktop CPU) | Use for |
|---|---|---|---|
| `--live` (6 codes per page) | ~2.3 KB | ~10 frames/s → ~20 KB/s | live scanning: fastest overall |
| `--live --color` | ~6.8 KB | ~1 frame/s → ~7 KB/s | when the screen is small or far away |
| default (photo) | ~3.8 KB | – | photos |
| `--color` (photo) | ~11 KB | 5–10 s per photo | photos: fewest shots |

Phones are slower than this desktop benchmark; expect roughly a third of the
speed. In colour mode each slot holds three QR codes, one per RGB channel.
The scanner corrects for white balance, lighting and the camera's colour
crosstalk, so no calibration is needed. Turn off night mode / blue-light
filters on the displaying screen.

## How it works

Compression (zstd or deflate) → optional encryption → a fountain code that
splits the result into blocks plus repair blocks, so any ~k+2 of the codes
rebuild the file → each block in a QR code with a CRC-32. The file's SHA-256
(or the GCM tag) is checked at the end. The full spec is in
[FORMAT.md](FORMAT.md).

## Deploy the scanner (scan.basiletti.com)

The camera only works over HTTPS. On the Oracle Cloud VM:

1. **DNS:** add an `A` record `scan` → the VM's public IP.
2. **nginx:** copy `deploy/nginx-scan.conf` into the sites folder, then run
   `sudo nginx -t && sudo systemctl reload nginx`. Ports 80 and 443 are
   already open if basiletti.com is served from this machine.
3. **HTTPS:** `sudo certbot --nginx -d scan.basiletti.com`
4. **Files:** `sudo mkdir -p /var/www/scan && sudo chown $USER /var/www/scan`,
   then from your computer run `deploy/deploy.sh ubuntu@<vm-ip>`.

To update later, run `deploy/deploy.sh` again. It stamps the service worker
so phones pick up the new version. On iPhone or Android, "Add to Home
Screen" installs the scanner like an app.

## Tests

```
pip install -e ".[dev]"      # or: pip install -r requirements-dev.txt
pytest
```

The tests cover:
- round trips through simulated phone photos and 1080p video frames (tilt,
  blur, colour crosstalk, uneven light, noise, JPEG);
- the JS decoder against packets made by the Python encoder;
- the real web page in headless Chromium, using a fake camera playing a
  simulated recording of the slideshow (needs Node and Playwright).

Third-party code bundled in `web/vendor/`:
- zxing-wasm 3.1.6 (zxing-cpp, Apache-2.0)
- fzstd 0.1.1 (MIT)
