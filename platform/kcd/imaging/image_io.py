"""Decode a CT key image (PNG / JPEG / DICOM) into a grayscale array in [0, 1].

The original bytes are never modified (refusal alter_image): decoding works on a copy.
"""
from __future__ import annotations

import io
from dataclasses import dataclass

import numpy as np
from PIL import Image, UnidentifiedImageError

# Abdominal soft-tissue window, used when a DICOM file carries no window of its own.
DEFAULT_WINDOW_CENTER = 40.0
DEFAULT_WINDOW_WIDTH = 400.0
MAX_PIXELS = 64_000_000


class ImageDecodeError(ValueError):
    pass


@dataclass(frozen=True)
class LoadedImage:
    gray: np.ndarray          # float32, shape (H, W), values in [0, 1]
    color_spread: float       # mean (max channel - min channel); ~0 for a true grayscale CT
    source_format: str        # "PNG", "JPEG", "DICOM", ...

    @property
    def height(self) -> int:
        return int(self.gray.shape[0])

    @property
    def width(self) -> int:
        return int(self.gray.shape[1])


def is_dicom(data: bytes) -> bool:
    return len(data) > 132 and data[128:132] == b"DICM"


def load_image(data: bytes) -> LoadedImage:
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise ImageDecodeError("empty image")
    if is_dicom(data):
        return _load_dicom(bytes(data))
    try:
        with Image.open(io.BytesIO(data)) as im:
            if im.width * im.height > MAX_PIXELS:
                raise ImageDecodeError(f"image too large: {im.width}x{im.height}")
            im.load()
            fmt = im.format or "unknown"
            if im.mode in ("I", "I;16", "I;16B", "I;16L", "F"):
                arr = np.asarray(im, dtype=np.float32)
                return LoadedImage(gray=_minmax(arr), color_spread=0.0, source_format=fmt)
            rgb = np.asarray(im.convert("RGB"), dtype=np.float32) / 255.0
    except ImageDecodeError:
        raise
    except (UnidentifiedImageError, OSError, ValueError) as e:
        raise ImageDecodeError(f"cannot decode image: {e}") from e
    gray = rgb @ np.array([0.299, 0.587, 0.114], dtype=np.float32)
    spread = float(np.mean(rgb.max(axis=2) - rgb.min(axis=2)))
    return LoadedImage(gray=np.clip(gray, 0.0, 1.0).astype(np.float32), color_spread=spread, source_format=fmt)


def _minmax(arr: np.ndarray) -> np.ndarray:
    arr = arr.astype(np.float32)
    lo, hi = float(np.min(arr)), float(np.max(arr))
    if not np.isfinite(lo) or not np.isfinite(hi):
        raise ImageDecodeError("image contains non-finite values")
    if hi <= lo:
        return np.zeros_like(arr, dtype=np.float32)
    return ((arr - lo) / (hi - lo)).astype(np.float32)


def _first(value, default: float) -> float:
    if value is None:
        return default
    try:
        return float(value[0]) if hasattr(value, "__len__") and not isinstance(value, str) else float(value)
    except (TypeError, ValueError, IndexError):
        return default


def _load_dicom(data: bytes) -> LoadedImage:
    try:
        import pydicom  # optional dependency
    except ImportError as e:
        raise ImageDecodeError("DICOM input needs pydicom: pip install pydicom") from e
    try:
        ds = pydicom.dcmread(io.BytesIO(data))
        px = ds.pixel_array.astype(np.float32)
    except Exception as e:  # pydicom raises many types for bad or compressed data
        raise ImageDecodeError(f"cannot decode DICOM: {e}") from e
    if px.ndim == 3:  # multi-frame: use the middle frame as the key image
        px = px[px.shape[0] // 2]
    if px.ndim != 2:
        raise ImageDecodeError(f"unsupported DICOM pixel shape {px.shape}")
    hu = px * _first(getattr(ds, "RescaleSlope", None), 1.0) + _first(getattr(ds, "RescaleIntercept", None), 0.0)
    center = _first(getattr(ds, "WindowCenter", None), DEFAULT_WINDOW_CENTER)
    width = max(_first(getattr(ds, "WindowWidth", None), DEFAULT_WINDOW_WIDTH), 1.0)
    lo, hi = center - width / 2.0, center + width / 2.0
    gray = np.clip((hu - lo) / (hi - lo), 0.0, 1.0).astype(np.float32)
    return LoadedImage(gray=gray, color_spread=0.0, source_format="DICOM")
