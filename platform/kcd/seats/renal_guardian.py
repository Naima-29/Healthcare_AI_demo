"""RENAL GUARDIAN on kcd_core: eGFR trajectory, CKD stage and drug-disease conflicts (deck slides 4, 20, 27 case 2).

Port of Diana's Node seat (demo/kidney-care-desk/backend/seats/clinical.mjs, diana/agent-4) onto Core, with the
same rules (knowledge/rules.json through km.check_conflicts / km.ckd_stage):

  medication_report (from the Companion)
    -> fhir.get_observations(eGFR)
    -> latest eGFR older than the stale limit (90 days)? data_request to the Companion: ask for a recent lab
       report, conclude nothing yet
    -> otherwise evaluate on the record
  data_delivered{kind: lab} (the patient's lab photo, from the Companion) -> evaluate on the fresh value
  evaluate: conditions, medications, home blood pressure, CKD stage -> km.check_conflicts
    -> therapy_conflict to the Safety Verifier (or nothing, when there is no conflict)

Refusals: change_dose, prescribe, send_to_patient, alter_record. Gate: nephrologist_review (on the case pack).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from kcd_core.contracts import Envelope, MessageType
from kcd_core.contracts.messages import DataDelivered, DataRequest, MedicationReport, TherapyConflict
from kcd_core.runtime import Seat, SeatContext
from pydantic import BaseModel

PEER = "safety_verifier"
STALE_DAYS = 90


@dataclass
class _Pending:
    drug: MedicationReport
    egfr_record: dict[str, Any]
    episode_id: str


class RenalGuardian(Seat):
    seat_id = "renal_guardian"

    def __init__(self, *, stale_days: int = STALE_DAYS):
        self.stale_days = stale_days
        self.pending: dict[str, _Pending] = {}  # patient id -> medication waiting for kidney data
        self.wearables_consent_requested: set[str] = set()

    async def handle(self, env: Envelope, payload: BaseModel, ctx: SeatContext) -> None:
        if isinstance(payload, MedicationReport):
            await self._on_medication(env, payload, ctx)
        elif isinstance(payload, DataDelivered) and payload.kind == "lab":
            d = payload.data
            await self._evaluate(env.patient_id, ctx, {"egfr": d.get("egfr"), "creatinine": d.get("creatinine"),
                                                       "date": d.get("date"), "source": d.get("source")})

    async def _on_medication(self, env: Envelope, med: MedicationReport, ctx: SeatContext) -> None:
        record = await ctx.tool("fhir.get_observations", patient_id=env.patient_id, code="eGFR")
        self.pending[env.patient_id] = _Pending(med, record, env.episode_id)
        age = record.get("age_days")
        if age is None or age > self.stale_days:
            latest = record.get("latest") or {}
            await ctx.send(MessageType.DATA_REQUEST, "companion", DataRequest(
                what="recent creatinine / eGFR",
                why=f"latest eGFR on record is {age} days old ({latest.get('date')})",
                ask_patient="photo of the most recent blood test report",
                items=[{"kind": "lab", "code": "egfr", "max_age_days": self.stale_days}]))
            return
        latest = record["latest"]
        await self._evaluate(env.patient_id, ctx, {"egfr": latest["value"], "creatinine": None,
                                                    "date": latest["date"], "source": "FHIR record"})

    async def _evaluate(self, patient_id: str, ctx: SeatContext, lab: dict[str, Any]) -> None:
        pend = self.pending.get(patient_id)
        if pend is None:
            return  # a lab report with no medication waiting: nothing to check
        conditions = await ctx.tool("fhir.get_conditions", patient_id=patient_id)
        medications = await ctx.tool("fhir.get_medications", patient_id=patient_id)
        try:
            wear = await ctx.tool("wearables.read", patient_id=patient_id, days=30)
            self.wearables_consent_requested.discard(patient_id)
        except PermissionError:
            wear = {}

            if patient_id not in self.wearables_consent_requested:
                self.wearables_consent_requested.add(patient_id)

                await ctx.send(
                    MessageType.DATA_REQUEST,
                    "companion",
                    DataRequest(
                        what="wearable_data_consent",
                        why="Wearable data could help assess the patient's home blood pressure.",
                        ask_patient="Please ask the patient for permission to access wearable data.",
                    ),
                    patient_id=patient_id,
                    episode_id=pend.episode_id,
                )
        stage = await ctx.tool("km.ckd_stage", egfr=lab["egfr"])
        sbp = (wear.get("summary_7d") or {}).get("sbp")
        conflicts = await ctx.tool("km.check_conflicts", drug_class=pend.drug.drug_class, egfr=lab["egfr"], sbp=sbp,
                                   conditions=[c["display"] for c in conditions if c.get("display")],
                                   medications=[m["name"] for m in medications if m.get("name")])
        del self.pending[patient_id]
        if not conflicts:
            return
        c = conflicts[0]
        series = [{"date": x["date"], "value": x["value"]} for x in pend.egfr_record.get("series", [])]
        if lab.get("date") and not any(x["date"] == lab["date"] for x in series):
            series.insert(0, {"date": lab["date"], "value": lab["egfr"]})
        await ctx.send(MessageType.THERAPY_CONFLICT, PEER, TherapyConflict(
            drug=c["drug"], conditions=c["conditions"], severity=c["severity"], rationale=c["rationale"],
            citations=c["citations"], rule_id=c.get("rule_id"), egfr_series=series,
            data_freshness={"egfr": lab["egfr"], "creatinine": lab.get("creatinine"), "date": lab.get("date"),
                            "source": lab.get("source"), "stage": stage["stage"] if stage else None,
                            "home_sbp_7d": sbp}),
            patient_id=patient_id, episode_id=pend.episode_id)
