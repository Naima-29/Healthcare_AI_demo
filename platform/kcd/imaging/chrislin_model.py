"""Chrislin's kidney CT classifier (Phase 8 notebook), as a Python callable for the Imaging Reader.

    python -m kcd.demo --ct-model-callable kcd.imaging.chrislin_model:predict \
        --calibration kcd/imaging/chrislin_calibration.json

`predict(gray)` reproduces kidney_phase8_inference.ipynb step by step:

  1. standardize the slice (crop the body, stretch intensities, pad to square, resize to 384)
  2. segment the kidneys (Phase 2 U-Net, flip-averaged); no kidney found -> error, not a guess
  3. kidney-only, brightness-normalised 320 x 320 crop (Phase 6 masked_norm input)
  4. 5 fold models (convnext_tiny), each flip-averaged; softmax probabilities averaged over the models
  5. Grad-CAM on the last stage for the predicted class, from fold model 0 (CAM_FOLD)

On a CPU, steps 2 and 4 run on OpenVINO when it is installed (KCD_CT_ENGINE=auto, the default; "torch" or
"openvino" to force one), in full fp32, with the same results; Grad-CAM needs gradients and stays on PyTorch.
PyTorch and OpenVINO use every core (KCD_CT_THREADS to limit them). The OpenVINO models are converted from the
weights once and cached next to them (KCD_CT_CACHE to put them elsewhere).

It returns contract C4 (kcd.imaging.model.prediction_from_dict). The notebook calibrates with
softmax(log(P_mean) / T), so `logits` here are log(P_mean) *before* the temperature: the Imaging
Reader's Calibration (chrislin_calibration.json, T = 0.798) applies T once and gives the notebook's
probabilities. The heat map is mapped back from the notebook's 384 x 384 frame to the input image.

Weights are not in git. Put them under models/kidney_project/ at the repository root (or point
KCD_CT_MODEL_DIR at a folder with the same layout); see MODEL_CARD_chrislin.md.
Needs torch, timm and segmentation-models-pytorch (openvino optional); they are imported on first use only.
"""
from __future__ import annotations

import importlib.util
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
    "The heat map (from one of the 5 fold models) shows the kidney region that drove the score, not the lesion.",
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
CAM_FOLD = 0  # Grad-CAM from one fold model: 5x faster on a CPU; on the 4 test images it correlates 0.94-0.99
              # with the 5-model mean, and the probabilities are unchanged (they come before the heat map)

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


# ---- torch / OpenVINO part, loaded once on first use ----
def _threads() -> int:
    return max(1, int(os.environ.get("KCD_CT_THREADS") or os.cpu_count() or 4))


def _softmax(z: np.ndarray) -> np.ndarray:
    e = np.exp(z - z.max(axis=1, keepdims=True))
    return e / e.sum(axis=1, keepdims=True)


class _Ensemble:
    def __init__(self, root: Path, device: str, engine: str = "auto"):
        import segmentation_models_pytorch as smp
        import timm
        import torch

        missing = [str(p) for p in weight_paths(root) if not p.is_file()]
        if missing:
            raise FileNotFoundError(f"CT model weights missing (see MODEL_CARD_chrislin.md): {missing}")
        if engine not in ("auto", "torch", "openvino"):
            raise ValueError(f"KCD_CT_ENGINE must be auto, torch or openvino, not {engine!r}")
        if device == "cpu":
            torch.set_num_threads(_threads())  # PyTorch uses only half the cores by default
        self.torch, self.F, self.dev = torch, torch.nn.functional, device
        self._mean = np.array([0.485, 0.456, 0.406], np.float32).reshape(1, 3, 1, 1)
        self._std = np.array([0.229, 0.224, 0.225], np.float32).reshape(1, 3, 1, 1)
        mean, std = torch.from_numpy(self._mean).to(device), torch.from_numpy(self._std).to(device)
        self.prep3 = lambda x: (x.repeat(1, 3, 1, 1) - mean) / std

        self.seg = smp.Unet("resnet34", encoder_weights=None, in_channels=3, classes=1).to(device)
        self.seg.load_state_dict(torch.load(root / "phase2" / "pilot_unet.pt", map_location=device))
        self.seg.eval()
        self.models = []
        for p in weight_paths(root)[1:]:
            m_ = timm.create_model(ARCH, pretrained=False, num_classes=4, drop_path_rate=0.1)
            m_.load_state_dict(torch.load(p, map_location="cpu"))
            self.models.append(m_.to(device).eval())
        self.cam_model = self.models[CAM_FOLD]

        has_ov = importlib.util.find_spec("openvino") is not None
        if engine == "openvino" and not (has_ov and device == "cpu"):
            raise RuntimeError("KCD_CT_ENGINE=openvino needs the openvino package and KCD_CT_DEVICE=cpu")
        self.engine = "openvino" if engine != "torch" and has_ov and device == "cpu" else "torch"
        if self.engine == "openvino":
            self._ov_seg, self._ov_cls = self._openvino(root)
            self.seg, self.models = None, []  # only the Grad-CAM model is still needed in PyTorch

    def _openvino(self, root: Path) -> tuple[Any, list[Any]]:
        import openvino as ov

        torch = self.torch
        core = ov.Core()
        cache = Path(os.environ.get("KCD_CT_CACHE") or root / "openvino")
        cache.mkdir(parents=True, exist_ok=True)
        # fp32 throughout: OpenVINO would otherwise save fp16 weights and may run bf16 on some CPUs
        config = {"PERFORMANCE_HINT": "LATENCY", "INFERENCE_NUM_THREADS": _threads(),
                  "INFERENCE_PRECISION_HINT": "f32"}

        def compiled(model: Any, size: int, weights: Path) -> Any:
            xml = cache / f"{weights.stem}-{size}.xml"
            if not xml.is_file() or xml.stat().st_mtime < weights.stat().st_mtime:
                with torch.no_grad():  # torch.export, not the deprecated torch.jit.trace
                    converted = ov.convert_model(torch.export.export(model, (torch.zeros(2, 3, size, size),)))
                ov.save_model(converted, str(xml), compress_to_fp16=False)
            return core.compile_model(str(xml), "CPU", config)

        paths = weight_paths(root)
        return (compiled(self.seg, PREP_SIZE, paths[0]),
                [compiled(m, CROP_SIZE, p) for m, p in zip(self.models, paths[1:], strict=True)])

    def _flip_pair(self, gray_u8: np.ndarray) -> np.ndarray:
        """(2, 3, S, S): the image and its mirror image, normalised like prep3, for one OpenVINO call."""
        x = gray_u8.astype(np.float32)[None, None] / 255
        x = np.concatenate([x, x[..., ::-1]])
        return np.ascontiguousarray((np.repeat(x, 3, axis=1) - self._mean) / self._std, dtype=np.float32)

    def segment(self, img384):
        if self.engine == "openvino":
            p = 1 / (1 + np.exp(-self._ov_seg(self._flip_pair(img384))[0][:, 0]))
            return (p[0] + p[1][:, ::-1]) / 2
        torch = self.torch
        with torch.no_grad():
            x = torch.from_numpy(img384.astype(np.float32) / 255)[None, None].to(self.dev)
            p = (torch.sigmoid(self.seg(self.prep3(x))) + torch.sigmoid(self.seg(self.prep3(x.flip(-1)))).flip(-1)) / 2
        return p[0, 0].cpu().numpy()

    def classify(self, X) -> np.ndarray:
        """(5, 4): each fold model's flip-averaged probabilities, in CLASSES order."""
        if self.engine == "openvino":
            pair = self._flip_pair(X)
            return np.stack([_softmax(m(pair)[0].astype(np.float64)).mean(0) for m in self._ov_cls])
        return np.stack([self.probs_one(m, X) for m in self.models])

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

    def cam(self, X, cls):
        torch = self.torch
        xb = torch.from_numpy(X).to(self.dev).float().div(255)[None, None]
        return self.gradcam(self.cam_model, xb, torch.tensor([cls], device=self.dev))[0].cpu().numpy()

    def warm_up(self) -> None:
        """One pass through every network, so the first real reading doesn't pay for the kernels' set-up."""
        self.segment(np.zeros((PREP_SIZE, PREP_SIZE), np.uint8))
        X = np.zeros((CROP_SIZE, CROP_SIZE), np.uint8)
        self.classify(X)
        self.cam(X, 0)


_lock = threading.Lock()
_ensemble: _Ensemble | None = None


def load() -> _Ensemble:
    """The six networks, loaded and warmed up once per process (about 10-20 s). Call early to avoid the wait."""
    global _ensemble
    with _lock:
        if _ensemble is None:
            env = os.environ.get
            ens = _Ensemble(model_dir(), env("KCD_CT_DEVICE", "cpu"), env("KCD_CT_ENGINE", "auto"))
            ens.warm_up()
            _ensemble = ens
        return _ensemble


def engine() -> str | None:
    """"openvino" or "torch" once loaded, else None."""
    return _ensemble.engine if _ensemble is not None else None


def analyze_u8(raw: np.ndarray) -> dict[str, Any]:
    """The notebook's analyze() on an 8-bit slice. Returns the per-model probabilities, the ensemble mean (before
    temperature), the Grad-CAM in the input image's frame and the notebook's notes."""
    ens = load()
    notes = []
    img, body, pad = standardize(raw)
    if (img > 20).mean() < 0.05:
        notes.append("image is almost entirely black after standardization: is this a CT slice?")
    with _lock:  # one reading at a time: Grad-CAM writes gradients into the shared model
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
    with _lock:
        P_m = ens.classify(X)                                          # (5, 4) raw per-model probabilities
        P_mean = P_m.mean(0)
        votes = P_m.argmax(1)
        pred = int(P_mean.argmax())  # temperature scaling never changes the arg-max
        cam384 = place_back(ens.cam(X, pred), win)
    if len(set(votes.tolist())) > 1:
        notes.append("fold models disagree: " + ", ".join(
            f"{CLASSES[v]}x{int((votes == v).sum())}" for v in sorted(set(votes.tolist()))))
    return {"P_models": P_m, "P_mean": P_mean, "pred": CLASSES[pred], "n_models_agree": int((votes == pred).sum()),
            "kidney_frac": round(kfrac, 4), "n_regions": ncomp, "notes": notes, "engine": ens.engine,
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
                     "n_models_agree": r["n_models_agree"], "engine": r["engine"]},
    }
