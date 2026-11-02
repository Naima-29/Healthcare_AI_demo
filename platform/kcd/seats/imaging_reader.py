"""IMAGING READER on kcd_core: CT key image -> preliminary imaging signal (deck slides 20, 22, 27).

A new study is an event from PACS, not an A2A message, so a PACS watcher calls
`process_study(ctx, ...)` with a context from this seat's runtime (`runtime.context()`):

  pacs.list_studies / pacs.fetch_study (read-only) -> key image as evidence (patient layer 3)
  -> ct.quality_ood_check -> ct.classify -> calibration -> prediction OOD check
  -> heat-map overlay (evidence) -> full details (evidence) -> imaging_signal to the Safety Verifier

ImagingSignal.quality (contract C2):
  quality          "adequate", or "limited" when an image check failed (issues lists which)
  in_distribution  False when the model is unsure (low top probability or high entropy)
  ood_score        normalized entropy of the calibrated probabilities (0 = certain, 1 = uniform)

When there is no model result at all (undecodable image, model failure) the contract has no way to
say so (label and p_calibrated are required), so nothing is sent: the failure is written to evidence
and audit (decision "error") and returned to the caller. See the proposal in platform/kcd/README.md.

Gate: radiologist_review (manifest), opened on the case pack downstream.
Refusals: label_as_diagnosis (text guard + ctx.guard_content("diagnosis_claim")), report_to_patient
(only peer is the Safety Verifier), alter_image (no PACS write tool; key image stored with its sha256).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
from kcd_core.audit import AuditSink
from kcd_core.clock import Clock
from kcd_core.contracts import Envelope, MessageType
from kcd_core.contracts.messages import ImageQuality, ImagingProvenance, ImagingSignal
from kcd_core.pep import PolicyViolation
from kcd_core.runtime import Seat, SeatContext
from pydantic import BaseModel

from kcd.cortex import CoreMemory, Cortex
from kcd.imaging import heatmap as hm
from kcd.imaging.calibration import Calibration, softmax
from kcd.imaging.image_io import ImageDecodeError, LoadedImage, load_image
from kcd.imaging.model import CT_LABELS, CtModelError, CtPrediction
from kcd.imaging.pacs import StudyRef
from kcd.imaging.quality import QualityThresholds, check_prediction
from kcd.textguard import is_diagnosis_claim
from kcd.util import sha256_hex

NOTE = ("Preliminary model signal for radiologist review. It is not a diagnosis and is not shared "
        "with the patient.")
PEER = "safety_verifier"


class NoStudy(LookupError):
    pass


@dataclass
class ImagingResult:
    status: str                         # ok | limited | out_of_distribution | unreadable | model_error
    sent: bool
    message_id: str | None
    details_evidence_id: str
    details: dict[str, Any] = field(repr=False)
    duplicate: bool = False


class ImagingReader(Seat):
    seat_id = "imaging_reader"

    def __init__(self, cortex: Cortex, calibration: Calibration | None, *, audit: AuditSink, clock: Clock,
                 thresholds: QualityThresholds | None = None):
        self.cortex = cortex
        self.calibration = calibration
        self.audit = audit  # the same sink and clock the seat's runtime uses
        self.clock = clock
        self.thresholds = thresholds or QualityThresholds()
        if is_diagnosis_claim(NOTE):  # the fixed note must itself pass the guard
            raise ValueError("imaging note reads as a diagnosis")

    async def handle(self, env: Envelope, payload: BaseModel, ctx: SeatContext) -> None:
        """Inbound types are acknowledgments (Dispatcher, Safety Verifier). A rejection is audited by Core."""

    # ------------------------------------------------------------------ entry point
    async def process_study(self, ctx: SeatContext, *, patient_id: str, episode_id: str,
                            study_uid: str | None = None, force: bool = False) -> ImagingResult:
        mem = CoreMemory(self.cortex, ctx, self.seat_id, patient_id)
        study = await self._find_study(ctx, patient_id, study_uid)
        key = f"study:{study.study_uid}"
        if not force:
            previous = mem.processed_result(key)
            if previous is not None:
                return ImagingResult(**previous, duplicate=True)

        fetched = await ctx.tool("pacs.fetch_study", study=study)
        image_bytes, media_type = fetched["data"], fetched["media_type"]
        image_sha = sha256_hex(image_bytes)
        key_ev = mem.append_evidence(
            kind="ct_key_image", source=f"pacs:{study.source or 'pacs'}", blob=image_bytes,
            content={"study_uid": study.study_uid, "modality": study.modality, "acquired": study.acquired,
                     "description": study.description, "media_type": media_type, "sha256": image_sha})
        provenance = {"study_uid": study.study_uid, "modality": study.modality, "acquired": study.acquired,
                      "source": study.source, "key_image_sha256": image_sha, "key_image_evidence_id": key_ev["id"]}

        details = await self._analyse(ctx, mem, image_bytes, media_type, key_ev["id"], provenance)
        details_ev = mem.append_evidence(kind="imaging_signal_details", content=details)

        if details["status"] in ("unreadable", "model_error"):
            self.audit.record(  # loud, not silent: the radiologist's queue reads these
                ts=self.clock.now(), actor=self.seat_id, manifest_version=ctx.manifest.ref,
                action="imaging.no_signal", decision="error", patient_id=patient_id,
                detail={"study_uid": study.study_uid, "status": details["status"],
                        "details_evidence_id": details_ev["id"], "error": details.get("error")})
            result = ImagingResult(details["status"], False, None, details_ev["id"], details)
        else:
            note = f"{NOTE} Details: cortex:evidence/{details_ev['id']}"
            if is_diagnosis_claim(note):
                ctx.guard_content("diagnosis_claim")  # no release for a model signal: the PEP blocks it
            cal = details["calibration"]
            signal = ImagingSignal(
                label=details["label"], p_raw=details["p_raw"], p_calibrated=details["p_calibrated"],
                cam_metrics=_numeric(details["cam_metrics"]),
                quality=ImageQuality(quality="limited" if details["issues"] else "adequate", issues=details["issues"],
                                     ood_score=details["ood_score"], in_distribution=details["in_distribution"]),
                provenance=ImagingProvenance(study_uid=study.study_uid, model=details["model_version"],
                                             modality=study.modality or None, acquired=study.acquired or None,
                                             live_model="stub" not in details["model_version"],
                                             source=study.source or None),
                heatmap_ref=details["heatmap_ref"], key_image=f"cortex:evidence/{key_ev['id']}", note=note,
                calibration=f"temperature scaling T={cal['temperature']} ({cal['status']})")
            env = await ctx.send(MessageType.IMAGING_SIGNAL, PEER, signal, patient_id=patient_id,
                                 episode_id=episode_id)
            result = ImagingResult(details["status"], True, str(env.id), details_ev["id"], details)

        if not force:
            mem.mark_processed(key, {"status": result.status, "sent": result.sent, "message_id": result.message_id,
                                     "details_evidence_id": result.details_evidence_id, "details": details})
        return result

    # ------------------------------------------------------------------ steps
    async def _find_study(self, ctx: SeatContext, patient_id: str, study_uid: str | None) -> StudyRef:
        studies = [s for s in await ctx.tool("pacs.list_studies", patient_id=patient_id)
                   if s.modality.upper() == "CT"]
        if study_uid is not None:
            studies = [s for s in studies if s.study_uid == study_uid]
        if not studies:
            which = f"with uid {study_uid} " if study_uid else ""
            raise NoStudy(f"no CT study {which}for patient {patient_id}")
        return studies[0]  # list_studies returns the most recent first

    async def _analyse(self, ctx: SeatContext, mem: CoreMemory, image_bytes: bytes, media_type: str,
                       key_ev_id: str, provenance: dict) -> dict:
        try:
            image = load_image(image_bytes)
        except ImageDecodeError as e:
            return self._no_result("unreadable", [], provenance, f"cannot decode: {e}")

        image_quality = await ctx.tool("ct.quality_ood_check", image_bytes=image_bytes)
        issues = [c["name"] for c in image_quality["checks"] if not c["ok"]]
        try:
            pred = await ctx.tool("ct.classify", image_bytes=image_bytes, media_type=media_type)
        except PolicyViolation:
            raise  # a blocked tool is a policy decision, never a model failure
        except (CtModelError, ImageDecodeError, OSError, ValueError, RuntimeError, LookupError) as e:
            return self._no_result("model_error", image_quality["checks"], provenance, f"{type(e).__name__}: {e}")
        if not isinstance(pred, CtPrediction):
            return self._no_result("model_error", image_quality["checks"], provenance,
                                   "ct.classify returned no prediction")

        calibration, probs = self._calibrate(pred)
        raw = softmax(pred.logits)
        top = int(np.argmax(probs))
        pred_checks = check_prediction(probs, self.thresholds)
        in_distribution = all(c["ok"] for c in pred_checks)
        ood_score = next(c["value"] for c in pred_checks if c["name"] == "normalized_entropy")
        status = "out_of_distribution" if not in_distribution else ("limited" if issues else "ok")
        heatmap_ref, heatmap_sha, metrics = self._heatmap(mem, image, pred, key_ev_id)
        return {
            "status": status, "label": CT_LABELS[top],
            "p_raw": round(float(raw[top]), 4), "p_calibrated": round(float(probs[top]), 4),
            "probabilities": {label: round(float(p), 4) for label, p in zip(CT_LABELS, probs, strict=True)},
            "model_version": pred.model_version, "heatmap_ref": heatmap_ref, "heatmap_sha256": heatmap_sha,
            "cam_metrics": metrics, "quality_checks": image_quality["checks"] + pred_checks, "issues": issues,
            "in_distribution": in_distribution, "ood_score": round(float(ood_score), 4),
            "calibration": calibration, "provenance": {**provenance, "model_version": pred.model_version},
            "note": NOTE,
        }

    def _calibrate(self, pred: CtPrediction) -> tuple[dict, np.ndarray]:
        cal = self.calibration
        if cal is None:
            return {"status": "uncalibrated", "temperature": 1.0}, softmax(pred.logits)
        if cal.model_version != pred.model_version:
            # A temperature fitted for another model version is meaningless: report raw confidence.
            return {"status": "stale", "temperature": 1.0, "fitted_for": cal.model_version}, softmax(pred.logits)
        return ({"status": "calibrated", "temperature": cal.temperature, "fitted_for": cal.model_version,
                 "ece_after": cal.ece_after}, cal.apply(pred.logits))

    def _heatmap(self, mem: CoreMemory, image: LoadedImage, pred: CtPrediction,
                 key_ev_id: str) -> tuple[str | None, str | None, dict | None]:
        if pred.heatmap is None:
            return None, None, None
        heat = hm.resize_to(hm.normalize(pred.heatmap), image.width, image.height)
        metrics = hm.cam_metrics(heat)
        ev = mem.append_evidence(
            kind="ct_heatmap_overlay", blob=hm.overlay_png(image.gray, heat),
            content={"key_image_evidence_id": key_ev_id, "model_version": pred.model_version,
                     "media_type": "image/png", "cam_metrics": metrics})
        return f"cortex:evidence/{ev['id']}", ev["blob_sha256"], metrics

    @staticmethod
    def _no_result(status: str, checks: list, provenance: dict, error: str) -> dict:
        return {"status": status, "label": None, "p_raw": None, "p_calibrated": None, "probabilities": None,
                "model_version": None, "heatmap_ref": None, "heatmap_sha256": None, "cam_metrics": None,
                "quality_checks": checks, "issues": [c["name"] for c in checks if not c["ok"]],
                "in_distribution": None, "ood_score": None, "calibration": None, "provenance": provenance,
                "note": NOTE, "error": error}


def _numeric(metrics: dict | None) -> dict[str, float]:
    """ImagingSignal.cam_metrics holds numbers only (no flags or coordinates)."""
    if not metrics:
        return {}
    return {k: float(v) for k, v in metrics.items() if isinstance(v, int | float) and not isinstance(v, bool)}
