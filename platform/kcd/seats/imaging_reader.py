"""IMAGING READER: CT key image -> preliminary imaging signal for the clinicians (deck slides 20, 22, 27).

Flow for a new study:
  PACS (read-only) -> evidence (key image, hashed) -> image quality check -> CT model
  -> calibration -> prediction OOD check -> heat map overlay (evidence) -> imaging_signal
  to the Dispatcher (route: Safety Verifier, Case Briefer) -> radiologist_review gate.

It never stays silent: an unreadable image or a failed model still produces a signal with
status "unreadable" / "model_error", so the radiologist knows a study is waiting.
Refusals: label_as_diagnosis (fixed note + text guard), report_to_patient (only the
Dispatcher is a peer), alter_image (read-only PACS, the key image is stored as evidence
with its sha256 and never rewritten).
"""
from __future__ import annotations

import numpy as np

from kcd.contracts import CT_LABELS
from kcd.cortex import Cortex, CortexView
from kcd.imaging import heatmap as hm
from kcd.imaging.calibration import Calibration, softmax
from kcd.imaging.image_io import ImageDecodeError, LoadedImage, load_image
from kcd.imaging.model import CtModel, CtPrediction
from kcd.imaging.pacs import PacsSource, StudyRef
from kcd.imaging.quality import QualityThresholds, check_image, check_prediction
from kcd.seatkit import GateOpener, Manifest, Outbox, Seat
from kcd.util import sha256_hex

NOTE = ("Preliminary model signal for radiologist review. It is not a diagnosis and is not shared "
        "with the patient.")
ROUTE = ["safety_verifier", "case_briefer"]


class NoStudy(LookupError):
    pass


class ImagingReader(Seat):
    def __init__(self, cortex: Cortex, outbox: Outbox, gates: GateOpener, pacs: PacsSource, model: CtModel,
                 calibration: Calibration | None, *, manifest: Manifest | None = None,
                 thresholds: QualityThresholds = QualityThresholds()):
        super().__init__(manifest or Manifest.load("imaging_reader"), outbox, gates)
        self.memory = CortexView(cortex, self.manifest)
        self.pacs = pacs
        self.model = model
        self.calibration = calibration
        self.thresholds = thresholds
        self.check_text(NOTE)

    # ------------------------------------------------------------------ entry point
    def process_study(self, patient: str, study_uid: str | None = None, *, correlation: str | None = None,
                      force: bool = False) -> dict:
        study = self._find_study(patient, study_uid)
        key = f"study:{study.study_uid}"
        if not force:
            previous = self.memory.processed_result(key)
            if previous is not None:
                self.audit("imaging.process_study", "duplicate_skipped", study_uid=study.study_uid)
                return {**previous, "duplicate": True}

        self.require_tool("pacs.fetch_study")
        image_bytes, media_type = self.pacs.fetch_key_image(study)
        image_sha = sha256_hex(image_bytes)
        key_ev = self.memory.append_evidence(
            3, patient, kind="ct_key_image", source=f"pacs:{study.source or type(self.pacs).__name__}",
            content={"study_uid": study.study_uid, "modality": study.modality, "acquired": study.acquired,
                     "description": study.description, "media_type": media_type, "sha256": image_sha},
            blob=image_bytes)
        provenance = {"study_uid": study.study_uid, "modality": study.modality, "acquired": study.acquired,
                      "source": study.source, "key_image_sha256": image_sha, "key_image_evidence_id": key_ev["id"],
                      "model_version": None}

        payload = self._analyse(patient, image_bytes, media_type, key_ev["id"], provenance)
        self.check_text(payload["note"])

        env = self.send("imaging_signal", patient, payload, correlation=correlation, route_hint=ROUTE)
        signal_ev = self.memory.append_evidence(3, patient, kind="imaging_signal", content=payload,
                                                meta={"message_id": env.id})
        gate_id = self.open_gate("radiologist_review", patient,
                                 f"Review CT signal for study {study.study_uid} ({payload['status']})",
                                 {"signal_evidence_id": signal_ev["id"], "study_uid": study.study_uid,
                                  "status": payload["status"], "label": payload["label"],
                                  "p_calibrated": payload["p_calibrated"], "heatmap_ref": payload["heatmap_ref"]})
        result = {"message_id": env.id, "signal_evidence_id": signal_ev["id"], "gate_id": gate_id,
                  "payload": payload}
        if not force:
            self.memory.mark_processed(key, 3, patient, result)
        self.audit("imaging.process_study", "done", study_uid=study.study_uid, status=payload["status"])
        return {**result, "duplicate": False}

    # ------------------------------------------------------------------ steps
    def _find_study(self, patient: str, study_uid: str | None) -> StudyRef:
        self.require_tool("pacs.list_studies")
        studies = [s for s in self.pacs.list_studies(patient) if s.modality.upper() == "CT"]
        if study_uid is not None:
            studies = [s for s in studies if s.study_uid == study_uid]
        if not studies:
            raise NoStudy(f"no CT study {'with uid ' + study_uid if study_uid else ''} for patient {patient}".strip())
        return studies[0]  # list_studies returns the most recent first

    def _analyse(self, patient: str, image_bytes: bytes, media_type: str, key_ev_id: str,
                 provenance: dict) -> dict:
        self.require_tool("ct.quality_ood_check")
        try:
            image = load_image(image_bytes)
        except ImageDecodeError as e:
            quality = {"ok": False, "checks": [{"name": "decodable", "ok": False, "value": 0, "limit": "decodes"}],
                       "error": str(e)}
            return self._empty_signal("unreadable", quality, provenance)

        quality = check_image(image, self.thresholds)
        if not quality["ok"]:
            return self._empty_signal("unreadable", quality, provenance)

        self.require_tool("ct.classify")
        try:
            pred = self.model.predict(image, image_bytes, media_type)
        except Exception as e:  # any model failure becomes a visible signal, never a silent drop
            quality = {**quality, "error": f"model failed: {type(e).__name__}: {e}"}
            return self._empty_signal("model_error", quality, provenance)
        provenance = {**provenance, "model_version": pred.model_version}

        calibration_info, probs = self._calibrate(pred)
        raw = softmax(pred.logits)
        top = int(np.argmax(probs))
        pred_checks = check_prediction(probs, self.thresholds)
        quality = {"ok": all(c["ok"] for c in pred_checks), "checks": quality["checks"] + pred_checks}

        heatmap_ref, heatmap_sha, metrics = self._heatmap(patient, image, pred, key_ev_id)
        return {
            "status": "ok" if quality["ok"] else "out_of_distribution",
            "label": CT_LABELS[top],
            "p_raw": round(float(raw[top]), 4),
            "p_calibrated": round(float(probs[top]), 4),
            "probabilities": {label: round(float(p), 4) for label, p in zip(CT_LABELS, probs)},
            "heatmap_ref": heatmap_ref,
            "heatmap_sha256": heatmap_sha,
            "cam_metrics": metrics,
            "quality": quality,
            "calibration": calibration_info,
            "provenance": provenance,
            "note": NOTE,
        }

    def _calibrate(self, pred: CtPrediction) -> tuple[dict, np.ndarray]:
        cal = self.calibration
        if cal is None:
            return {"status": "uncalibrated", "temperature": 1.0}, softmax(pred.logits)
        if cal.model_version != pred.model_version:
            # A temperature fitted for another model version is meaningless: report raw confidence.
            return ({"status": "stale", "temperature": 1.0, "fitted_for": cal.model_version},
                    softmax(pred.logits))
        return ({"status": "calibrated", "temperature": cal.temperature, "fitted_for": cal.model_version,
                 "ece_after": cal.ece_after}, cal.apply(pred.logits))

    def _heatmap(self, patient: str, image: LoadedImage, pred: CtPrediction,
                 key_ev_id: str) -> tuple[str | None, str | None, dict | None]:
        if pred.heatmap is None:
            return None, None, None
        heat = hm.resize_to(hm.normalize(pred.heatmap), image.width, image.height)
        metrics = hm.cam_metrics(heat)
        png = hm.overlay_png(image.gray, heat)
        ev = self.memory.append_evidence(
            3, patient, kind="ct_heatmap_overlay",
            content={"key_image_evidence_id": key_ev_id, "model_version": pred.model_version,
                     "media_type": "image/png", "cam_metrics": metrics},
            blob=png)
        return f"cortex:evidence/{ev['id']}", ev["blob_sha256"], metrics

    @staticmethod
    def _empty_signal(status: str, quality: dict, provenance: dict) -> dict:
        return {"status": status, "label": "indeterminate", "p_raw": None, "p_calibrated": None,
                "probabilities": None, "heatmap_ref": None, "heatmap_sha256": None, "cam_metrics": None,
                "quality": quality, "calibration": None, "provenance": provenance, "note": NOTE}
