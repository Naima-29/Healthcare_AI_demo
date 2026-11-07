"""Case Briefer (Person 5) on kcd_core: SBAR with sources, gates, signed decision binding."""

import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from kcd_core.contracts import MessageType
from kcd_core.contracts.messages import ClinicianDecision, DecisionItem
from kcd_core.pep import PolicyViolation

from kcd.desk import Desk
from kcd.fhir import FolderFhir
from kcd.knowledge import KnowledgeModel
from kcd.seats.briefer_tools import briefer_tools
from kcd.seats.case_briefer import CaseBriefer, decision_release_id

ROOT = Path(__file__).resolve().parents[2]
FIX = ROOT / "contracts" / "fixtures"
MARCO = Path(__file__).resolve().parents[1] / "kcd" / "fixtures"
NOW = datetime(2026, 10, 12, 19, 40, tzinfo=UTC)
PT = "pt_marco"


def fx(name: str, i: int = 0) -> dict:
    return json.loads((FIX / f"{name}.json").read_text(encoding="utf-8"))["valid"][i]


GENOMIC = {"kind": "genomic_context", "data": {
    "findings": [{"gene": "VHL", "hgvs": "c.499C>T", "lab_classification": "pathogenic", "relevance": "high",
                  "summary": "Loss of function variant linked to renal tumours.",
                  "citations": ["[8] ClinVar VHL"]}],
    "hereditary_syndromes": ["von Hippel-Lindau disease"],
    "case_implications": ["Offer genetic counselling and family cascade testing."],
    "needs_geneticist_review": True}}


class CaseBrieferTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / PT).mkdir()
        (root / PT / "fhir-bundle.json").write_text((MARCO / "marco-r" / "fhir-bundle.json").read_text("utf-8"))
        self.desk = await Desk.create(start=NOW)
        self.seat = CaseBriefer(now=self.desk.clock.now)
        tools = briefer_tools(FolderFhir(root), KnowledgeModel.load(), self.desk.clock.now,
                              self.desk.gates.create_item)
        self.rt = await self.desk.add_seat(self.seat, tools)

    async def asyncTearDown(self):
        await self.desk.close()
        self.tmp.cleanup()

    async def feed(self, type_, payload, sender):
        await self.desk.deliver(self.desk.envelope(type_, sender, "case_briefer", payload, patient=PT))

    async def fill(self):
        await self.feed(MessageType.RED_FLAG_ESCALATION, fx("red_flag_escalation"), "companion")
        await self.feed(MessageType.THERAPY_CONFLICT, fx("therapy_conflict"), "safety_verifier")
        await self.feed(MessageType.IMAGING_SIGNAL, fx("imaging_signal"), "safety_verifier")
        await self.feed(MessageType.DATA_DELIVERED, GENOMIC, "safety_verifier")

    async def pack(self):
        pack = await self.seat.publish_case_pack(self.rt.context(), patient_id=PT)
        await self.desk.bus.drain()
        return pack

    async def test_sbar_covers_every_seat_with_sources(self):
        await self.fill()
        pack = await self.pack()
        self.assertEqual(pack.gates, ["clinician_signoff", "radiologist_review", "nephrologist_review",
                                      "genetic_disclosure"])
        text = " ".join(pack.sbar.S + pack.sbar.B + pack.sbar.A + pack.sbar.R)
        for needle in ("Red flag", "Marco", "RCC", "Preliminary CT", "VHL", "Stop NSAID", "Radiologist review"):
            self.assertIn(needle, text)
        detail = next(i for i in self.desk.gates.items if i["kind"] == "case_pack_detail")
        self.assertEqual({a["from"] for a in detail["data"]["A"]},
                         {"Renal Guardian", "Imaging Reader", "Genomics Interpreter"})
        self.assertTrue(all(r["ref"] for r in detail["data"]["R"]))
        codes = [d["code"] for d in detail["data"]["draft"]]
        self.assertEqual(codes, sorted(codes, key=["urgent_visit", "stop_nsaid", "review_medication", "repeat_labs",
                                                   "ct_review", "vhl_followup", "genetics_followup"].index))
        self.assertEqual(len(self.desk.gates.opened), 1)  # the Dispatcher opened the clinician's gate

    async def test_draft_text_never_leaks_results_or_genetics(self):
        await self.fill()
        await self.pack()
        detail = next(i for i in self.desk.gates.items if i["kind"] == "case_pack_detail")
        for d in detail["data"]["draft"]:
            low = d["patient_text"].lower()
            for word in ("tumor", "tumour", "cancer", "vhl", "pathogenic", "egfr", "p="):
                self.assertNotIn(word, low)

    async def test_reissue_bumps_version_and_changes_hash(self):
        await self.fill()
        a = await self.pack()
        await self.feed(MessageType.SYMPTOM_REPORT, fx("symptom_report"), "companion")
        b = await self.pack()
        self.assertEqual((a.version, b.version), (1, 2))
        self.assertNotEqual(a.artifact_hash, b.artifact_hash)
        self.assertEqual(a.case_id, b.case_id)

    async def test_decision_needs_a_release_for_this_exact_pack(self):
        await self.fill()
        a = await self.pack()
        old = self.desk.release(a.case_id, a.artifact_hash, release_id="old")
        await self.feed(MessageType.SYMPTOM_REPORT, fx("symptom_report"), "companion")
        b = await self.pack()
        items = [DecisionItem(code="stop_nsaid", patient_text="Please stop ibuprofen.")]
        with self.assertRaises(PolicyViolation):  # signed v1, pack is now v2
            await self.seat.publish_decision(self.rt.context(), patient_id=PT, release=old, items=items)
        wrong = self.desk.release("case-other", b.artifact_hash, release_id="x")
        with self.assertRaises(PolicyViolation):
            await self.seat.publish_decision(self.rt.context(), patient_id=PT, release=wrong, items=items)
        forged = self.desk.release(b.case_id, b.artifact_hash, release_id="f").model_copy(update={"sig": "AAAA"})
        with self.assertRaises(PolicyViolation):
            await self.seat.publish_decision(self.rt.context(), patient_id=PT, release=forged, items=items)
        self.assertEqual(self.desk.delivered_to("companion"), [])

    async def test_signed_decision_reaches_companion_and_archivist_once(self):
        await self.fill()
        p = await self.pack()
        rel = self.desk.release(p.case_id, p.artifact_hash, release_id="dec-1")
        items = [DecisionItem(code="stop_nsaid", patient_text="Please stop ibuprofen.")]
        d = await self.seat.publish_decision(self.rt.context(), patient_id=PT, release=rel, items=items)
        await self.desk.bus.drain()
        self.assertIsInstance(d, ClinicianDecision)
        self.assertEqual(d.role, "nephrologist")
        for seat in ("companion", "archivist"):
            got = [e for e in self.desk.delivered_to(seat) if e.type == MessageType.CLINICIAN_DECISION]
            self.assertEqual(len(got), 1)
        with self.assertRaises(PolicyViolation):  # a decision is published once
            await self.seat.publish_decision(self.rt.context(), patient_id=PT, release=rel, items=items)

    async def test_strict_binding_rejects_edited_wording(self):
        self.seat.strict_binding = True
        await self.fill()
        p = await self.pack()
        items = [DecisionItem(code="stop_nsaid", patient_text="Please stop ibuprofen.")]
        rid = decision_release_id(items, "", p.artifact_hash)
        ok = self.desk.release(p.case_id, p.artifact_hash, release_id=rid)
        edited = [DecisionItem(code="stop_nsaid", patient_text="Take double ibuprofen.")]
        with self.assertRaises(PolicyViolation):
            await self.seat.publish_decision(self.rt.context(), patient_id=PT, release=ok, items=edited)
        await self.seat.publish_decision(self.rt.context(), patient_id=PT, release=ok, items=items)

    async def test_auto_publish_and_dedupe(self):
        self.seat.auto_publish = True
        env = self.desk.envelope(MessageType.RED_FLAG_ESCALATION, "companion", "case_briefer",
                                 fx("red_flag_escalation"), patient=PT)
        await self.desk.deliver(env)
        await self.desk.deliver(env)  # redelivered
        self.assertEqual(len(self.seat.cases[PT].received), 1)

    async def test_policy(self):
        ctx = self.rt.context()
        for action in ("case.decide", "patient.send"):
            with self.assertRaises(PolicyViolation):
                ctx.act(action)
        for tool in ("channel.send", "fhir.write", "pacs.fetch_study"):
            with self.assertRaises(PolicyViolation):
                ctx.pep.check_tool(tool)
        self.assertEqual(set(ctx.manifest.a2a_peers), {"archivist", "companion", "dispatcher", "genomics_interpreter"})


if __name__ == "__main__":
    unittest.main()
