"""Adapters for the CT classifier trained by Chrislin (Person 5).

The model can arrive in two shapes; both return the same dictionary:
  * an HTTP service:   POST {"image_b64", "media_type"} -> JSON
  * a Python callable: "package.module:function"(gray: float32 array HxW in [0, 1]) -> dict

Expected dictionary (contract C4, see platform/kcd/README.md):
  {"model_version": "kidney-ct-cls 1.0",
   "labels": ["Normal", "Cyst", "Stone", "Tumor"],     # any order, each class once
   "logits": [...]  or  "probs": [...],                # same order as labels
   "heatmap": [[...], ...]  or  "heatmap_png_b64": "..."  (optional, Grad-CAM)}
"""
from __future__ import annotations

import base64
import importlib
import io
import json
import math
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Protocol

import numpy as np
from PIL import Image

from kcd.contracts import CT_LABELS
from kcd.imaging.calibration import logits_from_probs
from kcd.imaging.image_io import LoadedImage

LABEL_ALIASES = {"normal": "normal", "cyst": "cyst", "stone": "stone", "tumor": "tumor", "tumour": "tumor"}


class CtModelError(RuntimeError):
    pass


@dataclass(frozen=True)
class CtPrediction:
    logits: np.ndarray            # shape (4,), ordered as CT_LABELS
    heatmap: np.ndarray | None    # 2-D, any resolution, or None
    model_version: str
    output_kind: str              # "logits" or "probs" (what the model actually returned)


class CtModel(Protocol):
    def predict(self, image: LoadedImage, image_bytes: bytes, media_type: str) -> CtPrediction: ...


def prediction_from_dict(d: Any) -> CtPrediction:
    if not isinstance(d, dict):
        raise CtModelError("model output must be a JSON object")
    version = d.get("model_version")
    if not isinstance(version, str) or not version.strip():
        raise CtModelError("model output needs a non-empty model_version")
    labels = d.get("labels")
    if not isinstance(labels, list) or not all(isinstance(x, str) for x in labels):
        raise CtModelError("model output needs labels as a list of strings")
    canon = [LABEL_ALIASES.get(x.strip().lower()) for x in labels]
    if None in canon or sorted(canon) != sorted(CT_LABELS):
        raise CtModelError(f"labels must be exactly {CT_LABELS} (any order, each once), got {labels}")

    if ("logits" in d) == ("probs" in d):
        raise CtModelError("model output needs exactly one of 'logits' or 'probs'")
    kind = "logits" if "logits" in d else "probs"
    values = d[kind]
    if (not isinstance(values, list) or len(values) != len(labels)
            or not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in values)):
        raise CtModelError(f"'{kind}' must be {len(labels)} finite numbers")
    arr = np.asarray(values, dtype=np.float64)
    if kind == "probs":
        if arr.min() < 0 or arr.max() > 1 or abs(arr.sum() - 1.0) > 1e-3:
            raise CtModelError("probs must be in [0, 1] and sum to 1")
        arr = logits_from_probs(arr)
    order = [canon.index(label) for label in CT_LABELS]
    return CtPrediction(logits=arr[order], heatmap=_heatmap(d), model_version=version.strip(), output_kind=kind)


def _heatmap(d: dict) -> np.ndarray | None:
    if d.get("heatmap") is not None:
        try:
            h = np.asarray(d["heatmap"], dtype=np.float32)
        except (TypeError, ValueError) as e:
            raise CtModelError(f"heatmap is not a numeric 2-D array: {e}") from e
    elif d.get("heatmap_png_b64"):
        try:
            with Image.open(io.BytesIO(base64.b64decode(d["heatmap_png_b64"], validate=True))) as im:
                h = np.asarray(im.convert("L"), dtype=np.float32) / 255.0
        except Exception as e:
            raise CtModelError(f"heatmap_png_b64 is not a valid image: {e}") from e
    else:
        return None
    if h.ndim != 2 or h.size == 0 or not np.all(np.isfinite(h)):
        raise CtModelError(f"heatmap must be a finite, non-empty 2-D array, got shape {h.shape}")
    return h


class HttpCtModel:
    def __init__(self, url: str, timeout: float = 30.0):
        if not url.startswith(("http://", "https://")):
            raise ValueError("CT model URL must be http(s)")
        self.url, self.timeout = url, timeout

    def predict(self, image: LoadedImage, image_bytes: bytes, media_type: str) -> CtPrediction:
        body = json.dumps({"image_b64": base64.b64encode(image_bytes).decode("ascii"),
                           "media_type": media_type}).encode()
        req = urllib.request.Request(self.url, data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return prediction_from_dict(json.load(resp))
        except CtModelError:
            raise
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
            raise CtModelError(f"CT model service failed: {e}") from e


class CallableCtModel:
    def __init__(self, target: str | Callable[[np.ndarray], dict]):
        if isinstance(target, str):
            module, _, name = target.partition(":")
            if not module or not name:
                raise ValueError("target must look like 'package.module:function'")
            target = getattr(importlib.import_module(module), name)
        if not callable(target):
            raise ValueError("CT model target is not callable")
        self.fn = target

    def predict(self, image: LoadedImage, image_bytes: bytes, media_type: str) -> CtPrediction:
        try:
            out = self.fn(image.gray.copy())  # a copy: the model can't touch our pixels
        except Exception as e:
            raise CtModelError(f"CT model raised {type(e).__name__}: {e}") from e
        return prediction_from_dict(out)


class FixedCtModel:
    """Returns fixed probabilities and a synthetic heat map. For tests and the local demo only,
    until Chrislin's model is delivered. Its model_version always says 'stub'."""

    def __init__(self, probs: dict[str, float], heat_center: tuple[float, float] | None = (0.35, 0.55),
                 model_version: str = "stub-fixed"):
        self.probs = probs
        self.heat_center = heat_center
        self.model_version = model_version if "stub" in model_version else f"stub-{model_version}"

    def predict(self, image: LoadedImage, image_bytes: bytes, media_type: str) -> CtPrediction:
        d: dict = {"model_version": self.model_version, "labels": list(self.probs),
                   "probs": [float(v) for v in self.probs.values()]}
        if self.heat_center is not None:
            ys, xs = np.mgrid[0:32, 0:32] / 31.0
            cx, cy = self.heat_center
            d["heatmap"] = np.exp(-(((xs - cx) ** 2) + ((ys - cy) ** 2)) / 0.01).tolist()
        return prediction_from_dict(d)
