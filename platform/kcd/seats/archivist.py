"""ARCHIVIST: turns signed clinician decisions into Cortex memory (deck slides 20, 24).

Learning loop: seat proposes -> clinician corrects -> Archivist drafts rule -> clinician
confirms -> rule reused in the next episode.

* Every decision is stored as evidence first; every rule cites that evidence.
* Rules are proposed in trial (shadow) status and a rule_confirmation gate is opened.
  The Archivist has no way to confirm: confirmation is a human-only Cortex call.
* L2 (hospital) rules cite L2 evidence that carries no patient identifiers.
"""
from __future__ import annotations

from typing import Callable

from kcd.contracts import Envelope, validate
from kcd.cortex import HOSPITAL_PARTITION, Cortex, CortexView
from kcd.cortex.store import HUMAN_ROLES
from kcd.seatkit import GateOpener, Manifest, Outbox, Seat
from kcd.util import sha256_hex

# Decision item code -> rule drafted from it. Items without a template (and without an
# explicit "rule") are kept as evidence only.
RULE_TEMPLATES: dict[str, dict] = {
    "stop_nsaid": {
        "layer": 3, "key": "avoid_drug_class:NSAID", "confidence": 0.95,
        "value": {"instruction": "Avoid NSAIDs (ibuprofen and similar).",
                  "used_by": ["companion", "renal_guardian"]},
    },
    "urgent_visit": {
        "layer": 2, "key": "routing:rcc_red_flag", "confidence": 0.9,
        "value": {"pattern": "RCC follow-up + visible haematuria + flank pain >= 7/10",
                  "route_to": "uro_oncologist_on_call"},
    },
}

# Messages kept as evidence in the patient's partition (no rule is drafted from them).
EVIDENCE_MESSAGE_TYPES = {"red_flag_escalation", "therapy_conflict", "case_pack_ready"}

DecisionVerifier = Callable[[dict], bool]


def default_decision_verifier(payload: dict) -> bool:
    """Development check. Core replaces it with verification of the clinician's signed release."""
    signer = str(payload.get("signed_by") or "")
    return bool(signer.strip()) and not signer.startswith("seat:") and payload.get("role") in HUMAN_ROLES


class Archivist(Seat):
    def __init__(self, cortex: Cortex, outbox: Outbox, gates: GateOpener, *,
                 manifest: Manifest | None = None, decision_verifier: DecisionVerifier = default_decision_verifier,
                 hospital: str = HOSPITAL_PARTITION):
        super().__init__(manifest or Manifest.load("archivist"), outbox, gates)
        self.memory = CortexView(cortex, self.manifest)
        self.decision_verifier = decision_verifier
        self.hospital = hospital

    def handle(self, env: Envelope) -> dict | None:
        if env.type == "clinician_decision":
            return self._on_decision(env)
        if env.type in EVIDENCE_MESSAGE_TYPES or (env.type == "data_delivered" and env.payload.get("kind") == "lab"):
            ev = self.memory.append_evidence(3, env.patient, kind=env.type, source=f"seat:{env.sender}",
                                             content=env.payload, meta={"message_id": env.id})
            self.audit("cortex.evidence_append", "allowed", kind=env.type, evidence_id=ev["id"])
            return {"evidence_id": ev["id"], "proposals": [], "gates": []}
        return None

    def _on_decision(self, env: Envelope) -> dict:
        validate("clinician_decision", env.payload)
        p = env.payload
        if not self.decision_verifier(p):
            # Without a verified human signature there is no evidence to learn from.
            raise self.refuse("store_without_evidence", "clinician_decision is not signed by a verified clinician")

        key = f"decision:{env.id}"
        previous = self.memory.processed_result(key)
        if previous is not None:
            self.audit("clinician_decision", "duplicate_skipped", message_id=env.id)
            return {**previous, "duplicate": True}

        # Validate every draft before writing anything, so a bad item can't leave half a decision behind.
        drafts = [(item, d) for item in p["items"] if (d := self._draft(item)) is not None]
        for item, d in drafts:
            if d["layer"] not in (2, 3):
                raise self.refuse("memory_scope", f"the Archivist does not write L{d['layer']} rules")

        decision_ev = self.memory.append_evidence(
            3, env.patient, kind="clinician_decision", source=f"clinician:{p['signed_by']}", content=p,
            meta={"message_id": env.id, "case_id": p["case_id"]})

        proposals, gates = [], []
        for item, draft in drafts:
            layer = draft["layer"]
            if layer == 3:
                partition, evidence_ids = env.patient, [decision_ev["id"]]
            elif layer == 2:
                partition = self.hospital
                precedent = self.memory.append_evidence(2, partition, kind="routing_precedent", content={
                    "case_ref": sha256_hex(f"{env.patient}:{p['case_id']}")[:16],  # no patient identifier at L2
                    "item_code": item["code"], "decided_by_role": p.get("role"),
                    "pattern": draft["value"].get("pattern") if isinstance(draft["value"], dict) else None,
                })
                evidence_ids = [precedent["id"]]
            else:
                raise self.refuse("memory_scope", f"the Archivist does not write L{layer} rules")
            rule = self.memory.propose(
                layer, partition, key=draft["key"], value=draft["value"], confidence=draft["confidence"],
                source=f"clinician:{p['signed_by']} ({p.get('role')}) case {p['case_id']}",
                evidence_ids=evidence_ids, note=item.get("clinician_text") or item.get("patient_text"))
            proposals.append(rule)
            gates.append(self.open_gate(
                "rule_confirmation", env.patient,
                f"Confirm trial rule ({'L2 hospital' if layer == 2 else 'L3 patient'}): {draft['key']}",
                {"knowledge_id": rule["id"], "layer": layer, "key": rule["key"], "value": rule["value"],
                 "version": rule["version"], "evidence_ids": rule["evidence_ids"]}))
            self.audit("cortex.knowledge_propose", "allowed", knowledge_id=rule["id"], key=rule["key"], layer=layer)

        result = {"evidence_id": decision_ev["id"], "proposals": [r["id"] for r in proposals], "gates": gates}
        self.memory.mark_processed(key, 3, env.patient, result)
        return {**result, "duplicate": False}

    @staticmethod
    def _draft(item: dict) -> dict | None:
        explicit = item.get("rule")
        if explicit is not None:
            if not isinstance(explicit, dict) or "key" not in explicit or "value" not in explicit:
                raise ValueError(f"decision item {item.get('code')!r}: rule needs 'key' and 'value'")
            return {"layer": int(explicit.get("layer", 3)), "key": str(explicit["key"]), "value": explicit["value"],
                    "confidence": float(explicit.get("confidence", 0.9))}
        template = RULE_TEMPLATES.get(item["code"])
        return dict(template) if template else None

    def rules_for_next_episode(self, patient: str) -> list[dict]:
        """Confirmed, still-confident rules for this patient and their hospital."""
        return self.memory.active_rules([(3, patient), (2, self.hospital)])
