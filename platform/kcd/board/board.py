"""CLINICAL BOARD on kcd_core: every human gate lives here.

What it does
  - is the Dispatcher's GatePort: `open_gate(env, pack)` turns each case_pack_ready into one open review item
    (idempotent on case id + version; a newer version supersedes the older one)
  - is the `board.create_item` backend (case_pack_detail from the Case Briefer, rule_confirmation from the Archivist)
  - takes the clinician's actions and SIGNS them with the clinician's own Ed25519 key:
        review(...)        radiologist / nephrologist / geneticist reviews required by the case pack's gates
        sign(...)          the decision on a case pack -> Case Briefer.publish_decision (what the patient hears)
        confirm_rule(...)  / reject_rule(...) -> Cortex (human-only)
  - pushes every change to subscribers (the UI's live stream)

What it never does: decide, write to the patient, or sign for a clinician who is not signed in. In this slice the
Board holds the demo clinicians' keys (`Clinician.signer`); in production the key lives with the clinician
(hospital SSO + hardware key) and `sign` receives a ready-made Release instead.

Rules enforced here, on top of what the Case Briefer and the PEP verify again:
  - a decision needs every other gate of that case pack reviewed by a clinician of the matching role
  - only the latest open version of a case pack can be signed
  - the signer must hold a clinician role, and each item is acted on once
"""

from __future__ import annotations

import copy
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from kcd_core.contracts import Envelope, Release
from kcd_core.contracts.messages import CasePackReady, ClinicianDecision, DecisionItem
from kcd_core.signing import Signer, sign_release

from kcd.seats.case_briefer import decision_release_id

GATE_ROLE = {  # catalogue gates -> the role that must act
    "radiologist_review": "clinician:radiology",
    "nephrologist_review": "clinician:nephrology",
    "genetic_disclosure": "clinician:genetics",
}
GATE_LABEL = {
    "urgent_ack": "Urgent escalation acknowledged",
    "clinician_signoff": "Case pack signed",
    "radiologist_review": "Imaging signal reviewed",
    "nephrologist_review": "Medication change reviewed",
    "genetic_disclosure": "Genetic findings disclosed",
    "rule_confirmation": "New rule confirmed",
}


class BoardError(Exception):
    """A refused Board action (shown to the clinician, never retried)."""


@dataclass(frozen=True)
class Clinician:
    id: str
    name: str
    role: str  # e.g. "clinician:nephrology"
    signer: Signer


Decide = Callable[[str, Release, list[DecisionItem], str], Awaitable[ClinicianDecision]]
RuleAction = Callable[[str, str, str, Release | None, str | None], Any]  # (rule id, clinician id, role, release, reason)


class ClinicalBoard:
    def __init__(self, clinicians: list[Clinician], clock: Callable[[], datetime], *,
                 decide: Decide | None = None, confirm_rule: RuleAction | None = None,
                 reject_rule: RuleAction | None = None,
                 acknowledge: Callable[[str, str], Awaitable[Any]] | None = None) -> None:
        self.clinicians = {c.id: c for c in clinicians}
        self.clock = clock
        self.decide = decide  # set after the Case Briefer exists: board.decide = lambda ...
        self.confirm_rule_fn = confirm_rule
        self.reject_rule_fn = reject_rule
        self.acknowledge_fn = acknowledge  # (escalation id, clinician id): Core's EscalationService.acknowledge
        self._items: dict[str, dict[str, Any]] = {}
        self._details: dict[tuple[str, int], dict[str, Any]] = {}
        self._n = 0
        self._listeners: list[Callable[[dict[str, Any]], None]] = []
        self.opened: list[tuple[Envelope, CasePackReady]] = []  # same name as the test stand-in
        self.decisions: list[ClinicianDecision] = []

    # ------------------------------------------------------------------ read / subscribe
    @property
    def items(self) -> list[dict[str, Any]]:
        return [copy.deepcopy(i) for i in self._items.values()]

    def get(self, item_id: str) -> dict[str, Any]:
        if item_id not in self._items:
            raise BoardError(f"unknown board item {item_id}")
        return copy.deepcopy(self._items[item_id])

    def subscribe(self, fn: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
        self._listeners.append(fn)
        return lambda: self._listeners.remove(fn)

    def _emit(self, op: str, item: dict[str, Any]) -> None:
        for fn in list(self._listeners):
            fn({"op": op, "item": copy.deepcopy(item)})

    def _new_id(self) -> str:
        self._n += 1
        return f"board_item_{self._n}"

    def _history(self, item: dict[str, Any], note: str) -> None:
        item["history"].append({"ts": self.clock().isoformat(), "note": note})

    # ------------------------------------------------------------------ GatePort (Dispatcher)
    async def open_gate(self, env: Envelope, pack: CasePackReady) -> None:
        if any(p.case_id == pack.case_id and p.version == pack.version for _, p in self.opened):
            return  # bus retry
        self.opened.append((env, pack))
        for old in self._items.values():  # the older versions can no longer be signed
            if old["kind"] == "case_pack" and old["case_id"] == pack.case_id and old["status"] == "open":
                old["status"] = "superseded"
                self._history(old, f"superseded by version {pack.version}")
                self._emit("update", old)
        reviews = {g: None for g in pack.gates if g != "clinician_signoff"}
        item = {"id": self._new_id(), "kind": "case_pack", "gate": "clinician_signoff",
                "gate_label": GATE_LABEL["clinician_signoff"], "signer_role": "treating_clinician",
                "status": "open", "title": f"Case pack v{pack.version}", "patient_id": env.patient_id,
                "case_id": pack.case_id, "version": pack.version, "artifact_hash": pack.artifact_hash,
                "sbar": pack.sbar.model_dump(mode="json"), "required_gates": list(pack.gates), "reviews": reviews,
                "data": self._details.get((pack.case_id, pack.version), {}), "created": self.clock().isoformat(),
                "history": []}
        self._items[item["id"]] = item
        self._emit("create", item)

    # ------------------------------------------------------------------ board.create_item / update_item
    def create_item(self, spec: dict[str, Any]) -> str:
        if spec.get("kind") == "case_pack_detail":  # from the Case Briefer: sources + draft decision
            key = (spec["case_id"], spec["version"])
            self._details[key] = spec.get("data", {})
            for it in self._items.values():  # the pack may already be open (order is not guaranteed)
                if it["kind"] == "case_pack" and (it["case_id"], it["version"]) == key:
                    it["data"] = self._details[key]
                    self._emit("update", it)
            return f"detail:{key[0]}:v{key[1]}"
        gate = spec.get("gate", "none")
        item = {"id": self._new_id(), "kind": spec.get("kind", "info"), "gate": gate,
                "gate_label": GATE_LABEL.get(gate), "status": "info" if gate == "none" else "open",
                "title": spec.get("title", ""), "patient_id": spec.get("patient_id"), "case_id": spec.get("case_id"),
                "artifact_id": spec.get("artifact_id"), "artifact_hash": spec.get("artifact_hash"),
                "data": spec.get("data", {}), "created": self.clock().isoformat(), "history": []}
        self._items[item["id"]] = item
        self._emit("create", item)
        return item["id"]

    def update_item(self, item_id: str, patch: dict[str, Any]) -> None:
        item = self._items.get(item_id)
        if item is None:
            raise BoardError(f"unknown board item {item_id}")
        item.update(patch)
        self._emit("update", item)

    # ------------------------------------------------------------------ human actions
    def _clinician(self, clinician_id: str) -> Clinician:
        c = self.clinicians.get(clinician_id)
        if c is None:
            raise BoardError(f"{clinician_id!r} is not a signed-in clinician")
        return c

    def _open(self, item_id: str, kind: str) -> dict[str, Any]:
        item = self._items.get(item_id)
        if item is None or item["kind"] != kind:
            raise BoardError(f"no {kind} item {item_id}")
        if item["status"] != "open":
            raise BoardError(f"item {item_id} is {item['status']}, not open")
        return item

    def _release(self, c: Clinician, artifact_id: str, artifact_hash: str, release_id: str,
                 classes: list[str] | None = None) -> Release:
        return sign_release(Release(id=release_id, artifact_id=artifact_id, artifact_hash=artifact_hash,
                                    clinician_id=c.id, role=c.role, content_classes=classes or [],
                                    signed_at=self.clock()), c.signer)

    def review(self, item_id: str, gate: str, clinician_id: str) -> dict[str, Any]:
        """A specialist reviews their part of a case pack (imaging, medication, genetics)."""
        item, c = self._open(item_id, "case_pack"), self._clinician(clinician_id)
        if gate not in item["reviews"]:
            raise BoardError(f"this case pack has no {gate} gate")
        if c.role != GATE_ROLE[gate]:
            raise BoardError(f"{gate} must be reviewed by {GATE_ROLE[gate]}, not {c.role}")
        rel = self._release(c, item["case_id"], item["artifact_hash"], f"review-{item['id']}-{gate}")
        item["reviews"][gate] = {"by": c.id, "name": c.name, "role": c.role, "at": self.clock().isoformat(),
                                 "release_id": rel.id}
        self._history(item, f"{gate} reviewed by {c.name}")
        self._emit("update", item)
        return copy.deepcopy(item)

    async def sign(self, item_id: str, clinician_id: str, selections: list[dict[str, Any]] | None = None,
                   note: str = "") -> ClinicianDecision:
        """Sign the decision. `selections`: [{code, selected?, patient_text?}] over the draft; wording the
        clinician typed is what gets signed and what the Companion says."""
        if self.decide is None:
            raise BoardError("the Board is not connected to the Case Briefer")
        item, c = self._open(item_id, "case_pack"), self._clinician(clinician_id)
        if not c.role.startswith("clinician:"):
            raise BoardError("only a clinician can sign a case pack")
        missing = [g for g, r in item["reviews"].items() if r is None]
        if missing:
            raise BoardError(f"review first: {', '.join(missing)}")
        draft = {d["code"]: d for d in item["data"].get("draft", [])}
        chosen = selections if selections is not None else [{"code": k} for k in draft]
        items: list[DecisionItem] = []
        for s in chosen:
            d = draft.get(s["code"])
            if d is None:
                raise BoardError(f"{s['code']!r} is not an option of this case pack")
            if s.get("selected", True) is False:
                continue
            text = (s.get("patient_text") or d["patient_text"]).strip()
            if not text:
                raise BoardError(f"{s['code']}: the patient wording is empty")
            items.append(DecisionItem(code=s["code"], patient_text=text, patient_text_en=d.get("patient_text_en"),
                                      clinician_text=d.get("clinician_text")))
        if not items:
            raise BoardError("nothing selected: a decision needs at least one item")
        rel = self._release(c, item["case_id"], item["artifact_hash"],
                            decision_release_id(items, note, item["artifact_hash"]))
        decision = await self.decide(item["patient_id"], rel, items, note)
        item["status"] = "signed"
        item.update(signed_by=c.id, signed_role=c.role, decision_id=decision.decision_id)
        self._history(item, f"signed by {c.name}")
        self.decisions.append(decision)
        self._emit("update", item)
        return decision

    async def acknowledge(self, item_id: str, clinician_id: str) -> dict[str, Any]:
        """The on-call clinician acknowledges an urgent escalation (stops the ladder)."""
        item, c = self._open(item_id, "escalation"), self._clinician(clinician_id)
        if not c.role.startswith("clinician:"):
            raise BoardError("only a clinician can acknowledge an escalation")
        if self.acknowledge_fn is None:
            raise BoardError("the Board is not connected to the escalation service")
        await self.acknowledge_fn(item["artifact_id"], c.id)
        item["status"] = "acknowledged"
        item.update(signed_by=c.id, signed_role=c.role)
        self._history(item, f"acknowledged by {c.name}")
        self._emit("update", item)
        return copy.deepcopy(item)

    def confirm_rule(self, item_id: str, clinician_id: str) -> dict[str, Any]:
        item, c = self._open(item_id, "rule"), self._clinician(clinician_id)
        if self.confirm_rule_fn is None:
            raise BoardError("the Board is not connected to Cortex")
        rel = self._release(c, item["artifact_id"], item["artifact_hash"], f"confirm-{item['id']}")
        self.confirm_rule_fn(item["artifact_id"], c.id, c.role, rel, None)
        item["status"] = "confirmed"
        self._history(item, f"confirmed by {c.name}")
        self._emit("update", item)
        return copy.deepcopy(item)

    def reject_rule(self, item_id: str, clinician_id: str, reason: str) -> dict[str, Any]:
        item, c = self._open(item_id, "rule"), self._clinician(clinician_id)
        if self.reject_rule_fn is None:
            raise BoardError("the Board is not connected to Cortex")
        if not reason.strip():
            raise BoardError("a reason is required to reject a rule")
        rel = self._release(c, item["artifact_id"], item["artifact_hash"], f"reject-{item['id']}")
        self.reject_rule_fn(item["artifact_id"], c.id, c.role, rel, reason)
        item["status"] = "rejected"
        self._history(item, f"rejected by {c.name}: {reason}")
        self._emit("update", item)
        return copy.deepcopy(item)
