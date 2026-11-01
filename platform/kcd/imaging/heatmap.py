"""Heat-map handling: normalise, resize to the key image, summary metrics, overlay PNG.

Chrislin's model produces the Grad-CAM map; this module only reads it. The overlay is a
new artifact: the original image is never altered.
"""
from __future__ import annotations

import io

import numpy as np
from PIL import Image


def normalize(heat: np.ndarray) -> np.ndarray:
    h = np.asarray(heat, dtype=np.float32)
    if h.ndim != 2 or h.size == 0:
        raise ValueError(f"heat map must be a non-empty 2-D array, got shape {h.shape}")
    if not np.all(np.isfinite(h)):
        raise ValueError("heat map contains non-finite values")
    h = np.maximum(h, 0.0)  # Grad-CAM is ReLU'd; negative values carry no attention
    peak = float(h.max())
    return (h / peak).astype(np.float32) if peak > 0 else np.zeros_like(h, dtype=np.float32)


def resize_to(heat01: np.ndarray, width: int, height: int) -> np.ndarray:
    if heat01.shape == (height, width):
        return heat01.astype(np.float32)
    im = Image.fromarray(heat01.astype(np.float32)).resize((width, height), Image.BILINEAR)
    return np.clip(np.asarray(im, dtype=np.float32), 0.0, 1.0)


def cam_metrics(heat01: np.ndarray, threshold: float = 0.5) -> dict:
    """Where the model looked. coverage: share of pixels >= threshold; center_ratio: share of attention
    mass in the central box (middle half of each axis); lr/tb_asymmetry: |a - b| / (a + b) of the mass
    between halves; peak: (x, y) of the maximum as fractions of width and height."""
    h = np.asarray(heat01, dtype=np.float64)
    rows, cols = h.shape
    total = float(h.sum())
    if total <= 0:
        return {"empty": True, "coverage": 0.0, "center_ratio": 0.0, "lr_asymmetry": 0.0, "tb_asymmetry": 0.0,
                "peak": None}
    r0, r1, c0, c1 = rows // 4, rows - rows // 4, cols // 4, cols - cols // 4
    left, right = float(h[:, : cols // 2].sum()), float(h[:, cols - cols // 2:].sum())
    top, bottom = float(h[: rows // 2].sum()), float(h[rows - rows // 2:].sum())
    py, px = np.unravel_index(int(np.argmax(h)), h.shape)

    def asym(a: float, b: float) -> float:
        return abs(a - b) / (a + b) if a + b > 0 else 0.0

    return {
        "empty": False,
        "coverage": round(float(np.mean(h >= threshold)), 4),
        "center_ratio": round(float(h[r0:r1, c0:c1].sum()) / total, 4),
        "lr_asymmetry": round(asym(left, right), 4),
        "tb_asymmetry": round(asym(top, bottom), 4),
        "peak": [round(float(px + 0.5) / cols, 4), round(float(py + 0.5) / rows, 4)],
    }


def _colormap(h: np.ndarray) -> np.ndarray:
    """Jet-like map: blue (low) -> green -> red (high)."""
    r = np.clip(1.5 - np.abs(4 * h - 3), 0, 1)
    g = np.clip(1.5 - np.abs(4 * h - 2), 0, 1)
    b = np.clip(1.5 - np.abs(4 * h - 1), 0, 1)
    return np.stack([r, g, b], axis=-1)


def overlay_png(gray: np.ndarray, heat01: np.ndarray, alpha: float = 0.5) -> bytes:
    if gray.shape != heat01.shape:
        raise ValueError(f"image {gray.shape} and heat map {heat01.shape} must have the same shape")
    base = np.repeat(np.clip(gray, 0, 1)[..., None], 3, axis=-1)
    weight = (alpha * heat01)[..., None]
    rgb = (1 - weight) * base + weight * _colormap(heat01)
    buf = io.BytesIO()
    Image.fromarray((np.clip(rgb, 0, 1) * 255).astype(np.uint8)).save(buf, format="PNG")
    return buf.getvalue()
