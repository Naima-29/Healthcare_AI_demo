"""Minimal seat runtime for this slice.

PROVISIONAL: Harith (Core) owns the real runtime and policy enforcement point.
This kit implements the same rules as the demo (backend/runtime/seat.mjs and
guard.mjs) so the seats here are enforced today, and it exposes the three ports
Core will replace: an outbox (A2A bus), a gate opener (Clinical Board) and an
audit sink.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from kcd import contracts
from kcd.util import iso, utcnow

MANIFEST_DIR = Path(__file__).parent / "manifests"
MANIFEST_FIELDS = ("seat", "version", "tools", "surfaces", "memory", "refuses", "gates", "a2a_peers")

# Refusal token -> actions it blocks (subset of the demo's contracts/catalog.mjs#REFUSALS).
REFUSAL_ACTIONS: dict[str, tuple[str, ...]] = {
    "label_as_diagnosis": ("imaging.label_diagnosis",),
    "report_to_patient": ("patient.send", "patient.disclose_imaging"),
    "alter_image": ("pacs.write",),
    "reclassify_variants": ("genomics.reclassify",),
    "disclose_genetics_to_patient": ("patient.disclose_genetics",),
    "send_to_patient": ("patient.send",),
    "store_without_evidence": ("cortex.store_unevidenced",),
    "auto_confirm_rule": ("cortex.confirm_rule",),
    "write_general_weights": ("model.train_general",),
}

# Text that would turn a model signal into a diagnosis (refusal label_as_diagnosis).
# Negated mentions ("it is not a diagnosis") are removed before the claim patterns run.
NEGATED_DIAGNOSIS = re.compile(r"\b(not|never|no|without)\s+(a\s+|any\s+)?diagnos\w*", re.IGNORECASE)
DIAGNOSIS_CLAIM = re.compile(
    r"\bdiagnos(is|es)\s*(of|is|:)"
    r"|\bdiagnosed\s+(with|as)\b"
    r"|\bdiagnostic\s+(of|for)\b"
    r"|\bconfirm(s|ed|ing)?\s+(a\s+|an\s+|the\s+)?(tumou?r|cancer|carcinoma|malignan\w*)"
    r"|\b(is|has|shows)\s+(a\s+|an\s+)?(malignant|malignancy|cancer|carcinoma)\b"
    r"|\bconsistent\s+with\s+(a\s+)?(cancer|carcinoma|malignan\w*)"
    r"|\bdefinitive(ly)?\b",
    re.IGNORECASE,
)


def is_diagnosis_claim(text: str) -> bool:
    return bool(DIAGNOSIS_CLAIM.search(NEGATED_DIAGNOSIS.sub(" ", text or "")))


class RefusalError(PermissionError):
    def __init__(self, seat: str, refusal: str, detail: str = ""):
        super().__init__(f"{seat}: refused ({refusal}) {detail}".strip())
        self.seat, self.refusal, self.detail = seat, refusal, detail


@dataclass(frozen=True)
class Manifest:
    seat: str
    version: int
    tools: tuple[str, ...]
    surfaces: tuple[str, ...]
    memory_read: tuple[int, ...]
    memory_write: tuple[int, ...]
    refuses: tuple[str, ...]
    gates: tuple[str, ...]
    a2a_peers: tuple[str, ...]
    raw: dict

    @classmethod
    def from_dict(cls, d: dict) -> "Manifest":
        missing = [k for k in MANIFEST_FIELDS if k not in d]
        if missing:
            raise ValueError(f"manifest {d.get('seat', '?')}: missing {missing}")
        unknown = [r for r in d["refuses"] if r not in REFUSAL_ACTIONS]
        if unknown:
            raise ValueError(f"manifest {d['seat']}: refusals not in catalogue {unknown}")
        mem = d["memory"]
        layers = list(mem.get("read", [])) + list(mem.get("write", []))
        if any(layer not in (0, 1, 2, 3) for layer in layers):
            raise ValueError(f"manifest {d['seat']}: memory layers must be 0-3")
        return cls(seat=d["seat"], version=int(d["version"]), tools=tuple(d["tools"]), surfaces=tuple(d["surfaces"]),
                   memory_read=tuple(mem.get("read", [])), memory_write=tuple(mem.get("write", [])),
                   refuses=tuple(d["refuses"]), gates=tuple(d["gates"]), a2a_peers=tuple(d["a2a_peers"]), raw=d)

    @classmethod
    def load(cls, seat: str, directory: Path = MANIFEST_DIR) -> "Manifest":
        return cls.from_dict(json.loads((directory / f"{seat}.manifest.json").read_text(encoding="utf-8")))


class Outbox(Protocol):
    def __call__(self, envelope: contracts.Envelope) -> None: ...


class GateOpener(Protocol):
    def __call__(self, gate: str, patient: str, title: str, artifact: dict, opened_by: str) -> str: ...


AuditSink = Callable[[dict], None]


class InMemoryOutbox:
    """Collects sent envelopes. Stand-in for the A2A bus in tests and local runs."""

    def __init__(self) -> None:
        self.sent: list[contracts.Envelope] = []

    def __call__(self, envelope: contracts.Envelope) -> None:
        self.sent.append(envelope)

    def of_type(self, msg_type: str) -> list[contracts.Envelope]:
        return [e for e in self.sent if e.type == msg_type]


class InMemoryGates:
    """Records opened gates. Stand-in for the Clinical Board's gate service."""

    def __init__(self) -> None:
        self.opened: list[dict] = []

    def __call__(self, gate: str, patient: str, title: str, artifact: dict, opened_by: str) -> str:
        gate_id = f"gate_{len(self.opened) + 1}"
        self.opened.append({"id": gate_id, "gate": gate, "patient": patient, "title": title,
                            "artifact": artifact, "opened_by": opened_by})
        return gate_id


class Seat:
    def __init__(self, manifest: Manifest, outbox: Outbox, gates: GateOpener | None = None,
                 audit: AuditSink | None = None):
        self.manifest = manifest
        self.outbox = outbox
        self.gates = gates
        self.audit_events: list[dict] = []
        self._audit_sink = audit

    @property
    def id(self) -> str:
        return self.manifest.seat

    def audit(self, action: str, decision: str, **details: Any) -> None:
        event = {"ts": iso(utcnow()), "seat": self.id, "manifest_version": self.manifest.version,
                 "action": action, "decision": decision, **details}
        self.audit_events.append(event)
        if self._audit_sink:
            self._audit_sink(event)

    def refuse(self, refusal: str, detail: str) -> RefusalError:
        self.audit("refusal", "blocked", refusal=refusal, detail=detail)
        return RefusalError(self.id, refusal, detail)

    def require_tool(self, tool: str) -> None:
        if tool not in self.manifest.tools:
            raise self.refuse("tool_not_in_manifest", tool)

    def act(self, action: str) -> None:
        """Raise if any refusal in the manifest blocks this action."""
        for refusal in self.manifest.refuses:
            if action in REFUSAL_ACTIONS.get(refusal, ()):
                raise self.refuse(refusal, action)

    def check_text(self, text: str) -> str:
        if "label_as_diagnosis" in self.manifest.refuses and is_diagnosis_claim(text):
            raise self.refuse("label_as_diagnosis", f"diagnosis claim in text: {text[:120]!r}")
        return text

    def send(self, msg_type: str, patient: str, payload: dict, *, to: str = "dispatcher",
             correlation: str | None = None, route_hint: list[str] | None = None) -> contracts.Envelope:
        self.require_tool("a2a.send")
        if to == "patient":
            self.act("patient.send")
            raise self.refuse("not_a_peer", "seats never address the patient directly")
        if to not in self.manifest.a2a_peers:
            raise self.refuse("not_a_peer", to)
        contracts.validate(msg_type, payload)
        env = contracts.Envelope(type=msg_type, sender=self.id, to=to, patient=patient, payload=payload,
                                 correlation=correlation, route_hint=list(route_hint or []))
        self.outbox(env)
        self.audit("a2a.send", "allowed", type=msg_type, to=to, message_id=env.id)
        return env

    def open_gate(self, gate: str, patient: str, title: str, artifact: dict) -> str:
        if gate not in self.manifest.gates:
            raise self.refuse("gate_not_in_manifest", gate)
        if self.gates is None:
            # A human gate that can't be opened must stop the flow, never be skipped silently.
            self.audit("gate.open", "failed", gate=gate, reason="no gate service connected")
            raise RuntimeError(f"{self.id}: cannot open gate {gate!r}: no gate service connected")
        gate_id = self.gates(gate, patient, title, artifact, self.id)
        self.audit("gate.open", "allowed", gate=gate, gate_id=gate_id)
        return gate_id
