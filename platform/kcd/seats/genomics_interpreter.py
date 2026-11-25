"""GENOMICS INTERPRETER on kcd_core: the DNA panel read against the variant rules (deck slides 20, 26, 27).

On a data_request for the genomic context (from the Case Briefer: `what == "genomic_context"`, or an item of
kind "genomics"):
  genomics.get_panel -> wrong-patient check -> panel stored as evidence (patient layer 3)
  -> fhir.get_conditions (RCC history) -> km.interpret_variant (knowledge/variant_rules.json)
  -> lab classes checked unchanged -> data_delivered{kind: genomic_context} to the Safety Verifier

Gate: genetic_disclosure (manifest), opened on the case pack downstream.
Refusals: reclassify_variants (no write tool; lab class copied verbatim and verified before sending),
disclose_genetics_to_patient and send_to_patient (only peer is the Safety Verifier).
"""

from __future__ import annotations

import re
from typing import Any

from kcd_core.contracts import Envelope, MessageType
from kcd_core.contracts.messages import DataDelivered, DataRequest, EvidenceRef
from kcd_core.pep import PolicyViolation
from kcd_core.runtime import Seat, SeatContext
from pydantic import BaseModel

from kcd.cortex import CoreMemory, Cortex
from kcd.genomics.panel import Panel
from kcd.util import sha256_hex

PEER = "safety_verifier"
RCC = re.compile(r"renal cell carcinoma|\brcc\b|clear[- ]cell", re.IGNORECASE)


def history_from_conditions(conditions: Any) -> dict[str, bool]:
    items = conditions if isinstance(conditions, list) else []
    texts = [c if isinstance(c, str) else str(c.get("display") or "") if isinstance(c, dict) else "" for c in items]
    return {"rcc": any(RCC.search(t) for t in texts)}


def wants_genomics(req: DataRequest) -> bool:
    return req.what == "genomic_context" or any(i.kind == "genomics" for i in req.items)


class GenomicsInterpreter(Seat):
    seat_id = "genomics_interpreter"

    def __init__(self, cortex: Cortex):
        self.cortex = cortex

    async def handle(self, env: Envelope, payload: BaseModel, ctx: SeatContext) -> None:
        if isinstance(payload, DataRequest) and wants_genomics(payload):
            await self.interpret(ctx, env)

    async def interpret(self, ctx: SeatContext, env: Envelope) -> dict[str, Any]:
        mem = CoreMemory(self.cortex, ctx, self.seat_id, env.patient_id)
        key = f"request:{env.id}"
        previous = mem.processed_result(key)
        if previous is not None:
            return {**previous, "duplicate": True}

        fetched = await ctx.tool("genomics.get_panel", patient_id=env.patient_id)
        raw, panel = fetched["raw"], fetched["panel"]
        if not isinstance(panel, Panel) or panel.patient_id != env.patient_id:
            # A panel filed under the wrong patient must never reach a case pack.
            raise PolicyViolation(self.seat_id, "data", "genomics.get_panel", "panel belongs to a different patient")

        panel_sha = sha256_hex(raw)
        panel_ev = mem.append_evidence(
            kind="genomic_panel", source=f"genomics:{panel.method or panel.source_format}", blob=raw,
            content={"panel_id": panel.panel_id, "method": panel.method, "date": panel.date,
                     "format": panel.source_format, "variant_count": len(panel.variants), "sha256": panel_sha})

        history = history_from_conditions(await ctx.tool("fhir.get_conditions", patient_id=env.patient_id))
        data = await ctx.tool("km.interpret_variant", panel=panel, history=history)
        self._check_not_reclassified(panel, data)

        citations = list(dict.fromkeys(c for f in data["findings"] for c in f["citations"]))
        evidence = [EvidenceRef(kind="evidence", ref=panel_ev["id"])]
        evidence += [EvidenceRef(kind="variant", ref=f["variant_id"] or f"{f['gene']}:{f['hgvs']}")
                     for f in data["findings"]]
        sent = await ctx.send(MessageType.DATA_DELIVERED, PEER, DataDelivered(
            kind="genomic_context", data=data, evidence_id=panel_ev["id"], citations=citations, evidence=evidence))

        result = {"message_id": str(sent.id), "panel_evidence_id": panel_ev["id"], "data": data}
        mem.mark_processed(key, result)
        return {**result, "duplicate": False}

    def _check_not_reclassified(self, panel: Panel, data: dict) -> None:
        lab = {(v.gene, v.variant_id, v.hgvs): v.classification_raw for v in panel.variants}
        for f in data["findings"]:
            if lab.get((f["gene"], f["variant_id"], f["hgvs"])) != f["lab_classification"]:
                raise PolicyViolation(self.seat_id, "content", "genomic_context",
                                      f"{f['gene']} classification differs from the laboratory's",
                                      "reclassify_variants")
