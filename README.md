# qrtransfer

Send a file through photos of a screen. The file is compressed, cut into
blocks, protected with a fountain code and drawn as pages of QR codes.
Photograph the pages (or film a slideshow of them) and decode the photos to
get the exact original file back.

```
pip install -r requirements.txt
python -m qrtransfer encode report.pdf           # -> report_qr/page_*.png + viewer.html
python -m qrtransfer encode report.pdf --color   # 3x denser colour codes
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
| `color.py` | With `--color`, stacks 3 QR codes per slot, one in each of R, G and B, so each module is one of 8 colors |
| `decode.py` | Uses zxing-cpp to find every QR code in each photo or video frame, then solves for the blocks. Color photos are detected automatically |

Integrity is checked at three levels: the Reed-Solomon code inside each QR,
the CRC-32 on each packet, and the SHA-256 of the whole file. A decoded file
is either bit-exact or rejected.

## Color mode

Each slot holds three ordinary QR codes, one per RGB channel. Finder and
timing patterns are dark in all three layers, so they show as black and the
codes are located just like normal QR codes. To read a color photo, the
decoder:

1. normalises each channel against the local brightest and darkest pixels.
   This cancels white balance, uneven lighting and screen brightness.
2. undoes the camera's color mixing (each sensor channel also picks up some
   of the neighbouring display colors). It tries a few typical strengths, so
   no calibration chart is needed.
3. thresholds each channel into a black-and-white QR image and decodes it
   normally. Any misread is caught by the CRC.

Tips for real screens: turn off night mode or blue-light filters (they shift
the colors), set brightness high, and avoid glare.

## Tuning

`--qr-version`, `--ecc`, `--cols` and `--rows` trade density against how easy
the codes are to photograph. In simulated 12 MP photos (perspective, blur,
uneven light, noise, JPEG):

| Setting | Black & white | `--color` |
|---|---|---|
| v20-M, 3×2 (default) | ~3.8 KB | ~11 KB |
| v20-M, 4×3 | ~7.5 KB | ~22 KB |
| v30-L, 4×2 | ~13 KB | ~31 KB (some codes missed, covered by repair codes) |

Real screens add moiré, so start with the default and go denser once that
works. `--redundancy` (default 0.15) sets how many extra repair codes to add;
raise it if you expect to miss whole pages.

`pytest` runs the round-trip tests, including decoding simulated photos (black
and white, and color with heavy color mixing) with one page never
photographed.
