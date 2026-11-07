"""Chrislin's CT model (kcd/imaging/chrislin_model.py) against her notebook's results.

The weights are not in git (MODEL_CARD_chrislin.md says where to get them): without them, or without
torch / timm / segmentation-models-pytorch, the model tests skip. The calibration file is always checked.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pytest

from kcd.imaging.calibration import Calibration
from kcd.imaging.model import CT_LABELS, CallableCtModel, prediction_from_dict

CAL_PATH = Path(__file__).resolve().parents[1] / "kcd" / "imaging" / "chrislin_calibration.json"
# phase8_inference/inference_results.csv from the notebook run in Colab (GPU), Chrislin's 4 test images.
NOTEBOOK = {
    "Tumor- (90).jpg": ("tumor", 0.1325, {"cyst": 0.010, "normal": 0.009, "stone": 0.010, "tumor": 0.971}),
    "Stone- (90).jpg": ("stone", 0.0406, {"cyst": 0.009, "normal": 0.009, "stone": 0.972, "tumor": 0.010}),
    "Normal- (90).jpg": ("normal", 0.0433, {"cyst": 0.010, "normal": 0.970, "stone": 0.010, "tumor": 0.010}),
    "Cyst- (99).jpg": ("cyst", 0.1056, {"cyst": 0.970, "normal": 0.010, "stone": 0.009, "tumor": 0.011}),
}

_deps = all(importlib.util.find_spec(m) for m in ("torch", "timm", "segmentation_models_pytorch", "scipy"))
needs_scipy = pytest.mark.skipif(importlib.util.find_spec("scipy") is None, reason="scipy not installed")


def _cm():
    from kcd.imaging import chrislin_model
    return chrislin_model


def _ready():
    if not _deps:
        pytest.skip("torch / timm / segmentation-models-pytorch / scipy not installed")
    cm = _cm()
    if not cm.weights_present():
        pytest.skip(f"CT model weights not found under {cm.model_dir()} (see MODEL_CARD_chrislin.md)")
    return cm


@needs_scipy
def test_calibration_file_matches_the_model():
    cal = Calibration.load(CAL_PATH)
    assert cal.temperature == 0.798
    assert cal.model_version == _cm().MODEL_VERSION
    assert "stub" not in cal.model_version


@needs_scipy
def test_weights_absent_is_reported(tmp_path):
    assert not _cm().weights_present(tmp_path)


@pytest.mark.parametrize("name", sorted(NOTEBOOK))
def test_reproduces_the_notebook(name):
    cm = _ready()
    path = cm.model_dir() / "Test Images" / name
    if not path.is_file():
        pytest.skip(f"test image {path} not found")
    from kcd.imaging.image_io import load_image

    image = load_image(path.read_bytes())
    pred = CallableCtModel(cm.predict).predict(image, b"", "image/jpeg")
    probs = Calibration.load(CAL_PATH).apply(pred.logits)
    label, kidney_frac, expected = NOTEBOOK[name]

    assert pred.model_version == cm.MODEL_VERSION and pred.output_kind == "logits"
    assert CT_LABELS[int(np.argmax(probs))] == label
    assert {lbl: round(float(p), 3) for lbl, p in zip(CT_LABELS, probs, strict=True)} == expected
    assert pred.heatmap is not None and pred.heatmap.shape == image.gray.shape
    assert pred.caveats and any("0.80" in c for c in pred.caveats)
    assert cm.analyze_u8(cm.to_u8(image.gray))["kidney_frac"] == kidney_frac


def test_raw_logits_are_before_the_temperature():
    """The model must not apply T itself: softmax(logits) is the plain 5-model mean."""
    cm = _ready()
    path = cm.model_dir() / "Test Images" / "Tumor- (90).jpg"
    if not path.is_file():
        pytest.skip("test image not found")
    from kcd.imaging.image_io import load_image

    gray = load_image(path.read_bytes()).gray
    d = cm.predict(gray)
    r = cm.analyze_u8(cm.to_u8(gray))
    mean = {c.lower(): float(r["P_mean"][j]) for j, c in enumerate(cm.CLASSES)}
    pred = prediction_from_dict(d)
    raw = np.exp(pred.logits) / np.exp(pred.logits).sum()
    assert np.allclose(raw, [mean[lbl] for lbl in CT_LABELS], atol=1e-6)


def test_no_kidney_is_not_classified():
    cm = _ready()
    from kcd.imaging.model import CtModelError

    blank = np.zeros((256, 256), np.float32)
    blank[64:192, 64:192] = 0.5  # a flat grey square: no kidney
    with pytest.raises(CtModelError, match="no kidney"):
        CallableCtModel(cm.predict).predict(type("I", (), {"gray": blank})(), b"", "image/png")
