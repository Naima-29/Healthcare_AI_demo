"""Chief of Staff: plain-language job descriptions -> manifest drafts, with a deterministic safety floor.

Ported from the demo compiler's rule pass. The rule pass always runs. A language model may propose a richer
draft, but `enforce_floor` merges it so that refusals and gates can be added and never dropped, and every tool
must exist in the catalogue. Nothing here activates a manifest: the result is a draft plus a diff, and the
Director approves it (ManifestRegistry.activate) outside this module.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..contracts import Manifest, PolicyCatalog
from ..registry import check_against_catalog
from .patterns import GATE_PATTERNS, REFUSAL_PATTERNS, TOOL_PATTERNS

# Every new seat inherits these, whatever the text says.
BASE_FLOOR = ("diagnose", "prescribe", "change_dose")

_SENTENCES = re.compile(r"(?<=[.;!?\n])\s+")
_NAME = re.compile(
    r"(?:agente|agent|seat|posto)\s+(?:per|for|chiamato|called|named)?\s*(?:il |la |lo |l'|the )?([^.:,\n]{3,50})", re.I
)
_PATIENT_EDGE = re.compile(r"(telefono del paziente|patient'?s phone|a casa|at home|presso il paziente)", re.I)


def rule_refusals(text: str) -> list[str]:
    found: list[str] = []
    for sentence in _SENTENCES.split(text):
        for pattern, token in REFUSAL_PATTERNS:
            if pattern.search(sentence) and token not in found:
                found.append(token)
    return found


def rule_tools(text: str) -> tuple[list[str], list[str]]:
    tools: list[str] = []
    surfaces: list[str] = []
    for pattern, names, surface in TOOL_PATTERNS:
        if pattern.search(text):
            tools += [t for t in names if t not in tools]
            if surface and surface not in surfaces:
                surfaces.append(surface)
    return tools, surfaces


def rule_gates(text: str) -> list[str]:
    return [gate for pattern, gate in GATE_PATTERNS if pattern.search(text)]


def _slug(text: str) -> str:
    ascii_text = unicodedata.normalize("NFD", text).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "_", ascii_text).strip("_")[:40]


def _union(*lists: list[str] | tuple[str, ...]) -> list[str]:
    out: list[str] = []
    for lst in lists:
        out += [x for x in lst if x not in out]
    return out


@dataclass(frozen=True)
class Diff:
    refuses: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    gates: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.refuses or self.tools or self.gates)


@dataclass(frozen=True)
class Draft:
    manifest: Manifest
    diff: Diff

    @property
    def understood(self) -> bool:
        return not self.diff.empty


def amend(current: Manifest, instruction: str, catalog: PolicyCatalog, *, now: datetime,
          by: str = "chief_of_staff") -> Draft:
    """Apply one plain-language instruction to a live manifest (the demo's step 1). Returns a draft."""
    refusals = rule_refusals(instruction)
    tools, surfaces = rule_tools(instruction)
    gates = rule_gates(instruction)

    added_refuses = [r for r in refusals if r not in current.refuses]
    # A sentence that is a prohibition never grants tools ("never send the CT to the patient" must not add PACS).
    added_tools = [] if refusals else [t for t in tools if t not in current.tools and t != "a2a.send"]
    new_surfaces = current.surfaces if refusals else _union(current.surfaces, surfaces)
    added_gates = [g for g in gates if g not in current.gates and g != "none"]
    next_gates = _union([g for g in current.gates if g != "none" or not added_gates], added_gates)

    draft = current.model_copy(update={
        "refuses": _union(current.refuses, added_refuses),
        "tools": _union(current.tools, added_tools),
        "surfaces": new_surfaces,
        "gates": next_gates,
        "version": current.version + 1,
        "job_description": f"{current.job_description.strip()} {instruction.strip()}",
        "compiled_by": by,
        "compiled_at": now.isoformat(),
    })
    draft = _validated(draft, catalog)
    return Draft(draft, Diff(added_refuses, added_tools, added_gates))


def compile_new(text: str, catalog: PolicyCatalog, *, now: datetime, name: str | None = None,
                location: str | None = None) -> Draft:
    """Compile a brand-new seat from a job description (rule pass only). The seat starts with no A2A routes."""
    m = _NAME.search(text)
    seat_name = (name or (m.group(1) if m else "New Seat")).strip()
    loc = location or ("patient_edge" if _PATIENT_EDGE.search(text) else "hospital_edge")
    tools, surfaces = rule_tools(text)
    refuses = _union(rule_refusals(text), BASE_FLOOR)
    if loc == "hospital_edge" and "channel" not in surfaces:
        refuses = _union(refuses, ["send_to_patient"])
    gates = rule_gates(text) or ["clinical_advice"]
    for tool in tools:
        surface = catalog.surface_of(tool)
        if surface and surface not in surfaces:
            surfaces.append(surface)

    body: dict[str, Any] = {
        "seat": _slug(seat_name) or "new_seat",
        "name": seat_name[:1].upper() + seat_name[1:],
        "role": "Compiled by the Chief of Staff",
        "version": 1,
        "location": loc,
        "desk": "new",
        "model": {"edge": "loopq-clin-2b" if loc == "patient_edge" else "loopq-clin-4b", "fallback": None},
        "skills": [],
        "tools": tools,
        "surfaces": surfaces,
        "memory": {"read": [0, 1, 3], "write": []},
        "refuses": refuses,
        "gates": gates,
        "artifact": "task_result",
        "unit": {"kind": "per_task", "tc": 2},
        "job_description": text.strip(),
        "compiled_by": "chief_of_staff",
        "compiled_at": now.isoformat(),
    }
    draft = _validated(Manifest.model_validate(body), catalog)
    return Draft(draft, Diff(refuses, tools, gates))


def enforce_floor(
    proposed: Manifest, text: str, catalog: PolicyCatalog, *, previous: Manifest | None = None
) -> Manifest:
    """Merge a model-proposed manifest with the rule pass. The model can add restrictions, never remove them.

    - refuses = proposed ∪ rule pass on the text ∪ previous version ∪ BASE_FLOOR
    - gates   = proposed ∪ rule pass ∪ previous version ('none' only if nothing else is left)
    - tools / surfaces / seat identity are the model's, but must pass the catalogue check
    """
    refuses = _union(proposed.refuses, rule_refusals(text), previous.refuses if previous else [], BASE_FLOOR)
    gates = _union(proposed.gates, rule_gates(text), previous.gates if previous else [])
    if len(gates) > 1:
        gates = [g for g in gates if g != "none"]
    merged = proposed.model_copy(update={"refuses": refuses, "gates": gates})
    if previous is not None:
        merged = merged.model_copy(update={"seat": previous.seat, "version": previous.version + 1})
    return _validated(merged, catalog)


def _validated(m: Manifest, catalog: PolicyCatalog) -> Manifest:
    # model_copy skips validation, so round-trip through the model and the catalogue on every draft.
    fresh = Manifest.model_validate(m.model_dump())
    check_against_catalog(fresh, catalog)
    return fresh
