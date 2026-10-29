"""Color QR: three ordinary QR codes stacked in the R, G and B channels.

Each module takes one of 8 colors (white, black, R, G, B, C, M, Y), so one
color QR carries three packets in the space of one. All three layers share
the same QR version, so finder and timing patterns are dark in every layer
and print as black: detection works exactly like a normal QR.

Decoding a photo (no calibration chart needed):
  1. each channel is normalised locally from the brightest/darkest pixels in a
     window of a few modules, removing white balance, vignetting and screen
     brightness, so a pixel becomes ~ C @ bits with bits in {0,1}^3,
  2. C is the camera's colour crosstalk (each sensor channel also sees part
     of the neighbouring display primaries). It is unknown, so we undo a few
     typical strengths with inv(C) and threshold each layer at 0.5,
  3. every result is a black-and-white image a standard QR reader decodes;
     the per-packet CRC throws away anything misread.
"""
import cv2
import numpy as np

CROSSTALK_LEVELS = (0.0, 0.25, 0.45)


def compose(layers: list[np.ndarray]) -> np.ndarray:
    """Three boolean module matrices (True = dark) -> RGB uint8 image (one pixel per module)."""
    return np.stack([np.where(m, 0, 255) for m in layers], axis=-1).astype(np.uint8)


def is_colorful(rgb: np.ndarray) -> bool:
    small = rgb[::8, ::8].astype(np.int16)
    sat = small.max(axis=-1) - small.min(axis=-1)
    return np.mean(sat > 60) > 0.02


def crosstalk_matrix(a: float) -> np.ndarray:
    """observed = C @ true. Green leaks equally into both neighbours; red and blue leak mostly into green."""
    return np.array([[1 - a, 0.75 * a, 0.25 * a],
                     [0.5 * a, 1 - a, 0.5 * a],
                     [0.25 * a, 0.75 * a, 1 - a]], dtype=np.float32)


def _normalise(rgb: np.ndarray, win: int) -> np.ndarray:
    f = rgb.astype(np.float32)
    kernel = np.ones((win, win), np.uint8)
    hi = cv2.blur(cv2.dilate(f, kernel), (win, win))
    lo = cv2.blur(cv2.erode(f, kernel), (win, win))
    return np.clip((f - lo) / np.maximum(hi - lo, 25.0), 0.0, 1.0)


def separate(rgb: np.ndarray):
    """Photo -> candidate black-and-white layer images to hand to a QR reader (generator)."""
    h, w = rgb.shape[:2]
    if max(h, w) > 3000:  # keeps it fast; modules stay well above 3 px
        scale = 3000 / max(h, w)
        rgb = cv2.resize(rgb, (round(w * scale), round(h * scale)), interpolation=cv2.INTER_AREA)
    for c in range(3):
        yield np.ascontiguousarray(rgb[..., c])
    for win in (max(15, max(rgb.shape) // 64), max(31, max(rgb.shape) // 24)):
        norm = _normalise(rgb, win | 1)
        for a in CROSSTALK_LEVELS:
            unmixed = norm @ np.linalg.inv(crosstalk_matrix(a)).T
            for c in range(3):
                yield np.where(unmixed[..., c] > 0.5, 255, 0).astype(np.uint8)
