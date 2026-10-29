"""Contract C2: the 10 A2A message types and their payloads.

Payloads are strict (extra="forbid"). Adding an optional field is a minor version bump;
renaming or removing one is a major bump.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class MessageType(StrEnum):
    SYMPTOM_REPORT = "symptom_report"
    MEDICATION_REPORT = "medication_report"
    RED_FLAG_ESCALATION = "red_flag_escalation"
    DATA_REQUEST = "data_request"
    DATA_DELIVERED = "data_delivered"
    IMAGING_SIGNAL = "imaging_signal"
    THERAPY_CONFLICT = "therapy_conflict"
    CASE_PACK_READY = "case_pack_ready"
    CLINICIAN_DECISION = "clinician_decision"
    ACKNOWLEDGMENT = "acknowledgment"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


Probability = Annotated[float, Field(ge=0.0, le=1.0)]


class Citation(_Strict):
    source: str = Field(description="Guideline or rule source, e.g. 'KDIGO 2024'")
    ref: str = Field(description="Section, rule id or URL")


class EvidenceRef(_Strict):
    kind: Literal["message", "observation", "study", "document", "variant", "series"]
    ref: str


# --- payloads -----------------------------------------------------------------


class Symptom(_Strict):
    code: str = Field(description="Internal or SNOMED code")
    label: str
    severity: int = Field(ge=0, le=10)
    onset: str | None = None
    source_text: str | None = Field(default=None, description="Patient wording that produced this finding")


class SymptomReport(_Strict):
    symptoms: list[Symptom] = Field(min_length=1)
    language: str = Field(pattern=r"^[a-z]{2}$")
    channel: Literal["telegram", "whatsapp", "app"]
    distress_score: Probability | None = None
    evidence: list[EvidenceRef] = Field(default_factory=list)


class Medication(_Strict):
    name: str
    rxnorm: str | None = None
    dose: str | None = None
    frequency: str | None = None
    source: Literal["patient_reported", "fhir"]


class MedicationReport(_Strict):
    medications: list[Medication] = Field(min_length=1)
    evidence: list[EvidenceRef] = Field(default_factory=list)


class RedFlag(_Strict):
    code: str
    detected_by: Literal["rules", "model", "both"]
    rule_id: str | None = None
    evidence: str


class RedFlagEscalation(_Strict):
    flags: list[RedFlag] = Field(min_length=1)
    severity: Literal["critical", "urgent"]
    unit: str = Field(description="Care unit whose on-call rota is paged, e.g. 'nephrology'")
    safety_script_sent: bool


class RequestedItem(_Strict):
    kind: Literal["lab", "imaging", "vitals", "genomics"]
    code: str
    max_age_days: int | None = Field(default=None, ge=0)


class DataRequest(_Strict):
    items: list[RequestedItem] = Field(min_length=1)
    reason: str


class DataDelivered(_Strict):
    kind: Literal["lab", "vitals", "wearable_series", "genomic_context", "document"]
    items: list[dict[str, object]] = Field(min_length=1)
    citations: list[Citation] = Field(default_factory=list)
    evidence: list[EvidenceRef] = Field(default_factory=list)
    in_reply_to: UUID | None = None


class ImagingSignal(_Strict):
    study_uid: str
    label: Literal["normal", "cyst", "tumor", "stone"]
    p_raw: Probability
    p_calibrated: Probability
    heatmap_ref: str
    quality: Literal["ok", "low", "out_of_distribution"]
    model_version: str
    technical_note: str | None = None


class TherapyConflict(_Strict):
    drug: str
    condition: str
    rule_id: str
    severity: Literal["contraindicated", "caution", "monitor"]
    explanation: str
    options: list[str] = Field(default_factory=list)
    citations: list[Citation] = Field(min_length=1)
    evidence: list[EvidenceRef] = Field(default_factory=list)


class CasePackReady(_Strict):
    artifact_id: str
    artifact_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    gate_role: str = Field(description="Signer role the gate is opened for, e.g. 'clinician:nephrology'")
    summary: str


class ClinicianDecision(_Strict):
    artifact_id: str
    artifact_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    release_id: str
    decision: Literal["approved", "edited", "rejected"]
    clinician_role: str
    patient_explanation_points: list[str] = Field(default_factory=list)


class Acknowledgment(_Strict):
    ack_of: UUID
    status: Literal["received", "accepted", "rejected"]
    reason: str | None = None


PAYLOADS: dict[MessageType, type[BaseModel]] = {
    MessageType.SYMPTOM_REPORT: SymptomReport,
    MessageType.MEDICATION_REPORT: MedicationReport,
    MessageType.RED_FLAG_ESCALATION: RedFlagEscalation,
    MessageType.DATA_REQUEST: DataRequest,
    MessageType.DATA_DELIVERED: DataDelivered,
    MessageType.IMAGING_SIGNAL: ImagingSignal,
    MessageType.THERAPY_CONFLICT: TherapyConflict,
    MessageType.CASE_PACK_READY: CasePackReady,
    MessageType.CLINICIAN_DECISION: ClinicianDecision,
    MessageType.ACKNOWLEDGMENT: Acknowledgment,
}
