"""Renal Guardian and Safety Verifier on kcd_core.

The behaviour tests are Diana's (demo/kidney-care-desk/tests/seats-behavior.test.mjs), translated. Two of her
manifest assertions encode the demo's routing ("talks only to the Dispatcher"); in Core every message still goes
through the Dispatcher, but `a2a_peers` names the receiving seats, so those are checked as routes instead.
"""

import copy
import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from kcd_core.bus import DISPATCHER_INBOX
from kcd_core.contracts import MessageType
from kcd_core.contracts.messages import TherapyConflict
from kcd_core.pep import PolicyViolation

from kcd.desk import Desk
from kcd.fhir import FolderFhir
from kcd.knowledge import KnowledgeModel
from kcd.seats.renal_guardian import RenalGuardian
from kcd.seats.safety_verifier import SafetyVerifier
from kcd.tools import clinical_tools

FIXTURES = Path(__file__).resolve().parents[1] / "kcd" / "fixtures" / "marco-r"
NOW = datetime(2026, 10, 12, 19, 40, tzinfo=UTC)
KM = KnowledgeModel.load()
MED = {"drug": "ibuprofen", "class": "NSAID", "dose_text": "ibuprofen 600 three times a day"}
LAB = {"kind": "lab", "data": {"egfr": 52, "creatinine": 1.41, "date": "2026-10-08", "source": "patient photo"}}


def bundle(egfr_date: str | None = "2026-06-12", egfr: float = 58, conditions=("Essential (primary) hypertension",),
           meds=("Sunitinib (TKI)",)) -> dict:
    entries = [{"resource": {"resourceType": "Condition", "code": {"coding": [{"display": c}]}}} for c in conditions]
    entries += [{"resource": {"resourceType": "MedicationStatement", "medicationCodeableConcept": {"text": m},
                              "status": "active"}} for m in meds]
    if egfr_date:
        entries.append({"resource": {"resourceType": "Observation", "code": {"text": "eGFR"},
                                     "effectiveDateTime": egfr_date, "valueQuantity": {"value": egfr}}})
    return {"resourceType": "Bundle", "entry": entries}


class RenalGuardianTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.desk = await Desk.create(start=NOW)

    async def asyncTearDown(self):
        await self.desk.close()
        self.tmp.cleanup()

    async def start(self, record: dict, sbp: float | None = 148):
        (self.root / "pt_marco").mkdir(exist_ok=True)
        (self.root / "pt_marco" / "fhir-bundle.json").write_text(json.dumps(record))
        wear = {"pt_marco": {"sbp": sbp}} if sbp is not None else {}
        self.rt = await self.desk.add_seat(RenalGuardian(),
                                           clinical_tools(FolderFhir(self.root), KM, self.desk.clock.now, wear))

    async def send(self, type_, payload):
        await self.desk.deliver(self.desk.envelope(type_, "companion", "renal_guardian", payload))

    def out(self):
        # what the seat sent (to the Dispatcher), not the routed copies
        return [e for s, e in self.desk.bus.published if s == DISPATCHER_INBOX and e.from_ == "renal_guardian"]

    async def test_fresh_egfr_gives_a_high_conflict_with_sources(self):
        await self.start(bundle(egfr_date="2026-10-01", egfr=56))
        await self.send(MessageType.MEDICATION_REPORT, MED)
        sent = self.out()
        self.assertEqual([(e.type, e.to) for e in sent], [(MessageType.THERAPY_CONFLICT, "safety_verifier")])
        p = sent[0].payload
        self.assertEqual(p["severity"], "high")                       # eGFR + blood pressure + TKI
        self.assertGreaterEqual(len(p["citations"]), 1)
        self.assertEqual((p["data_freshness"]["stage"], p["data_freshness"]["egfr"]), ("G3a", 56))
        self.assertEqual(self.desk.bus.dead_letters, [])

    async def test_stale_egfr_asks_for_fresh_data_and_concludes_nothing(self):
        await self.start(bundle(egfr_date="2026-06-12"))              # 122 days old
        await self.send(MessageType.MEDICATION_REPORT, MED)
        sent = self.out()
        self.assertEqual([(e.type, e.to) for e in sent], [(MessageType.DATA_REQUEST, "companion")])
        self.assertIn("123 days", sent[0].payload["why"])          # 122.8 days at 19:40 UTC, rounded
        self.assertTrue(sent[0].payload["ask_patient"])

    async def test_fresh_lab_completes_the_check(self):
        await self.start(bundle(egfr_date="2026-06-12"))
        await self.send(MessageType.MEDICATION_REPORT, MED)
        await self.send(MessageType.DATA_DELIVERED, LAB)
        self.assertEqual([e.type for e in self.out()], [MessageType.DATA_REQUEST, MessageType.THERAPY_CONFLICT])
        p = self.out()[-1].payload
        self.assertEqual(p["data_freshness"]["egfr"], 52)
        self.assertEqual(p["egfr_series"][0], {"date": "2026-10-08", "value": 52})

    async def test_no_record_at_all_asks_for_data(self):
        await self.start(bundle(egfr_date=None))
        await self.send(MessageType.MEDICATION_REPORT, MED)
        self.assertEqual([e.type for e in self.out()], [MessageType.DATA_REQUEST])

    async def test_healthy_kidneys_no_conflict(self):
        await self.start(bundle(egfr_date="2026-10-01", egfr=95, conditions=(), meds=()), sbp=120)
        await self.send(MessageType.MEDICATION_REPORT, MED)
        self.assertEqual(self.out(), [])

    async def test_paracetamol_no_conflict(self):
        await self.start(bundle(egfr_date="2026-10-01", egfr=40))
        await self.send(MessageType.MEDICATION_REPORT, {"drug": "paracetamol", "class": "non_NSAID_analgesic"})
        self.assertEqual(self.out(), [])

    async def test_lab_without_pending_medication_does_nothing(self):
        await self.start(bundle())
        await self.send(MessageType.DATA_DELIVERED, LAB)
        self.assertEqual(self.out(), [])

    async def test_policy(self):
        await self.start(bundle())
        ctx = self.rt.context()
        for action, refusal in (("medication.change_dose", "change_dose"), ("medication.prescribe", "prescribe"),
                                ("patient.send", "send_to_patient"), ("fhir.write", "alter_record")):
            with self.assertRaises(PolicyViolation) as cm:
                ctx.act(action)
            self.assertEqual(cm.exception.refusal, refusal)
        for cls in ("dose_instruction", "prescription"):
            with self.assertRaises(PolicyViolation):
                ctx.guard_content(cls)
        for tool in ("pacs.fetch_study", "channel.send", "genomics.get_panel"):
            with self.assertRaises(PolicyViolation):
                ctx.pep.check_tool(tool)
        self.assertEqual(set(ctx.manifest.a2a_peers), {"companion", "safety_verifier"})
        self.assertEqual(ctx.manifest.gates, ["nephrologist_review"])


CONFLICT = json.loads((Path(__file__).resolve().parents[2] / "contracts" / "fixtures" / "therapy_conflict.json")
                      .read_text(encoding="utf-8"))["valid"][0]
IMAGING = json.loads((Path(__file__).resolve().parents[2] / "contracts" / "fixtures" / "imaging_signal.json")
                     .read_text(encoding="utf-8"))["valid"][0]


class SafetyVerifierTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.desk = await Desk.create(start=NOW)
        self.seat = SafetyVerifier(now=self.desk.clock.now)
        self.rt = await self.desk.add_seat(self.seat, clinical_tools(FolderFhir(FIXTURES.parent), KM,
                                                                     self.desk.clock.now))

    async def asyncTearDown(self):
        await self.desk.close()

    async def verify(self, type_, payload, sender):
        await self.desk.deliver(self.desk.envelope(type_, sender, "safety_verifier", payload))

    def passed_on(self):
        return self.desk.delivered_to("case_briefer")

    def rejections(self):
        return [e for s, e in self.desk.bus.published
                if s == DISPATCHER_INBOX and e.from_ == "safety_verifier" and e.type == MessageType.ACKNOWLEDGMENT]

    async def test_passes_a_correct_conflict_unchanged(self):
        await self.verify(MessageType.THERAPY_CONFLICT, CONFLICT, "renal_guardian")
        self.assertEqual(len(self.passed_on()), 1)
        # Passed on unchanged (no `verification` stamp: the verifier's signature is the proof)
        self.assertEqual(TherapyConflict.model_validate(self.passed_on()[0].payload),
                         TherapyConflict.model_validate(CONFLICT))
        self.assertNotIn("verification", self.passed_on()[0].payload)
        self.assertEqual(self.passed_on()[0].from_, "safety_verifier")
        self.assertEqual(self.rejections(), [])

    async def test_blocks_failed_checks_and_tells_the_sender(self):
        cases = {
            "no sources": {"citations": ["[x] placeholder"]},
            "implausible eGFR": {"data_freshness": {**CONFLICT["data_freshness"], "egfr": 400}},
            "old kidney data": {"data_freshness": {**CONFLICT["data_freshness"], "date": "2026-01-01"}},
            "unreadable date": {"data_freshness": {**CONFLICT["data_freshness"], "date": "08/10/2026"}},
            "date in the future": {"data_freshness": {**CONFLICT["data_freshness"], "date": "2027-03-01"}},
            "made-up citation": {"citations": ["[99] Invented Guideline 2030"]},
            "partial citation": {"citations": [CONFLICT["citations"][0][:30]]},
            "one fake among real": {"citations": [CONFLICT["citations"][0], "[99] Invented Guideline 2030"]},
        }
        for i, (name, change) in enumerate(cases.items()):
            payload = copy.deepcopy(CONFLICT)
            payload.update(change)
            payload["drug"] = f"NSAID case {i}"  # distinct payloads, so dedupe can't hide a case
            await self.verify(MessageType.THERAPY_CONFLICT, payload, "renal_guardian")
            self.assertEqual(self.passed_on(), [], name)
            self.assertEqual(len(self.rejections()), i + 1, name)
        rej = self.rejections()[0]
        self.assertEqual((rej.to, rej.payload["status"]), ("renal_guardian", "rejected"))
        self.assertIn("every citation exists in the Knowledge Model", rej.payload["reason"])
        overrides = [e for e in self.desk.audit.find("pep.action", "blocked")
                     if e.detail["target"] == "verifier.override"]
        self.assertEqual(len(overrides), len(cases))  # each failed check tried to override, and was refused
        self.assertEqual(self.desk.bus.dead_letters, [])  # a bad date is rejected, never crashes the verifier

    async def test_imaging_checks(self):
        await self.verify(MessageType.IMAGING_SIGNAL, IMAGING, "imaging_reader")
        self.assertEqual(len(self.passed_on()), 1)
        bad = {
            "uncalibrated": {"p_calibrated": IMAGING["p_raw"]},
            "out of distribution": {"quality": {**IMAGING["quality"], "in_distribution": False}},
            "diagnosis claim": {"note": "Diagnosis: renal cell carcinoma"},
        }
        for name, change in bad.items():
            await self.verify(MessageType.IMAGING_SIGNAL, {**IMAGING, **change}, "imaging_reader")
            self.assertEqual(len(self.passed_on()), 1, name)
        self.assertEqual(len(self.rejections()), 3)
        self.assertTrue(all(r.to == "imaging_reader" for r in self.rejections()))

    async def test_genomic_context_checks(self):
        good = {"kind": "genomic_context", "citations": [KM.citations["ACMG2015"]], "data": {
            "findings": [{"gene": "VHL", "lab_classification": "pathogenic"}],
            "disclosure": "Disclosure to the patient requires a geneticist (gate genetic_disclosure)."}}
        await self.verify(MessageType.DATA_DELIVERED, good, "genomics_interpreter")
        self.assertEqual(len(self.passed_on()), 1)
        relabelled = copy.deepcopy(good)
        relabelled["data"]["findings"][0]["lab_classification"] = ""
        await self.verify(MessageType.DATA_DELIVERED, relabelled, "genomics_interpreter")
        fake = {**good, "citations": ["[99] Invented 2030"]}
        await self.verify(MessageType.DATA_DELIVERED, fake, "genomics_interpreter")
        self.assertEqual(len(self.passed_on()), 1)
        self.assertEqual(len(self.rejections()), 2)

    async def test_freshness_follows_the_clock(self):
        self.desk.clock.advance(timedelta(days=200))
        await self.verify(MessageType.THERAPY_CONFLICT, CONFLICT, "renal_guardian")
        self.assertEqual(self.passed_on(), [])

    async def test_policy(self):
        ctx = self.rt.context()
        for action, refusal in (("verifier.override", "pass_failed_check"), ("fhir.write", "alter_record"),
                                ("llm.external_identifiable", "transmit_identifiable_external")):
            with self.assertRaises(PolicyViolation) as cm:
                ctx.act(action)
            self.assertEqual(cm.exception.refusal, refusal)
        self.assertEqual(ctx.visible_tools(), ["km.query", "a2a.send"])
        for tool in ("fhir.get_observations", "pacs.fetch_study"):
            with self.assertRaises(PolicyViolation):
                ctx.pep.check_tool(tool)


if __name__ == "__main__":
    unittest.main()
