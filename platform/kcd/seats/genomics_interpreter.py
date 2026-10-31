"""GENOMICS INTERPRETER: the DNA panel read against the variant rules (deck slides 20, 26, 27 case 3).

Sends the hereditary context to the case pack as data_delivered{kind: genomic_context}.
Refusals: reclassify_variants (lab class copied verbatim and checked before sending),
disclose_genetics_to_patient / send_to_patient (only the Dispatcher is a peer).
Gate: genetic_disclosure, signed by a geneticist or clinician.
"""
from __future__ import annotations

from kcd.contracts import Envelope
from kcd.cortex import Cortex, CortexView
from kcd.genomics.panel import Panel, PanelError
from kcd.genomics.rules import VariantRules, interpret
from kcd.genomics.source import PanelSource
from kcd.seatkit import GateOpener, Manifest, Outbox, Seat
from kcd.util import sha256_hex

REQUEST_KINDS = ("genomic_context",)


class GenomicsInterpreter(Seat):
    def __init__(self, cortex: Cortex, outbox: Outbox, gates: GateOpener, panels: PanelSource, *,
                 rules: VariantRules | None = None, manifest: Manifest | None = None):
        super().__init__(manifest or Manifest.load("genomics_interpreter"), outbox, gates)
        self.memory = CortexView(cortex, self.manifest)
        self.panels = panels
        self.rules = rules or VariantRules.load()

    def handle(self, env: Envelope) -> dict | None:
        if env.type == "data_request" and env.payload.get("what") in REQUEST_KINDS:
            return self.interpret(env.patient, context=env.payload.get("context"), correlation=env.id)
        return None

    def interpret(self, patient: str, *, context: dict | None = None, correlation: str | None = None) -> dict:
        self.require_tool("genomics.get_panel")
        raw, panel = self.panels.get_panel(patient)
        if panel.patient_id != patient:
            # A panel filed under the wrong patient must never reach a case pack.
            self.audit("genomics.get_panel", "blocked", reason="patient mismatch")
            raise PanelError("panel belongs to a different patient")
        panel_sha = sha256_hex(raw)
        history = (context or {}).get("history") or {}
        key = f"panel:{panel_sha}:{sha256_hex(repr(sorted(history.items())))[:12]}"
        previous = self.memory.processed_result(key)
        if previous is not None:
            self.audit("genomics.interpret", "duplicate_skipped", panel_id=panel.panel_id)
            return {**previous, "duplicate": True}

        panel_ev = self.memory.append_evidence(
            3, patient, kind="genomic_panel", source=f"genomics:{panel.method or panel.source_format}",
            content={"panel_id": panel.panel_id, "method": panel.method, "date": panel.date,
                     "format": panel.source_format, "variant_count": len(panel.variants), "sha256": panel_sha},
            blob=raw)

        self.require_tool("km.interpret_variant")
        data = interpret(panel, self.rules, context)
        self._assert_not_reclassified(panel, data)

        env = self.send("data_delivered", patient,
                        {"kind": "genomic_context", "data": data, "evidence_id": panel_ev["id"]},
                        correlation=correlation, route_hint=["case_briefer"])
        gate_id = self.open_gate("genetic_disclosure", patient,
                                 f"Genetic findings for review ({len(data['findings'])} reported)",
                                 {"panel_evidence_id": panel_ev["id"], "panel_id": panel.panel_id,
                                  "hereditary_syndromes": data["hereditary_syndromes"]})
        result = {"message_id": env.id, "panel_evidence_id": panel_ev["id"], "gate_id": gate_id, "data": data}
        self.memory.mark_processed(key, 3, patient, result)
        return {**result, "duplicate": False}

    def _assert_not_reclassified(self, panel: Panel, data: dict) -> None:
        lab = {(v.gene, v.variant_id, v.hgvs): v.classification_raw for v in panel.variants}
        for f in data["findings"]:
            if lab.get((f["gene"], f["variant_id"], f["hgvs"])) != f["lab_classification"]:
                self.act("genomics.reclassify")  # raises: reclassify_variants is a hard block
                raise self.refuse("reclassify_variants", f"{f['gene']} classification changed")
