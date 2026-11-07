# Model card: Chrislin's kidney CT classifier (Phase 8)

**Research prototype. Not a medical device. Not for clinical use.** The Imaging Reader sends its output to the
Safety Verifier as a *preliminary signal for radiologist review*, never as a diagnosis.

| | |
|---|---|
| Code | `kcd/imaging/chrislin_model.py` (`predict(gray)`, contract C4) |
| Calibration | `kcd/imaging/chrislin_calibration.json` (temperature 0.798) |
| `model_version` | `chrislin-kidney-ct phase8 (convnext_tiny masked_norm x5)` |
| Author | Chrislin (training, validation, notebook `kidney_phase8_inference.ipynb`) |
| Classes | Normal, Cyst, Stone, Tumor |
| Input | one 2-D CT slice (PNG / JPEG / windowed DICOM frame), soft-tissue window |

## Pipeline (identical to the notebook)

1. **Standardize**: crop the body, stretch intensities (0.5-99.5 percentile), pad to square, resize to 384.
2. **Segment the kidneys**: U-Net (ResNet-34 encoder, `phase2/pilot_unet.pt`), flip-averaged, threshold 0.5,
   small components removed. If the kidney covers < 0.5% of the slice the image is **not classified**
   (the Imaging Reader records `model_error`, nothing is sent).
3. **Model input**: kidney-only, brightness-normalised 320 x 320 crop (Phase 6 `masked_norm`).
4. **Classify**: 5 cross-validation fold models (`convnext_tiny`, `phase6/weights/masked_norm_fold{0..4}.pt`),
   each flip-averaged; softmax probabilities averaged over the 5 models.
5. **Calibrate**: temperature scaling, T = 0.798, fitted on cross-validation predictions only.
   `predict()` returns `log(mean P)` as `logits` *before* the temperature; the Imaging Reader's
   `Calibration` applies T once (softmax(log P / T)), the same formula as the notebook's `apply_temperature`.
6. **Grad-CAM**: last ConvNeXt stage, for the predicted class, averaged over the 5 models, mapped back to the
   input image's pixels. The notebook's display-only clipping to the kidney mask is not applied.

## Performance (Chrislin's held-out test set: 1,638 slices, 27 patient groups)

| Metric | Value |
|---|---|
| Balanced accuracy | **0.93** (95% CI 0.835-0.974) |
| Macro F1 | 0.913 |
| ECE after calibration | 0.026 |
| Recall Normal / Cyst / Stone | 0.994 / 0.927 / 0.993 |
| Recall **Tumor** | **0.804** (95% CI 0.413-0.932), **only 6 patient groups** |
| Balanced accuracy axial / **coronal** | 0.96 / **0.806** |

Source: `phase7/phase7_summary.json`.

## Caveats (also added to every imaging signal's note)

- **Tumour recall is 0.80 on only 6 held-out patient groups**; the confidence interval goes down to 0.41.
  A missed tumour is the most harmful error; a "normal" or "cyst" signal never rules a tumour out.
- **Coronal slices are weaker** than axial (balanced accuracy 0.81 vs 0.96).
- **The heat map shows the kidney region whose features drove the score, not the lesion.** It is not a lesion
  segmentation and must not be read as one (faithfulness: deletion AUC 0.276, insertion AUC 0.897).
- Trained and tested on soft-tissue-window CT slices from **one public dataset**. Other scanners, other windows
  (bone, lung), other body parts or unsliced 3-D volumes are outside what was tested: the output may look
  confident and still be meaningless. The Imaging Reader's quality / OOD checks catch only some of these.
- Input conversion: the Imaging Reader decodes images to float grey in [0, 1]; `predict()` rounds back to 8 bits.
  For 8-bit PNG/JPEG this is exactly the notebook's input. 16-bit images are min-max scaled by the Reader, not
  0.5-99.5 percentile scaled as in the notebook, so their results can differ slightly.

## Verification on this repository

On Chrislin's 4 test images (`Test Images/` in the Drive folder) the CPU run here reproduces the notebook's
`phase8_inference/inference_results.csv` (Colab, GPU): same class, kidney fraction, number of regions, 5/5
models agree, and every calibrated probability equal to 3 decimals. `tests/test_chrislin_model.py` checks
this when the weights are present and skips otherwise.

CPU time (Naima's laptop, torch 2.15.0+cpu, 4 threads): **about 12 s per slice** (median 11.6 s, range
10-14 s over 8 runs after a warm-up), most of it the 5 Grad-CAM backward passes; loading the six models takes
about 12 s once per process. The full demo (`python -m kcd.demo --ct-model-callable ...`) takes about 27 s.

## Getting the weights (not in git)

Shared Drive folder `kidney_project` (ask Chrislin or Naima for access):
https://drive.google.com/drive/folders/1E6RXOTQpKmhZR-c2z_R3XJh6u6HHlCo0

Put the files under `models/kidney_project/` at the repository root (gitignored), keeping the Drive layout, or
set `KCD_CT_MODEL_DIR` to another folder with the same layout:

```
models/kidney_project/
  phase2/pilot_unet.pt                         9dfa3dcce695ccb26139f5e0e38f3ae35b5e2fe65378729b6e3e0b510d56045b
  phase6/weights/masked_norm_fold0.pt          7323e2f9e90d05ce97dea4fea97bea48fe6e29d74645d86b3740d4e6095a4d84
  phase6/weights/masked_norm_fold1.pt          665c51e4fd44898619ef96456ddd413c8bcb44012791c52c04e09388184f75bd
  phase6/weights/masked_norm_fold2.pt          bfa42af519cf831175705dc36316339c3d2bf4777d1682658ed7df8ba4f1264f
  phase6/weights/masked_norm_fold3.pt          5c8caab2896a146615b848b886edb016fd16a6ab827b1d38ab50eac102888a97
  phase6/weights/masked_norm_fold4.pt          909f8ae5150e97c962ae11c9f1ff07b042abc162ae5037c4d6bc3e66ceabe73b
  phase7/phase7_summary.json                   (optional; temperature and test metrics)
  phase8_inference/inference_results.csv       (optional; the notebook's results, used by the test)
  Test Images/*.jpg                            (optional; Chrislin's 4 test slices, used by the test)
```

(sha256 on the right; check with `sha256sum`.) Dependencies, not in `pyproject.toml` because the rest of the
platform does not need them:

```
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install timm segmentation-models-pytorch scipy
```

`KCD_CT_DEVICE=cuda` runs on a GPU (the notebook then uses float16 autocast, as in Colab); the default is CPU.
