"""Chrislin's kidney CT classifier (Phase 8 notebook), as a Python callable for the Imaging Reader.

    python -m kcd.demo --ct-model-callable kcd.imaging.chrislin_model:predict \
        --calibration kcd/imaging/chrislin_calibration.json

`predict(gray)` reproduces kidney_phase8_inference.ipynb step by step:

  1. standardize the slice (crop the body, stretch intensities, pad to square, resize to 384)
  2. segment the kidneys (Phase 2 U-Net, flip-averaged); no kidney found -> error, not a guess
  3. kidney-only, brightness-normalised 320 x 320 crop (Phase 6 masked_norm input)
  4. 5 fold models (convnext_tiny), each flip-averaged; softmax probabilities averaged over the models
  5. Grad-CAM on the last stage for the predicted class, averaged over the 5 models

It returns contract C4 (kcd.imaging.model.prediction_from_dict). The notebook calibrates with
softmax(log(P_mean) / T), so `logits` here are log(P_mean) *before* the temperature: the Imaging
Reader's Calibration (chrislin_calibration.json, T = 0.798) applies T once and gives the notebook's
probabilities. The heat map is mapped back from the notebook's 384 x 384 frame to the input image.

Weights are not in git. Put them under models/kidney_project/ at the repository root (or point
KCD_CT_MODEL_DIR at a folder with the same layout); see MODEL_CARD_chrislin.md.
Needs torch, timm and segmentation-models-pytorch; they are imported on first use only.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image
from scipy import ndimage as ndi

MODEL_VERSION = "chrislin-kidney-ct phase8 (convnext_tiny masked_norm x5)"
LABELS = ["Normal", "Cyst", "Stone", "Tumor"]          # order of the returned logits
CLASSES = ["Cyst", "Normal", "Stone", "Tumor"]         # the notebook's (training) class order
CAVEATS = [
    "Research prototype, not a medical device.",
    "Tumour recall 0.80 (95% CI 0.41-0.93) on only 6 held-out patient groups.",
    "Coronal slices are weaker than axial (balanced accuracy 0.81 vs 0.96).",
    "The heat map shows the kidney region that drove the score, not the lesion.",
    "Trained on soft-tissue-window CT slices from one public dataset; other scanners or windows are untested.",
]

# ============ FROZEN SETTINGS (must match Phases 2, 6, 7) ============
ARCH = "convnext_tiny"
ARM = "masked_norm"
N_FOLD_MODELS = 5
PREP_SIZE = 384
MIN_KIDNEY_FRAC = 0.005
CROP_SIZE, CROP_SIDE_MULT, MIN_SIDE_FRAC = 320, 1.15, 0.40
MASK_FULL_MARGIN, MASK_FEATHER = 0.04, 0.03
NORM_MEAN, NORM_STD = 110.0, 35.0
SEG_THRESHOLD = 0.5
# =====================================================================

REPO_ROOT = Path(__file__).resolve().parents[3]


def model_dir() -> Path:
    return Path(os.environ.get("KCD_CT_MODEL_DIR") or REPO_ROOT / "models" / "kidney_project")


def weight_paths(root: Path | None = None) -> list[Path]:
    root = root or model_dir()
    return [root / "phase2" / "pilot_unet.pt"] + [
        root / "phase6" / "weights" / f"{ARM}_fold{k}.pt" for k in range(N_FOLD_MODELS)]


def weights_present(root: Path | None = None) -> bool:
    return all(p.is_file() for p in weight_paths(root))


class NoKidneyFound(ValueError):
    pass


# ---- helpers (no torch): copies of the notebook's validated functions ----
def standardize(img_u8: np.ndarray) -> tuple[np.ndarray, tuple[int, int, int, int], tuple[int, int, int]]:
    """Phase 1b recipe. Returns the PREP_SIZE square slice, the body box (x0, y0, x1, y1) in the input and
    the padding (side, oy, ox), so the heat map can be mapped back."""
    a = img_u8
    H, W = a.shape
    m = ndi.gaussian_filter(a.astype(np.float32), 2) > 20
    lab, k = ndi.label(m)
    keep = (lab == 1 + int(np.argmax(ndi.sum(m, lab, range(1, k + 1))))) if k > 0 else m
    if keep.mean() < 0.05:
        x0, y0, x1, y1 = 0, 0, W, H
    else:
        ys, xs = np.where(keep)
        x0, x1, y0, y1 = xs.min(), xs.max() + 1, ys.min(), ys.max() + 1
    mg = int(0.04 * max(x1 - x0, y1 - y0))
    x0, y0, x1, y1 = max(0, x0 - mg), max(0, y0 - mg), min(W, x1 + mg), min(H, y1 + mg)
    crop = a[y0:y1, x0:x1].astype(np.float32)
    bg = crop <= 20
    if (~bg).sum() > 100:
        lo, hi = np.percentile(crop[~bg], [0.5, 99.5])
        crop = np.clip((crop - lo) / (hi - lo + 1e-6), 0, 1) * 255
    crop[bg] = 0
    h, w = crop.shape
    side = max(h, w)
    canvas = np.zeros((side, side), np.float32)
    oy, ox = (side - h) // 2, (side - w) // 2
    canvas[oy:oy + h, ox:ox + w] = crop
    out = Image.fromarray(np.clip(canvas, 0, 255).astype(np.uint8)).resize((PREP_SIZE, PREP_SIZE), Image.BILINEAR)
    return np.asarray(out), (int(x0), int(y0), int(x1), int(y1)), (side, oy, ox)


def clean_mask(m, min_frac=0.0015):
    lab_, n_ = ndi.label(m)
    if n_ == 0:
        return m.astype(np.uint8)
    areas = ndi.sum(m, lab_, range(1, n_ + 1))
    return np.isin(lab_, 1 + np.where(areas >= min_frac * m.size)[0]).astype(np.uint8)


def crop_box(m, margin=0.25):
    ys, xs = np.where(m > 0)
    if len(xs) == 0:
        return None
    x0, x1, y0, y1 = xs.min(), xs.max() + 1, ys.min(), ys.max() + 1
    mg = margin * max(x1 - x0, y1 - y0)
    H, W = m.shape
    return (int(max(0, x0 - mg)), int(max(0, y0 - mg)), int(min(W, x1 + mg)), int(min(H, y1 + mg)))


def crop_window(box, size=PREP_SIZE, mult=CROP_SIDE_MULT, min_frac=MIN_SIDE_FRAC):
    x0, y0, x1, y1 = box
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    side = min(max(max(x1 - x0, y1 - y0) * mult, min_frac * size), size)
    h = side / 2
    cx = min(max(cx, h), size - h)
    cy = min(max(cy, h), size - h)
    a, b, c, d = (int(round(v)) for v in (cx - h, cy - h, cx + h, cy + h))
    return max(0, a), max(0, b), min(size, c), min(size, d)


def soft_alpha(mask, full=MASK_FULL_MARGIN, feather=MASK_FEATHER):
    S = mask.shape[0]
    if not mask.any():
        return np.zeros(mask.shape, np.float32)
    dist = ndi.distance_transform_edt(~mask)
    return np.clip(1.0 - (dist - full * S) / (feather * S), 0, 1).astype(np.float32)


def norm_kidney(a, m, mean_t=NORM_MEAN, std_t=NORM_STD):
    vals = a[m]
    return (a - vals.mean()) / (vals.std() + 1.0) * std_t + mean_t


def to_crop(a_float, box):
    x0, y0, x1, y1 = box
    return np.asarray(Image.fromarray(np.clip(a_float[y0:y1, x0:x1], 0, 255).astype(np.uint8))
                      .resize((CROP_SIZE, CROP_SIZE), Image.BILINEAR))


def place_back(cam, win, size=PREP_SIZE):
    x0, y0, x1, y1 = win
    h = np.asarray(Image.fromarray(cam.astype(np.float32)).resize((x1 - x0, y1 - y0), Image.BILINEAR))
    full = np.zeros((size, size), np.float32)
    full[y0:y1, x0:x1] = h
    return full


def build_model_input(img384, mask):
    """kidney-only, brightness-normalised crop (Phase 6 recipe). Returns (320x320 uint8, window)."""
    m = mask > 0
    win = crop_window(crop_box(mask))
    a = img384.astype(np.float32)
    return to_crop(norm_kidney(a, m) * soft_alpha(m), win), win


def to_input_frame(cam384: np.ndarray, body: tuple[int, int, int, int], pad: tuple[int, int, int],
                   shape: tuple[int, int]) -> np.ndarray:
    """Inverse of standardize() for the heat map: 384 square -> padded square -> body box in the input image."""
    side, oy, ox = pad
    x0, y0, x1, y1 = body
    sq = np.asarray(Image.fromarray(cam384.astype(np.float32)).resize((side, side), Image.BILINEAR))
    full = np.zeros(shape, np.float32)
    full[y0:y1, x0:x1] = sq[oy:oy + (y1 - y0), ox:ox + (x1 - x0)]
    return full


def to_u8(gray: np.ndarray) -> np.ndarray:
    """Imaging Reader gray (float in [0, 1]) -> the notebook's 8-bit input. For an 8-bit PNG/JPEG this gives back
    the decoded pixel values exactly (PIL's 'L' conversion for colour images may differ by 1 grey level)."""
    g = np.asarray(gray, dtype=np.float32)
    if g.ndim != 2:
        raise ValueError(f"expected a 2-D grayscale array, got shape {g.shape}")
    return np.clip(np.rint(g * 255.0), 0, 255).astype(np.uint8)


# ---- torch part, loaded once on first use ----
class _Ensemble:
    def __init__(self, root: Path, device: str):
        import segmentation_models_pytorch as smp
        import timm
        import torch

        missing = [str(p) for p in weight_paths(root) if not p.is_file()]
        if missing:
            raise FileNotFoundError(f"CT model weights missing (see MODEL_CARD_chrislin.md): {missing}")
        self.torch, self.F, self.dev = torch, torch.nn.functional, device
        mean = torch.tensor([0.485, 0.456, 0.406], device=device).view(1, 3, 1, 1)
        std = torch.tensor([0.229, 0.224, 0.225], device=device).view(1, 3, 1, 1)
        self.prep3 = lambda x: (x.repeat(1, 3, 1, 1) - mean) / std

        self.seg = smp.Unet("resnet34", encoder_weights=None, in_channels=3, classes=1).to(device)
        self.seg.load_state_dict(torch.load(root / "phase2" / "pilot_unet.pt", map_location=device))
        self.seg.eval()
        self.models = []
        for p in weight_paths(root)[1:]:
            m_ = timm.create_model(ARCH, pretrained=False, num_classes=4, drop_path_rate=0.1)
            m_.load_state_dict(torch.load(p, map_location="cpu"))
            self.models.append(m_.to(device).eval())

    def segment(self, img384):
        torch = self.torch
        with torch.no_grad():
            x = torch.from_numpy(img384.astype(np.float32) / 255)[None, None].to(self.dev)
            p = (torch.sigmoid(self.seg(self.prep3(x))) + torch.sigmoid(self.seg(self.prep3(x.flip(-1)))).flip(-1)) / 2
        return p[0, 0].cpu().numpy()

    def probs_one(self, model, X):
        torch, F = self.torch, self.F
        with torch.no_grad():
            xb = torch.from_numpy(X).to(self.dev).float().div(255)[None, None]
            with torch.autocast(device_type=self.dev, dtype=torch.float16, enabled=(self.dev == "cuda")):
                p = F.softmax(model(self.prep3(xb)).float(), 1) + F.softmax(model(self.prep3(xb.flip(-1))).float(), 1)
        return (p / 2)[0].cpu().numpy()

    def gradcam(self, model, x_gray, cls_idx):
        torch, F = self.torch, self.F
        S = x_gray.shape[-1]
        store: dict[str, Any] = {}

        def fwd(_m, _i, o):
            store["A"] = o
            if o.requires_grad:
                o.register_hook(lambda g: store.__setitem__("G", g))

        h = model.stages[3].register_forward_hook(fwd)
        try:
            model.zero_grad(set_to_none=True)
            with torch.enable_grad():
                model(self.prep3(x_gray)).gather(1, cls_idx[:, None]).sum().backward()
            A, G = store["A"].detach(), store["G"].detach()
        finally:
            h.remove()
        cam = F.relu((G.mean((2, 3), keepdim=True) * A).sum(1))
        c = F.interpolate(cam[:, None], size=(S, S), mode="bilinear", align_corners=False)[:, 0]
        mn = c.flatten(1).min(1)[0].view(-1, 1, 1)
        mx = c.flatten(1).max(1)[0].view(-1, 1, 1)
        return (c - mn) / (mx - mn + 1e-8)

    def ensemble_cam(self, X, cls):
        torch = self.torch
        xb = torch.from_numpy(X).to(self.dev).float().div(255)[None, None]
        c = torch.tensor([cls], device=self.dev)
        return torch.stack([self.gradcam(m, xb, c) for m in self.models]).mean(0)[0].cpu().numpy()


_lock = threading.Lock()
_ensemble: _Ensemble | None = None


def load() -> _Ensemble:
    global _ensemble
    with _lock:
        if _ensemble is None:
            _ensemble = _Ensemble(model_dir(), os.environ.get("KCD_CT_DEVICE", "cpu"))
        return _ensemble


def analyze_u8(raw: np.ndarray) -> dict[str, Any]:
    """The notebook's analyze() on an 8-bit slice. Returns the per-model probabilities, the ensemble mean (before
    temperature), the Grad-CAM in the input image's frame and the notebook's notes."""
    ens = load()
    notes = []
    img, body, pad = standardize(raw)
    if (img > 20).mean() < 0.05:
        notes.append("image is almost entirely black after standardization: is this a CT slice?")
    mask = clean_mask(ens.segment(img) > SEG_THRESHOLD)
    kfrac = float(mask.mean())
    ncomp = int(ndi.label(mask)[1])
    if kfrac < MIN_KIDNEY_FRAC:
        raise NoKidneyFound(f"no kidney segmented (kidney fraction {kfrac:.4f} < {MIN_KIDNEY_FRAC}); not classified. "
                            + " ".join(notes))
    if ncomp > 2:
        notes.append(f"segmentation has {ncomp} separate regions (normally 1-2 kidneys); check the outline")
    X, win = build_model_input(img, mask)
    X = np.array(X)  # writable copy for torch.from_numpy (PIL arrays are read-only); same values
    with _lock:  # Grad-CAM writes gradients into the shared models
        P_m = np.stack([ens.probs_one(m, X) for m in ens.models])     # (5, 4) raw per-model probabilities
        P_mean = P_m.mean(0)
        votes = P_m.argmax(1)
        pred = int(P_mean.argmax())  # temperature scaling never changes the arg-max
        cam384 = place_back(ens.ensemble_cam(X, pred), win)
    if len(set(votes.tolist())) > 1:
        notes.append("fold models disagree: " + ", ".join(
            f"{CLASSES[v]}x{int((votes == v).sum())}" for v in sorted(set(votes.tolist()))))
    return {"P_models": P_m, "P_mean": P_mean, "pred": CLASSES[pred], "n_models_agree": int((votes == pred).sum()),
            "kidney_frac": round(kfrac, 4), "n_regions": ncomp, "notes": notes,
            "heatmap": to_input_frame(cam384, body, pad, raw.shape)}


def predict(gray: np.ndarray) -> dict[str, Any]:
    """Contract C4 for kcd.imaging.model.CallableCtModel. `gray`: float32 H x W in [0, 1]."""
    r = analyze_u8(to_u8(gray))
    logp = np.log(np.clip(r["P_mean"], 1e-8, 1))   # the notebook's apply_temperature() input
    return {
        "model_version": MODEL_VERSION,
        "labels": LABELS,
        "logits": [float(logp[CLASSES.index(c)]) for c in LABELS],
        "heatmap": r["heatmap"],
        "caveats": list(CAVEATS),
        "warnings": r["notes"],
        "pipeline": {"kidney_frac": r["kidney_frac"], "n_regions": r["n_regions"],
                     "n_models_agree": r["n_models_agree"]},
    }
