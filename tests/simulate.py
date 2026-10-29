"""Fake camera shots of a page shown on a screen."""
import io

import cv2
import numpy as np
from PIL import Image

# Camera colour response: each sensor channel also picks up part of the
# neighbouring display primaries, plus an off white balance.
CROSSTALK = np.array([[0.80, 0.15, 0.05],
                      [0.12, 0.76, 0.12],
                      [0.04, 0.16, 0.80]], dtype=np.float32)
HEAVY_CROSSTALK = np.array([[0.6, 0.3, 0.1],
                            [0.25, 0.5, 0.25],
                            [0.1, 0.3, 0.6]], dtype=np.float32)
WHITE_BALANCE = np.array([1.0, 0.92, 1.12], dtype=np.float32)

PHOTO = (4032, 3024)  # 12 MP phone photo
VIDEO = (1920, 1080)  # 1080p video frame


def fake_shot(page: Image.Image, seed: int, size=PHOTO, crosstalk=CROSSTALK, blur=2.0) -> np.ndarray:
    """Tilt, blur, colour crosstalk, uneven light, noise, JPEG with chroma subsampling. Returns RGB."""
    rng = np.random.default_rng(seed)
    src = np.asarray(page.convert("RGB"), dtype=np.float32)
    src = (src @ crosstalk.T) * WHITE_BALANCE
    h, w = src.shape[:2]
    W, H = size
    sx, sy = W * 0.08, H * 0.12
    jitter = lambda: rng.uniform(-0.05, 0.05) * W
    dst = np.float32([[sx + jitter(), sy + jitter()], [W - sx + jitter(), sy + jitter()],
                      [W - sx + jitter(), H - sy + jitter()], [sx + jitter(), H - sy + jitter()]])
    M = cv2.getPerspectiveTransform(np.float32([[0, 0], [w, 0], [w, h], [0, h]]), dst)
    shot = cv2.warpPerspective(src * 0.75 + 30, M, (W, H), borderValue=(15, 15, 15))  # screens are never pure black/white
    shot = cv2.GaussianBlur(shot, (0, 0), blur * W / PHOTO[0])
    xx = np.arange(W, dtype=np.float32)[None, :, None]
    shot *= 0.7 + 0.3 * (xx / W)  # light falloff across the screen
    shot += rng.normal(0, 6, shot.shape)
    buf = io.BytesIO()
    Image.fromarray(np.clip(shot, 0, 255).astype(np.uint8)).save(buf, "JPEG", quality=75)
    return np.asarray(Image.open(buf))
