"""Generate the synthetic edge-case patient Sofia B. (fixtures/sofia-b/). Deterministic: rerun to rebuild.

    python -m kcd.fixtures.make_sofia

Everything is synthetic. The CT key images are drawn here (no real scans), except the tumor study, which
reuses the public de-identified slice from the v2 demo (fixtures/marco-r/ct.png). The scripted CT model's
answer for each study is in ct-scripted-outputs.json: the edge-case scenario uses it so that every run is
the same; Chrislin's real model can be run on real slices separately (python -m kcd.scenarios.edge_cases
--with-real-ct-model).
"""

from __future__ import annotations

import io
import json
import shutil
from pathlib import Path

import numpy as np
from PIL import Image

HERE = Path(__file__).resolve().parent
OUT = HERE / "sofia-b"
PATIENT = "sofia-b"


def _png(arr: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format="PNG")
    return buf.getvalue()


def _slice(seed: int, size: int = 384, lesion: tuple[float, float, float] | None = None,
           contrast: float = 1.0) -> np.ndarray:
    """A synthetic axial slice: body ellipse, two kidneys, optional bright or dark lesion."""
    rng = np.random.default_rng(seed)
    ys, xs = np.mgrid[0:size, 0:size] / size
    body = ((xs - 0.5) / 0.42) ** 2 + ((ys - 0.5) / 0.32) ** 2 < 1
    img = np.where(body, 70 + 15 * rng.standard_normal((size, size)), 0)
    for cx in (0.33, 0.67):
        kidney = ((xs - cx) / 0.07) ** 2 + ((ys - 0.55) / 0.11) ** 2 < 1
        img = np.where(kidney, 130 + 10 * rng.standard_normal((size, size)), img)
    if lesion:
        lx, ly, value = lesion
        spot = ((xs - lx) / 0.025) ** 2 + ((ys - ly) / 0.025) ** 2 < 1
        img = np.where(spot, value, img)
    img = 128 + (img - 128) * contrast
    return np.clip(img, 0, 255).astype(np.uint8)


# study folder -> (study.json fields, how to make the key image, scripted model answer, what it tests)
STUDIES = [
    ("01-normal", {"acquired": "2026-01-10T09:00:00Z"}, lambda: _png(_slice(1)),
     {"probs": {"normal": 0.91, "cyst": 0.04, "stone": 0.03, "tumor": 0.02}}, "clear normal kidneys"),
    ("02-cyst", {"acquired": "2026-02-10T09:00:00Z"}, lambda: _png(_slice(2, lesion=(0.33, 0.52, 40))),
     {"probs": {"normal": 0.05, "cyst": 0.88, "stone": 0.04, "tumor": 0.03}}, "simple cyst"),
    ("03-stone", {"acquired": "2026-03-10T09:00:00Z"}, lambda: _png(_slice(3, lesion=(0.67, 0.58, 250))),
     {"probs": {"normal": 0.04, "cyst": 0.03, "stone": 0.90, "tumor": 0.03}}, "kidney stone"),
    ("04-tumor", {"acquired": "2026-10-11T18:20:00Z"}, None,  # public slice from the v2 demo
     {"probs": {"normal": 0.01, "cyst": 0.02, "stone": 0.02, "tumor": 0.95}}, "renal mass (follow-up CT)"),
    ("05-uncertain", {"acquired": "2026-04-10T09:00:00Z"}, lambda: _png(_slice(5, lesion=(0.35, 0.6, 180))),
     {"probs": {"normal": 0.30, "cyst": 0.26, "stone": 0.22, "tumor": 0.22}}, "model unsure (out of distribution)"),
    ("06-small-low-contrast", {"acquired": "2026-05-10T09:00:00Z"},
     lambda: _png(_slice(6, size=96, contrast=0.08)),
     {"probs": {"normal": 0.86, "cyst": 0.05, "stone": 0.05, "tumor": 0.04}}, "poor image: 96 px, almost no contrast"),
    ("07-colour-screenshot", {"acquired": "2026-06-10T09:00:00Z"},
     lambda: _png(np.stack([_slice(7), _slice(7) // 2, np.full((384, 384), 200, np.uint8)], axis=-1)),
     {"probs": {"normal": 0.87, "cyst": 0.05, "stone": 0.04, "tumor": 0.04}}, "colour screenshot, not a CT slice"),
    ("08-corrupt", {"acquired": "2026-07-10T09:00:00Z"}, lambda: b"\x89PNG\r\n\x1a\n...truncated upload...",
     None, "corrupt file (upload cut off)"),
    ("09-model-down", {"acquired": "2026-08-10T09:00:00Z"}, lambda: _png(_slice(9)),
     {"raise": "CT model service unavailable (HTTP 503)"}, "CT model service down"),
    ("10-other-model-version", {"acquired": "2026-09-10T09:00:00Z"}, lambda: _png(_slice(10)),
     {"probs": {"normal": 0.93, "cyst": 0.03, "stone": 0.02, "tumor": 0.02},
      "model_version": "stub-scripted-v2-untested"},
     "model updated, calibration not refitted"),
    ("11-mr-not-ct", {"acquired": "2026-10-01T09:00:00Z", "modality": "MR"}, lambda: _png(_slice(11)),
     None, "MR study (the Imaging Reader reads CT only)"),
]

PANEL = {
    "genomic_profile_id": "GEN-SB-2026-0042", "patient_id": PATIENT, "profiling_date": "2026-03-02",
    "profiling_method": "Targeted NGS Panel (NephroGenome v2.1)", "synthetic": True,
    "variants": [
        {"gene": "VHL", "variant_id": "rs5030827", "hgvs_notation": "c.499C>T (p.Arg167Trp)",
         "zygosity": "heterozygous",
         "clinical_significance": "pathogenic", "associated_condition": "von Hippel-Lindau disease"},
        {"gene": "FH", "variant_id": "rs121913123", "hgvs_notation": "c.1431_1433dup", "zygosity": "heterozygous",
         "clinical_significance": "Likely_pathogenic", "associated_condition": "Hereditary leiomyomatosis and RCC"},
        {"gene": "APOL1", "variant_id": "rs73885319", "hgvs_notation": "c.1024A>G (G1)", "zygosity": "homozygous",
         "clinical_significance": "risk_allele", "associated_condition": "APOL1-mediated kidney disease"},
        {"gene": "TP53", "variant_id": "rs28934578", "hgvs_notation": "c.524G>A (p.Arg175His)",
         "zygosity": "heterozygous",
         "clinical_significance": "pathogenic", "associated_condition": "Li-Fraumeni syndrome"},
        {"gene": "PBRM1", "variant_id": "rs1456789012", "hgvs_notation": "c.2134C>T (p.Pro712Ser)",
         "zygosity": "heterozygous", "clinical_significance": "Uncertain significance"},
        {"gene": "MET", "variant_id": "rs56391007", "hgvs_notation": "c.3029C>T", "zygosity": "heterozygous",
         "clinical_significance": "Conflicting interpretations of pathogenicity"},
        {"gene": "BAP1", "variant_id": "rs776543210", "hgvs_notation": "c.1892A>G", "zygosity": "heterozygous",
         "clinical_significance": "benign"},
    ],
}

OLD_VCF = """##fileformat=VCFv4.2
##fileDate=20230510
##panel_id=GEN-SB-2023-0007
##patient_id=sofia-b
##panel_method=Older 12-gene RCC panel
#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tSAMPLE
chr3\t10183812\trs5030827\tC\tT\t.\tPASS\tGENEINFO=VHL:7428;CLNSIG=Pathogenic\tGT\t0/1
chr2\t1\t.\tG\tA\t.\tPASS\tGENE=FLCN;CLNSIG=Benign\tGT\t0/0
"""

MISFILED_PANEL = {**PANEL, "genomic_profile_id": "GEN-MR-2026-0099", "patient_id": "marco-r",
                  "profiling_date": "2026-09-01", "variants": PANEL["variants"][:1]}


def _condition(code: str, display: str, onset: str) -> dict:
    return {"resource": {"resourceType": "Condition", "subject": {"reference": f"Patient/{PATIENT}"},
                         "code": {"coding": [{"system": "http://hl7.org/fhir/sid/icd-10", "code": code,
                                              "display": display}]}, "onsetDateTime": onset}}


def _med(text: str) -> dict:
    return {"resource": {"resourceType": "MedicationStatement", "subject": {"reference": f"Patient/{PATIENT}"},
                         "medicationCodeableConcept": {"text": text}, "status": "active"}}


def _obs(code: str, date: str, value: float, unit: str) -> dict:
    return {"resource": {"resourceType": "Observation", "status": "final", "code": {"text": code},
                         "subject": {"reference": f"Patient/{PATIENT}"}, "effectiveDateTime": date,
                         "valueQuantity": {"value": value, "unit": unit}}}


FHIR = {
    "resourceType": "Bundle", "type": "collection",
    "meta": {"tag": [{"code": "SYNTHETIC", "display": "Synthetic edge-case patient - no real data"}]},
    "entry": [
        {"resource": {"resourceType": "Patient", "id": PATIENT, "name": [{"family": "B.", "given": ["Sofia"]}],
                      "gender": "female", "birthDate": "1969-07-02"}},
        _condition("C64.2", "Malignant neoplasm of left kidney (clear-cell RCC), VHL carrier", "2023-06-01"),
        _condition("N18.3", "Chronic kidney disease, stage 3", "2025-02-11"),
        _condition("I10", "Essential (primary) hypertension", "2021-09-30"),
        _med("Sunitinib (TKI)"), _med("Amlodipine 5 mg"),
        _obs("eGFR", "2024-04-02", 68, "mL/min/1.73m2"), _obs("eGFR", "2025-02-11", 61, "mL/min/1.73m2"),
        _obs("eGFR", "2026-05-01", 55, "mL/min/1.73m2"),       # 164 days before the scenario: stale
        _obs("Creatinine", "2026-05-01", 1.18, "mg/dL"),
    ],
}

LAB_PHOTOS = {
    "lab-photo-ok.json": {"egfr": 49, "creatinine": 1.29, "date": "2026-10-09", "source": "patient photo (OCR)"},
    "lab-photo-ocr-error.json": {"egfr": 400, "creatinine": 1.29, "date": "2026-10-09",
                                 "source": "patient photo (OCR read 49 as 400)"},
}


def main() -> None:
    readme = (OUT / "README.md").read_text(encoding="utf-8") if (OUT / "README.md").exists() else None
    if OUT.exists():
        shutil.rmtree(OUT)
    scripted = {}
    for folder, meta, make_image, answer, what in STUDIES:
        d = OUT / f"ct-{folder}"
        d.mkdir(parents=True)
        image = (HERE / "marco-r" / "ct.png").read_bytes() if make_image is None else make_image()
        (d / "key.png").write_bytes(image)
        uid = f"2.25.{PATIENT.replace('-', '')}.{folder[:2]}"
        study = {"study_uid": uid, "modality": meta.get("modality", "CT"), "acquired": meta["acquired"],
                 "description": f"Synthetic: {what}", "source": "synthetic (kcd.fixtures.make_sofia)",
                 "key_image": "key.png"}
        (d / "study.json").write_text(json.dumps(study, indent=2) + "\n", encoding="utf-8")
        if answer is not None:
            scripted[uid] = {"folder": folder, "tests": what, **answer}
    (OUT / "ct-scripted-outputs.json").write_text(json.dumps(scripted, indent=2) + "\n", encoding="utf-8")
    (OUT / "panel-2026.json").write_text(json.dumps(PANEL, indent=2) + "\n", encoding="utf-8")
    (OUT / "panel-2023.vcf").write_text(OLD_VCF, encoding="utf-8", newline="\n")
    (OUT / "inbox").mkdir()
    (OUT / "inbox" / "misfiled-panel.json").write_text(json.dumps(MISFILED_PANEL, indent=2) + "\n", encoding="utf-8")
    (OUT / "fhir-bundle.json").write_text(json.dumps(FHIR, indent=2) + "\n", encoding="utf-8")
    for name, data in LAB_PHOTOS.items():
        (OUT / name).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    if readme is not None:  # hand-written, kept across rebuilds
        (OUT / "README.md").write_text(readme, encoding="utf-8")
    print(f"wrote {sum(1 for _ in OUT.rglob('*') if _.is_file())} files to {OUT}")


if __name__ == "__main__":
    main()
