"""ARCHIVIST on kcd_core: signed clinician decisions become Cortex memory (deck slides 20, 24).

Learning loop: seat proposes -> clinician corrects and signs -> Archivist drafts rules ->
clinician confirms -> rule reused in the next episode.

On a clinician_decision (from the Case Briefer):
  1. read the released artifact from the Board (tool board.read_artifact)
  2. verify it: the clinician's Ed25519 release (kcd_core.signing) covers this artifact and hash,
     and the artifact content hashes to the value in the decision. Anything else is a PolicyViolation.
  3. store the decision as evidence (L1), then draft trial rules from the artifact's items:
     patient rules (L2) cite the decision; hospital rules (L3) cite an L3 precedent with no patient id
  4. for each rule, send case_pack_ready to the Dispatcher, which opens the confirmation gate
  5. acknowledge to the Dispatcher
The Archivist can't confirm a rule: Cortex confirm is human-only, and refusal
no_rule_self_confirmation blocks cortex.confirm_rule in the catalogue.
"""

from __future__ import annotations

from typing import Any

from kcd_core.audit import content_hash
from kcd_core.contracts import Envelope, MessageType, Release
from kcd_core.contracts.messages import Acknowledgment, CasePackReady, ClinicianDecision
from kcd_core.pep import PolicyViolation
from kcd_core.runtime import Seat, SeatContext
from kcd_core.signing import SignatureError, verify_release
from pydantic import BaseModel, ValidationError

from kcd.cortex import DEFAULT_HOSPITAL, CoreMemory, Cortex
from kcd.util import sha256_hex

RULE_ARTIFACT = "knowledge_rule"

# Decision item code -> rule drafted from it. Items without a template (and without an explicit
# "rule") are kept in the decision evidence only.
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


class DraftError(ValueError):
    pass


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
        if not isinstance(payload, ClinicianDecision):
            return
        mem = self.memory(ctx, env.patient_id)
        key = f"decision:{env.id}"
        if mem.processed_result(key) is not None:
            return  # a bus redelivery of a message already handled

        fetched = await ctx.tool("board.read_artifact", artifact_id=payload.artifact_id)
        artifact, release = self._verified(fetched, payload, ctx)

        try:
            drafts = [] if payload.decision == "rejected" else self._drafts(artifact)
        except DraftError as exc:
            # A malformed artifact teaches nothing: store nothing, say so to the Dispatcher.
            await ctx.send(MessageType.ACKNOWLEDGMENT, "dispatcher",
                           Acknowledgment(ack_of=env.id, status="rejected", reason=str(exc)))
            mem.mark_processed(key, {"rejected": str(exc)})
            return

        decision_ev = mem.append_evidence(
            kind="clinician_decision", source=f"clinician:{release.clinician_id}",
            content={"decision": payload.model_dump(mode="json"), "release": release.model_dump(mode="json"),
                     "artifact": artifact},
            meta={"message_id": str(env.id)})

        gate_role = next(g.signer_role for g in ctx.manifest.gates if g.artifact == RULE_ARTIFACT)
        source = f"clinician:{release.clinician_id} ({release.role}) artifact {payload.artifact_id}"
        rule_ids = []
        for item, draft in drafts:
            note = item.get("clinician_text") or item.get("patient_text")
            if draft["scope"] == "patient":
                rule = mem.propose_patient_rule(key=draft["key"], value=draft["value"], confidence=draft["confidence"],
                                                source=source, evidence_ids=[decision_ev["id"]], note=note)
            else:
                precedent = mem.append_hospital_evidence(kind="routing_precedent", content={
                    "case_ref": sha256_hex(f"{env.patient_id}:{payload.artifact_id}")[:16],  # no patient id at L3
                    "item_code": item.get("code"), "decided_by_role": release.role,
                    "pattern": draft["value"].get("pattern") if isinstance(draft["value"], dict) else None,
                })
                rule = mem.propose_hospital_rule(key=draft["key"], value=draft["value"], confidence=draft["confidence"],
                                                 source=source, evidence_ids=[precedent["id"]], note=note)
            rule_ids.append(rule["id"])
            await ctx.send(MessageType.CASE_PACK_READY, "dispatcher", CasePackReady(
                artifact_id=rule["id"], artifact_hash=rule_hash(rule), gate_role=gate_role,
                summary=f"Confirm trial rule ({draft['scope']}): {rule['key']} v{rule['version']}"))

        await ctx.send(MessageType.ACKNOWLEDGMENT, "dispatcher", Acknowledgment(ack_of=env.id, status="accepted"))
        mem.mark_processed(key, {"evidence_id": decision_ev["id"], "rules": rule_ids})

    def rules_for_next_episode(self, ctx: SeatContext, patient_id: str) -> list[dict[str, Any]]:
        """Confirmed, still-confident rules for this patient and their hospital."""
        return self.memory(ctx, patient_id).active_rules()

    # ------------------------------------------------------------------ internals
    def _verified(self, fetched: Any, decision: ClinicianDecision, ctx: SeatContext) -> tuple[dict, Release]:
        def block(reason: str) -> PolicyViolation:
            return PolicyViolation(self.seat_id, "decision", decision.artifact_id, reason)

        if not isinstance(fetched, dict) or not isinstance(fetched.get("artifact"), dict):
            raise block("board returned no artifact")
        try:
            release = fetched.get("release")
            release = release if isinstance(release, Release) else Release.model_validate(release)
        except ValidationError as exc:
            raise block(f"board returned no valid release: {exc.error_count()} error(s)") from exc
        artifact = fetched["artifact"]
        if release.id != decision.release_id or release.artifact_id != decision.artifact_id:
            raise block("release does not match the decision")
        if release.artifact_hash != decision.artifact_hash or content_hash(artifact) != decision.artifact_hash:
            raise block("artifact hash does not match the signed release")
        if release.role != decision.clinician_role:
            raise block("release role does not match the decision")
        try:
            verify_release(release, ctx.pep.clinician_keys)
        except SignatureError as exc:
            raise block(f"invalid clinician release: {exc}") from exc
        return artifact, release

    @staticmethod
    def _drafts(artifact: dict) -> list[tuple[dict, dict]]:
        """Validate every draft before anything is written, so a bad item can't leave half a decision behind."""
        items = artifact.get("items", [])
        if not isinstance(items, list):
            raise DraftError("artifact items must be a list")
        out = []
        for i, item in enumerate(items):
            if not isinstance(item, dict) or not item.get("code"):
                raise DraftError(f"item {i} needs a code")
            explicit = item.get("rule")
            if explicit is not None:
                if not isinstance(explicit, dict) or not explicit.get("key") or "value" not in explicit:
                    raise DraftError(f"item {item['code']!r}: rule needs 'key' and 'value'")
                scope = explicit.get("scope", "patient")
                if scope not in ("patient", "hospital"):
                    raise DraftError(f"item {item['code']!r}: scope must be 'patient' or 'hospital'")
                try:
                    confidence = float(explicit.get("confidence", 0.9))
                except (TypeError, ValueError) as exc:
                    raise DraftError(f"item {item['code']!r}: confidence must be a number") from exc
                if not 0.0 < confidence <= 1.0:
                    raise DraftError(f"item {item['code']!r}: confidence must be in (0, 1]")
                draft = {"scope": scope, "key": str(explicit["key"]), "value": explicit["value"],
                         "confidence": confidence}
            elif item["code"] in RULE_TEMPLATES:
                draft = dict(RULE_TEMPLATES[item["code"]])
            else:
                continue
            out.append((item, draft))
        return out
