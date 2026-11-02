"""Stand-in seats for the parts of the desk that have no branch yet. Replace them, don't build on them.

- StandInCompanion   for Person 2 (Meet):     the patient's side, scripted (no Telegram, NLU or humanized AI)
- StandInCaseBriefer for Person 5 (Chrislin): collects verified outputs, builds a plain SBAR case pack, and
                                              publishes a clinician's signed decision

They are real kcd_core seats: their messages are signed, routed by the Dispatcher and checked by the PEP against
the real companion / case_briefer manifests, so the rest of the desk runs exactly as it will with the real
seats. Only their behaviour is scripted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from kcd_core.audit import content_hash
from kcd_core.contracts import Envelope, MessageType, Release
from kcd_core.contracts.messages import (
    CasePackReady,
    ClinicianDecision,
    DataDelivered,
    DataRequest,
    DecisionItem,
    ImagingSignal,
    MedicationReport,
    RedFlagEscalation,
    Sbar,
    TherapyConflict,
)
from kcd_core.pep import PolicyViolation
from kcd_core.runtime import Seat, SeatContext
from kcd_core.signing import SignatureError, verify_release
from pydantic import BaseModel

from kcd.knowledge import KnowledgeModel


class StandInCompanion(Seat):
    """Scripted patient side. `lab_reports` maps patient id -> what the patient's lab photo says (after OCR)."""

    seat_id = "companion"

    def __init__(self, km: KnowledgeModel, lab_reports: dict[str, dict[str, Any]] | None = None):
        self.km = km
        self.lab_reports = dict(lab_reports or {})
        self.told_patient: list[dict[str, Any]] = []  # what the Companion would say, as signed by the clinician
        self.asked_patient: list[str] = []

    async def handle(self, env: Envelope, payload: BaseModel, ctx: SeatContext) -> None:
        if isinstance(payload, DataRequest) and payload.ask_patient:
            self.asked_patient.append(payload.ask_patient)
            lab = self.lab_reports.get(env.patient_id)
            if lab is not None:  # the patient sends the photo; on-device OCR reads it
                await ctx.send(MessageType.DATA_DELIVERED, "renal_guardian", DataDelivered(kind="lab", data=lab))
                await ctx.send(MessageType.DATA_DELIVERED, "archivist", DataDelivered(kind="lab", data=lab))
        elif isinstance(payload, ClinicianDecision):
            # Only the clinician's signed wording reaches the patient.
            self.told_patient += [{"code": i.code, "text": i.patient_text} for i in payload.items]

    async def report_medication(self, ctx: SeatContext, *, patient_id: str, episode_id: str, text: str) -> Envelope:
        drug = self.km.classify_drug(text)
        if drug is None:
            raise ValueError(f"no known drug in {text!r}")
        return await ctx.send(MessageType.MEDICATION_REPORT, "renal_guardian",
                              MedicationReport(drug=drug["drug"], **{"class": drug["class"]}, dose_text=text),
                              patient_id=patient_id, episode_id=episode_id)

    async def escalate(self, ctx: SeatContext, *, patient_id: str, episode_id: str, red_flag: dict[str, Any],
                       unit: str | None = None) -> None:
        payload = RedFlagEscalation(urgency=red_flag["urgency"], pattern=red_flag["pattern"],
                                    evidence=red_flag["evidence"], citations=red_flag["citations"],
                                    safety_script_sent=True, unit=unit)
        for to in ("case_briefer", "archivist"):
            await ctx.send(MessageType.RED_FLAG_ESCALATION, to, payload, patient_id=patient_id, episode_id=episode_id)


@dataclass
class _Case:
    case_id: str
    episode_id: str
    received: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    version: int = 0
    artifact_hash: str | None = None
    sbar: Sbar | None = None


class StandInCaseBriefer(Seat):
    """Collects what reaches it (verified outputs, red flags) per patient and builds a plain SBAR case pack."""

    seat_id = "case_briefer"

    def __init__(self) -> None:
        self.cases: dict[str, _Case] = {}

    def case(self, patient_id: str, episode_id: str) -> _Case:
        if patient_id not in self.cases:
            self.cases[patient_id] = _Case(f"case-{patient_id}-1", episode_id)
        return self.cases[patient_id]

    async def handle(self, env: Envelope, payload: BaseModel, ctx: SeatContext) -> None:
        if isinstance(payload, ImagingSignal | TherapyConflict | DataDelivered | RedFlagEscalation):
            self.case(env.patient_id, env.episode_id).received.append(
                (str(env.type), payload.model_dump(mode="json", by_alias=True)))

    async def request_genomics(self, ctx: SeatContext, *, patient_id: str, episode_id: str) -> None:
        await ctx.send(MessageType.DATA_REQUEST, "genomics_interpreter",
                       DataRequest(what="genomic_context", why="case pack: hereditary context"),
                       patient_id=patient_id, episode_id=episode_id)

    async def publish_case_pack(self, ctx: SeatContext, *, patient_id: str) -> CasePackReady:
        c = self.cases[patient_id]
        c.version += 1
        c.sbar = _sbar(c.received)
        c.artifact_hash = content_hash({"case_id": c.case_id, "version": c.version,
                                        "sbar": c.sbar.model_dump(mode="json")})
        gates = ["clinician_signoff"]
        types = {t for t, _ in c.received}
        if "imaging_signal" in types:
            gates.append("radiologist_review")
        if "therapy_conflict" in types:
            gates.append("nephrologist_review")
        if any(t == "data_delivered" and p.get("kind") == "genomic_context" for t, p in c.received):
            gates.append("genetic_disclosure")
        pack = CasePackReady(case_id=c.case_id, sbar=c.sbar, artifact_hash=c.artifact_hash, version=c.version,
                             gates=gates)
        await ctx.send(MessageType.CASE_PACK_READY, "dispatcher", pack, patient_id=patient_id,
                       episode_id=c.episode_id)
        return pack

    async def publish_decision(self, ctx: SeatContext, *, patient_id: str, release: Release,
                               items: list[DecisionItem], note: str = "") -> ClinicianDecision:
        """The clinician signed the case pack on the Board. Publish the decision only if the signed release
        covers exactly this case pack version."""
        c = self.cases[patient_id]
        try:
            verify_release(release, ctx.pep.clinician_keys)
        except SignatureError as exc:
            raise PolicyViolation(self.seat_id, "decision", c.case_id, f"invalid clinician release: {exc}") from exc
        if release.artifact_hash != c.artifact_hash:
            raise PolicyViolation(self.seat_id, "decision", c.case_id, "release does not cover this case pack")
        decision = ClinicianDecision(case_id=c.case_id, decision_id=release.id, artifact_hash=release.artifact_hash,
                                     signed_by=release.clinician_id, role=_human_role(release.role), note=note,
                                     items=items, signature=release.sig)
        for to in ("archivist", "companion"):
            await ctx.send(MessageType.CLINICIAN_DECISION, to, decision, patient_id=patient_id,
                           episode_id=c.episode_id)
        return decision


def _human_role(role: str) -> str:
    """Board roles are 'clinician:<unit>'; the decision names the clinical role."""
    return {"clinician:nephrology": "nephrologist", "clinician:radiology": "radiologist",
            "clinician:genetics": "geneticist"}.get(role, role)


def _sbar(received: list[tuple[str, dict[str, Any]]]) -> Sbar:
    s, a, r = [], [], []
    for kind, p in received:
        if kind == "red_flag_escalation":
            s.append(f"Red flag at home ({p['urgency']}): {p['pattern']}.")
        elif kind == "therapy_conflict":
            a.append(f"Drug-disease conflict ({p['severity']}): {p['drug']} with {', '.join(p['conditions'])}.")
            r.append("Review the analgesic; consider stopping the NSAID (nephrologist).")
        elif kind == "imaging_signal":
            a.append(f"CT model signal: {p['label']} (calibrated p={p['p_calibrated']}), for radiologist review.")
            r.append("Radiologist to review the CT and the heat map.")
        elif kind == "data_delivered" and p.get("kind") == "genomic_context":
            syndromes = p["data"].get("hereditary_syndromes") or []
            a.append("Genomic context: " + (", ".join(syndromes) if syndromes else "no hereditary syndrome reported")
                     + ".")
            r += p["data"].get("case_implications", [])
    return Sbar(S=s or ["Follow-up review."], B=["Synthetic patient, RCC follow-up with CKD (demo data)."],
                A=a or ["No new findings."], R=r or ["Routine follow-up."])
