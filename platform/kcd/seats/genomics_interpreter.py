"""GENOMICS INTERPRETER on kcd_core: the DNA panel read against the variant rules (deck slides 20, 26, 27).

On a data_request with a genomics item (from the Case Briefer):
  genomics.read_panel (tool) -> wrong-patient check -> panel stored as evidence (L1)
  -> km.lookup("variant_rules") + fhir.read_patient (tools) -> interpretation
  -> lab classes checked unchanged -> data_delivered{genomic_context} to the Safety Verifier

DataDelivered.items: the first item is the panel summary, then one item per reported finding.
Gate: geneticist review (manifest gate genomic_context / clinician:genetics), opened downstream.
Refusals: no_variant_reclassification (no write tool; lab class copied verbatim and verified
before sending), no_result_disclosure and no_patient_contact (only peer is the Safety Verifier).
"""

from __future__ import annotations

import re
from typing import Any

from kcd_core.contracts import Envelope, MessageType
from kcd_core.contracts.messages import Citation, DataDelivered, DataRequest, EvidenceRef
from kcd_core.pep import PolicyViolation
from kcd_core.runtime import Seat, SeatContext
from pydantic import BaseModel

from kcd.cortex import CoreMemory, Cortex
from kcd.genomics.panel import Panel
from kcd.genomics.rules import VariantRules, interpret
from kcd.util import sha256_hex

PEER = "safety_verifier"
RCC = re.compile(r"renal cell carcinoma|\brcc\b|clear[- ]cell", re.IGNORECASE)


def history_from_summary(summary: Any) -> dict[str, bool]:
    conditions = summary.get("conditions", []) if isinstance(summary, dict) else []
    texts = [c if isinstance(c, str) else str(c.get("display", "")) if isinstance(c, dict) else "" for c in conditions]
    return {"rcc": any(RCC.search(t) for t in texts)}


class GenomicsInterpreter(Seat):
    seat_id = "genomics_interpreter"

    def __init__(self, cortex: Cortex):
        self.cortex = cortex
        self._rules: tuple[str, VariantRules] | None = None

    async def handle(self, env: Envelope, payload: BaseModel, ctx: SeatContext) -> None:
        if not isinstance(payload, DataRequest) or not any(i.kind == "genomics" for i in payload.items):
            return
        await self.interpret(ctx, env)

    async def interpret(self, ctx: SeatContext, env: Envelope) -> dict[str, Any]:
        mem = CoreMemory(self.cortex, ctx, self.seat_id, env.patient_id)
        key = f"request:{env.id}"
        previous = mem.processed_result(key)
        if previous is not None:
            return {**previous, "duplicate": True}

        fetched = await ctx.tool("genomics.read_panel", patient_id=env.patient_id)
        raw, panel = fetched["raw"], fetched["panel"]
        if not isinstance(panel, Panel) or panel.patient_id != env.patient_id:
            # A panel filed under the wrong patient must never reach a case pack.
            raise PolicyViolation(self.seat_id, "data", "genomics.read_panel", "panel belongs to a different patient")

        panel_sha = sha256_hex(raw)
        panel_ev = mem.append_evidence(
            kind="genomic_panel", source=f"genomics:{panel.method or panel.source_format}", blob=raw,
            content={"panel_id": panel.panel_id, "method": panel.method, "date": panel.date,
                     "format": panel.source_format, "variant_count": len(panel.variants), "sha256": panel_sha})

        rules = await self._load_rules(ctx)
        history = history_from_summary(await ctx.tool("fhir.read_patient", patient_id=env.patient_id))
        data = interpret(panel, rules, {"history": history})
        self._check_not_reclassified(panel, data)

        summary = {"type": "panel_summary", **{k: v for k, v in data.items() if k != "findings"}}
        findings = [{"type": "finding", **f} for f in data["findings"]]
        citations = {}
        for f in data["findings"]:
            for cid, text in zip(f["citation_ids"], f["citations"], strict=True):
                citations[cid] = Citation(source=cid, ref=text)
        evidence = [EvidenceRef(kind="document", ref=f"cortex:evidence/{panel_ev['id']}")]
        evidence += [EvidenceRef(kind="variant", ref=f["variant_id"] or f"{f['gene']}:{f['hgvs']}")
                     for f in data["findings"]]
        sent = await ctx.send(MessageType.DATA_DELIVERED, PEER, DataDelivered(
            kind="genomic_context", items=[summary, *findings], citations=list(citations.values()),
            evidence=evidence, in_reply_to=env.id))

        result = {"message_id": str(sent.id), "panel_evidence_id": panel_ev["id"], "data": data}
        mem.mark_processed(key, result)
        return {**result, "duplicate": False}

    async def _load_rules(self, ctx: SeatContext) -> VariantRules:
        doc = await ctx.tool("km.lookup", topic="variant_rules")
        version = str(doc.get("version", "")) if isinstance(doc, dict) else ""
        if self._rules is None or self._rules[0] != version or not version:
            self._rules = (version, VariantRules.from_dict(doc))
        return self._rules[1]

    def _check_not_reclassified(self, panel: Panel, data: dict) -> None:
        lab = {(v.gene, v.variant_id, v.hgvs): v.classification_raw for v in panel.variants}
        for f in data["findings"]:
            if lab.get((f["gene"], f["variant_id"], f["hgvs"])) != f["lab_classification"]:
                raise PolicyViolation(self.seat_id, "content", "genomic_context",
                                      f"{f['gene']} classification differs from the laboratory's",
                                      "no_variant_reclassification")
