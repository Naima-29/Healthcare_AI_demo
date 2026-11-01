"""Contract C2: the 10 A2A message types and their payloads.

Field names follow the v2 demo (`demo/kidney-care-desk/contracts/a2aSchema.mjs`) so its golden-path messages
validate here; Core adds the fields the real system needs (hashes, provenance, structured flags).

Payloads are strict (extra="forbid"): a field nobody declared is rejected, which is what stops a seat from
smuggling e.g. `verification: {passed: true}` past the Safety Verifier. Adding an optional field is a minor
version bump; renaming or removing one is a major bump.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class MessageType(StrEnum):
    SYMPTOM_REPORT = "symptom_report"
    RED_FLAG_ESCALATION = "red_flag_escalation"
    MEDICATION_REPORT = "medication_report"
    DATA_REQUEST = "data_request"
    DATA_DELIVERED = "data_delivered"
    IMAGING_SIGNAL = "imaging_signal"
    THERAPY_CONFLICT = "therapy_conflict"
    CASE_PACK_READY = "case_pack_ready"
    CLINICIAN_DECISION = "clinician_decision"
    ACKNOWLEDGMENT = "acknowledgment"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


Probability = Annotated[float, Field(ge=0.0, le=1.0)]
Sha256 = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
Citation = Annotated[str, Field(min_length=3, description="Knowledge Model citation, e.g. '[14] KDIGO 2024 ...'")]


class EvidenceRef(_Strict):
    kind: Literal["message", "observation", "study", "document", "variant", "series", "evidence"]
    ref: str


# --- symptom_report -----------------------------------------------------------


class Symptom(_Strict):
    code: str
    label: str
    severity: int | None = Field(default=None, ge=0, le=10)
    onset: str | None = None
    source_text: str | None = Field(default=None, description="Patient wording that produced this finding")


class Distress(_Strict):
    score: Probability
    level: Literal["low", "moderate", "high"]
    face: Any = Field(default=None, description="Facial features (e.g. PSPI) behind the score")
    voice: Any = None
    verbal: Any = None


class SymptomReport(_Strict):
    symptoms: list[Symptom] = Field(min_length=1)
    distress: Distress
    underreporting: bool = False
    transcript: str | None = Field(default=None, max_length=2000)
    language: str | None = Field(default=None, pattern=r"^[a-z]{2}$")
    channel: Literal["telegram", "whatsapp", "app"] | None = None
    evidence: list[EvidenceRef] = Field(default_factory=list)


# --- red_flag_escalation ------------------------------------------------------


class RedFlag(_Strict):
    code: str
    detected_by: Literal["rules", "model", "both"]
    rule_id: str | None = None


class RedFlagEscalation(_Strict):
    urgency: Literal["high", "medium"]
    pattern: str = Field(description="Red-flag pattern from the Knowledge Model")
    evidence: list[str] = Field(min_length=1)
    safety_script_sent: bool = False
    citations: list[Citation] = Field(default_factory=list)
    unit: str | None = Field(default=None, description="On-call unit to page; the Dispatcher's default if absent")
    flags: list[RedFlag] = Field(default_factory=list, description="Which detector fired (rules, model or both)")


# --- medication_report --------------------------------------------------------


class MedicationReport(_Strict):
    drug: str
    drug_class: str = Field(alias="class", description="Drug class used by the conflict rules, e.g. 'NSAID'")
    dose_text: str | None = None
    frequency: str | None = None
    reason: str | None = None
    rxnorm: str | None = None
    source: Literal["patient_reported", "fhir"] = "patient_reported"


# --- data_request / data_delivered -------------------------------------------


class RequestedItem(_Strict):
    kind: Literal["lab", "imaging", "vitals", "genomics"]
    code: str
    max_age_days: int | None = Field(default=None, ge=0)


class DataRequest(_Strict):
    what: str
    why: str
    ask_patient: str | None = Field(
        default=None, description="Set when the patient should be asked (routes to Companion)"
    )
    items: list[RequestedItem] = Field(default_factory=list)


class DataDelivered(_Strict):
    kind: str = Field(pattern=r"^[a-z][a-z_]{1,40}$", description="lab, genomic_context, wearable_series, ...")
    data: dict[str, Any]
    evidence_id: str | None = None
    citations: list[Citation] = Field(default_factory=list)
    evidence: list[EvidenceRef] = Field(default_factory=list)


# --- imaging_signal -----------------------------------------------------------


class ImageQuality(_Strict):
    quality: Literal["adequate", "limited"]
    issues: list[str] = Field(default_factory=list)
    ood_score: float | None = Field(default=None, ge=0.0)
    in_distribution: bool


class ImagingProvenance(_Strict):
    study_uid: str
    model: str
    modality: str | None = None
    acquired: str | None = None
    live_model: bool | None = None
    source: str | None = None


class ImagingSignal(_Strict):
    label: Literal["normal", "cyst", "tumor", "stone"]
    p_raw: Probability | None = None
    p_calibrated: Probability
    cam_metrics: dict[str, float] = Field(default_factory=dict)
    quality: ImageQuality
    provenance: ImagingProvenance
    heatmap_ref: str | None = None
    key_image: str | None = None
    note: str | None = Field(default=None, description="Technical note for the radiologist; never a diagnosis")
    calibration: str | None = None


# --- therapy_conflict ---------------------------------------------------------


class TherapyConflict(_Strict):
    drug: str
    conditions: list[str] = Field(min_length=1)
    severity: Literal["high", "moderate", "low"]
    rationale: str
    citations: list[Citation] = Field(min_length=1)
    data_freshness: dict[str, Any] = Field(default_factory=dict)
    egfr_series: list[dict[str, Any]] = Field(default_factory=list)
    rule_id: str | None = None
    options: list[str] = Field(default_factory=list)
    evidence: list[EvidenceRef] = Field(default_factory=list)


# --- case_pack_ready / clinician_decision ------------------------------------


class Sbar(_Strict):
    S: list[str]
    B: list[str]
    A: list[str]
    R: list[str]
    narrative: str | None = None


class CasePackReady(_Strict):
    case_id: str
    sbar: Sbar
    artifact_hash: Sha256 = Field(description="Hash of the case pack version; the clinician's release binds to it")
    version: int = Field(default=1, ge=1)
    gates: list[str] = Field(default_factory=lambda: ["clinician_signoff"])


class DecisionItem(_Strict):
    code: str
    patient_text: str = Field(description="Exactly what the Companion may tell the patient, as signed")
    patient_text_en: str | None = None
    clinician_text: str | None = None


class ClinicianDecision(_Strict):
    case_id: str
    decision_id: str = Field(description="Id of the signed release")
    artifact_hash: Sha256
    signed_by: str
    role: str | None = None
    note: str | None = None
    items: list[DecisionItem] = Field(min_length=1)
    signature: str | None = None


# --- acknowledgment -----------------------------------------------------------


class Acknowledgment(_Strict):
    ack_of: str
    status: Literal["received", "routed", "accepted", "rejected"]
    reason: str | None = None


PAYLOADS: dict[MessageType, type[BaseModel]] = {
    MessageType.SYMPTOM_REPORT: SymptomReport,
    MessageType.RED_FLAG_ESCALATION: RedFlagEscalation,
    MessageType.MEDICATION_REPORT: MedicationReport,
    MessageType.DATA_REQUEST: DataRequest,
    MessageType.DATA_DELIVERED: DataDelivered,
    MessageType.IMAGING_SIGNAL: ImagingSignal,
    MessageType.THERAPY_CONFLICT: TherapyConflict,
    MessageType.CASE_PACK_READY: CasePackReady,
    MessageType.CLINICIAN_DECISION: ClinicianDecision,
    MessageType.ACKNOWLEDGMENT: Acknowledgment,
}
