"""ARCHIVIST on kcd_core: signed clinician decisions become Cortex memory (deck slides 20, 24).

Learning loop: seat proposes -> clinician corrects and signs -> Archivist drafts rules ->
clinician confirms -> rule reused in the next episode.

On a clinician_decision (from the Case Briefer, which publishes it only after the clinician's release):
  1. check it is signed by a person (not a seat) with a clinical role
  2. store the decision as evidence (patient layer 3), then draft trial rules from its items:
     patient rules (layer 3) cite the decision; hospital rules (layer 2) cite a layer-2 precedent
     that carries no patient identifier
  3. for each rule, put a rule_confirmation item on the Clinical Board (tool board.create_item)
Red-flag escalations and lab reports from the Companion are kept as evidence.

The Archivist can't confirm a rule: Cortex confirm is human-only, and the refusal auto_confirm_rule
blocks the action cortex.confirm_rule.
"""

from __future__ import annotations

from typing import Any

from kcd_core.audit import content_hash
from kcd_core.contracts import Envelope
from kcd_core.contracts.messages import ClinicianDecision, DataDelivered, RedFlagEscalation
from kcd_core.pep import PolicyViolation
from kcd_core.runtime import Seat, SeatContext
from pydantic import BaseModel

from kcd.cortex import DEFAULT_HOSPITAL, CoreMemory, Cortex
from kcd.cortex.store import is_human_role
from kcd.util import sha256_hex

# Decision item code -> rule drafted from it. Items without a template are kept in the decision evidence only.
RULE_TEMPLATES: dict[str, dict[str, Any]] = {
    "stop_nsaid": {
        "scope": "patient", "key": "avoid_drug_class:NSAID", "confidence": 0.95,
        "value": {"instruction": "Avoid NSAIDs (ibuprofen and similar).", "used_by": ["companion", "renal_guardian"]},
    },
    "urgent_visit": {
        "scope": "hospital", "key": "routing:rcc_red_flag", "confidence": 0.9,
        "value": {"pattern": "RCC follow-up + visible haematuria + flank pain >= 7/10",
                  "route_to": "uro_oncologist_on_call"},
    },
}


def rule_hash(rule: dict[str, Any]) -> str:
    """The artifact hash the clinician's confirmation release must cover."""
    return content_hash({k: rule[k] for k in ("id", "layer", "partition", "key", "version", "value", "evidence_ids")})


class Archivist(Seat):
    seat_id = "archivist"

    def __init__(self, cortex: Cortex, *, hospital: str = DEFAULT_HOSPITAL):
        self.cortex = cortex
        self.hospital = hospital

    def memory(self, ctx: SeatContext, patient_id: str) -> CoreMemory:
        return CoreMemory(self.cortex, ctx, self.seat_id, patient_id, self.hospital)

    async def handle(self, env: Envelope, payload: BaseModel, ctx: SeatContext) -> None:
        if isinstance(payload, ClinicianDecision):
            await self._on_decision(env, payload, ctx)
        elif isinstance(payload, RedFlagEscalation) or (isinstance(payload, DataDelivered) and payload.kind == "lab"):
            mem = self.memory(ctx, env.patient_id)
            if mem.processed_result(f"message:{env.id}") is None:
                ev = mem.append_evidence(kind=str(env.type), source=f"seat:{env.from_}",
                                         content=payload.model_dump(mode="json", by_alias=True),
                                         meta={"message_id": str(env.id)})
                mem.mark_processed(f"message:{env.id}", {"evidence_id": ev["id"]})

    async def _on_decision(self, env: Envelope, d: ClinicianDecision, ctx: SeatContext) -> None:
        if not d.signed_by.strip() or d.signed_by.startswith("seat:") or not is_human_role(d.role or ""):
            # Without a person's signature there is nothing to learn from.
            raise PolicyViolation(self.seat_id, "decision", d.decision_id,
                                  "clinician_decision is not signed by a clinician", "store_without_evidence")
        mem = self.memory(ctx, env.patient_id)
        key = f"decision:{d.decision_id}"
        if mem.processed_result(key) is not None:
            return  # a redelivery of a decision already handled

        decision_ev = mem.append_evidence(
            kind="clinician_decision", source=f"clinician:{d.signed_by}",
            content=d.model_dump(mode="json"), meta={"message_id": str(env.id), "case_id": d.case_id})
        source = f"clinician:{d.signed_by} ({d.role}) case {d.case_id}"
        rule_ids, items = [], []
        for item in d.items:
            template = RULE_TEMPLATES.get(item.code)
            if template is None:
                continue
            note = item.clinician_text or item.patient_text
            if template["scope"] == "patient":
                rule = mem.propose_patient_rule(key=template["key"], value=template["value"],
                                                confidence=template["confidence"], source=source,
                                                evidence_ids=[decision_ev["id"]], note=note)
            else:
                precedent = mem.append_hospital_evidence(kind="routing_precedent", content={
                    "case_ref": sha256_hex(f"{env.patient_id}:{d.case_id}")[:16],  # no patient id in shared memory
                    "item_code": item.code, "decided_by_role": d.role,
                    "pattern": template["value"].get("pattern"),
                })
                rule = mem.propose_hospital_rule(key=template["key"], value=template["value"],
                                                 confidence=template["confidence"], source=source,
                                                 evidence_ids=[precedent["id"]], note=note)
            rule_ids.append(rule["id"])
            items.append(await ctx.tool(
                "board.create_item", kind="rule", gate="rule_confirmation", patient_id=env.patient_id,
                case_id=d.case_id, title=f"Confirm trial rule ({template['scope']}): {rule['key']} v{rule['version']}",
                artifact_id=rule["id"], artifact_hash=rule_hash(rule),
                data={"key": rule["key"], "value": rule["value"], "layer": rule["layer"], "version": rule["version"],
                      "evidence_ids": rule["evidence_ids"]}))
        mem.mark_processed(key, {"evidence_id": decision_ev["id"], "rules": rule_ids, "board_items": items})

    def rules_for_next_episode(self, ctx: SeatContext, patient_id: str) -> list[dict[str, Any]]:
        """Confirmed, still-confident rules for this patient and their hospital."""
        return self.memory(ctx, patient_id).active_rules()
