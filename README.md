# qrtransfer

Send a file through photos of a screen. The file is compressed, cut into
blocks, protected with a fountain code and drawn as pages of QR codes.
Photograph the pages (or film a slideshow of them) and decode the photos to
get the exact original file back.

```
pip install -r requirements.txt
python -m qrtransfer encode report.pdf           # -> report_qr/page_*.png + viewer.html
python -m qrtransfer decode IMG_*.jpg            # photos in any order
python -m qrtransfer decode clip.mp4             # or a video of viewer.html
```

## How it works

| Layer | What it does |
|---|---|
| `container.py` | Tries zlib, bz2, lzma and zstd and keeps the smallest. Stores the name, the size and a **SHA-256** of the original file |
| `fountain.py` | Systematic random-linear fountain code over GF(2). Packets `0..k-1` are the data blocks; later packets are random XORs of the blocks. **Any ~k+2 distinct codes rebuild the file**, so missing or blurry codes don't matter as long as enough others were read |
| `packet.py` | Each QR carries a 19-byte header (file id, length, block size, sequence number) plus a **CRC-32** |
| `encode.py` | Lays out a grid of QR codes per page (default: 6 × version 20-M on 1920×1080). Writes `viewer.html`, a fullscreen slideshow |
| `decode.py` | Uses zxing-cpp to find every QR code in each photo or video frame, then solves for the blocks |

Integrity is checked at three levels: the Reed-Solomon code inside each QR,
the CRC-32 on each packet, and the SHA-256 of the whole file. A decoded file
is either bit-exact or rejected.

## Tuning

`--qr-version`, `--ecc`, `--cols` and `--rows` trade density against how easy
the codes are to photograph. In simulated 12 MP photos (perspective, blur,
uneven light, noise, JPEG):

| Setting | Payload per photo |
|---|---|
| v20-M, 3×2 (default) | ~3.8 KB |
| v20-M, 4×3 | ~7.5 KB |
| v30-L, 4×2 | ~13 KB |

Real screens add moiré, so start with the default and go denser once that
works. `--redundancy` (default 0.15) sets how many extra repair codes to add;
raise it if you expect to miss whole pages.

`pytest` runs the round-trip tests, including decoding simulated photos with
one page never photographed.
