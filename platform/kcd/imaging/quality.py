"""Image-quality and out-of-distribution checks (deck slide 20: "quality / OOD check").

Two stages: image checks before the model runs, and prediction checks after it.
Every check is reported, pass or fail, so the radiologist sees why a signal was flagged.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from kcd.imaging.image_io import LoadedImage


@dataclass(frozen=True)
class QualityThresholds:
    min_side: int = 128               # px
    max_side: int = 8192
    min_aspect: float = 0.5
    max_aspect: float = 2.0
    min_std: float = 0.03             # a blank or near-uniform frame is not readable
    max_extreme_fraction: float = 0.98  # share of pixels at pure black or pure white
    max_color_spread: float = 0.08    # CT is grayscale; colour suggests a photo, screenshot or overlay
    min_top_probability: float = 0.5  # calibrated
    max_normalized_entropy: float = 0.85


DEFAULT_THRESHOLDS = QualityThresholds()


def _check(name: str, ok: bool, value: float, limit: str) -> dict:
    return {"name": name, "ok": bool(ok), "value": round(float(value), 4), "limit": limit}


def check_image(img: LoadedImage, t: QualityThresholds = DEFAULT_THRESHOLDS) -> dict:
    g = img.gray
    side_min, side_max = min(img.width, img.height), max(img.width, img.height)
    aspect = img.width / img.height if img.height else 0.0
    extreme = float(np.mean((g <= 0.01) | (g >= 0.99))) if g.size else 1.0
    checks = [
        _check("min_side_px", side_min >= t.min_side, side_min, f">= {t.min_side}"),
        _check("max_side_px", side_max <= t.max_side, side_max, f"<= {t.max_side}"),
        _check("aspect_ratio", t.min_aspect <= aspect <= t.max_aspect, aspect, f"{t.min_aspect}-{t.max_aspect}"),
        _check("contrast_std", float(np.std(g)) >= t.min_std, float(np.std(g)) if g.size else 0.0, f">= {t.min_std}"),
        _check("extreme_pixel_fraction", extreme <= t.max_extreme_fraction, extreme, f"<= {t.max_extreme_fraction}"),
        _check("grayscale", img.color_spread <= t.max_color_spread, img.color_spread, f"<= {t.max_color_spread}"),
    ]
    return {"ok": all(c["ok"] for c in checks), "checks": checks}


def check_prediction(probabilities: np.ndarray, t: QualityThresholds = DEFAULT_THRESHOLDS) -> list[dict]:
    p = np.asarray(probabilities, dtype=np.float64)
    top = float(p.max())
    k = p.size
    entropy = float(-np.sum(p * np.log(np.clip(p, 1e-12, 1.0))))
    norm_entropy = entropy / np.log(k) if k > 1 else 0.0
    return [
        _check("top_probability", top >= t.min_top_probability, top, f">= {t.min_top_probability}"),
        _check("normalized_entropy", norm_entropy <= t.max_normalized_entropy, norm_entropy,
               f"<= {t.max_normalized_entropy}"),
    ]
