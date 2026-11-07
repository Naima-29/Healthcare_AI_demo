"""Clinical Board (Person 5): gates, role checks, signed decisions end to end with the Case Briefer."""

import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from kcd_core.contracts import MessageType
from kcd_core.signing import Signer

from kcd.board import BoardError, Clinician, ClinicalBoard
from kcd.desk import Desk
from kcd.fhir import FolderFhir
from kcd.knowledge import KnowledgeModel
from kcd.seats.briefer_tools import briefer_tools
from kcd.seats.case_briefer import CaseBriefer

ROOT = Path(__file__).resolve().parents[2]
FIX = ROOT / "contracts" / "fixtures"
MARCO = Path(__file__).resolve().parents[1] / "kcd" / "fixtures" / "marco-r"
NOW = datetime(2026, 10, 12, 19, 40, tzinfo=UTC)
PT = "pt_marco"


def fx(name: str) -> dict:
    return json.loads((FIX / f"{name}.json").read_text(encoding="utf-8"))["valid"][0]


class ClinicalBoardTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / PT).mkdir()
        (root / PT / "fhir-bundle.json").write_text((MARCO / "fhir-bundle.json").read_text("utf-8"))
        self.desk = await Desk.create(start=NOW)
        # the real clinicians' keys must be the ones the PEP trusts
        self.people = {}
        for cid, name, role in (("dr_rossi", "Dr. Rossi", "clinician:nephrology"),
                                ("dr_neri", "Dr. Neri", "clinician:radiology"),
                                ("dr_gallo", "Dr. Gallo", "clinician:genetics")):
            s = self.desk.clinician if cid == "dr_rossi" else Signer.generate(cid)
            self.desk.clinician_keys.add(cid, s.public_key)
            self.people[cid] = Clinician(cid, name, role, s)
        self.confirmed = []
        self.board = ClinicalBoard(list(self.people.values()), self.desk.clock.now,
                                   confirm_rule=lambda *a: self.confirmed.append(a))
        self.desk.gates = self.desk.dispatcher.gates = self.board
        self.seat = CaseBriefer(now=self.desk.clock.now)
        self.rt = await self.desk.add_seat(self.seat, briefer_tools(
            FolderFhir(root), KnowledgeModel.load(), self.desk.clock.now, self.board.create_item))
        self.companion = await self._companion()
        self.board.decide = lambda pid, rel, items, note: self.seat.publish_decision(
            self.rt.context(), patient_id=pid, release=rel, items=items, note=note)

    async def _companion(self):
        from kcd.standins import StandInCompanion
        c = StandInCompanion(KnowledgeModel.load())
        await self.desk.add_seat(c)
        return c

    async def asyncTearDown(self):
        await self.desk.close()
        self.tmp.cleanup()

    async def feed(self, type_, payload, sender):
        await self.desk.deliver(self.desk.envelope(type_, sender, "case_briefer", payload, patient=PT))

    async def open_pack(self, with_imaging=True):
        await self.feed(MessageType.RED_FLAG_ESCALATION, fx("red_flag_escalation"), "companion")
        await self.feed(MessageType.THERAPY_CONFLICT, fx("therapy_conflict"), "safety_verifier")
        if with_imaging:
            await self.feed(MessageType.IMAGING_SIGNAL, fx("imaging_signal"), "safety_verifier")
        await self.seat.publish_case_pack(self.rt.context(), patient_id=PT)
        await self.desk.bus.drain()
        return next(i for i in self.board.items if i["kind"] == "case_pack" and i["status"] == "open")

    async def test_pack_becomes_one_open_item_with_detail_merged(self):
        item = await self.open_pack()
        self.assertEqual(item["required_gates"], ["clinician_signoff", "radiologist_review", "nephrologist_review"])
        self.assertTrue(item["data"]["draft"])
        self.assertEqual(len([i for i in self.board.items if i["kind"] == "case_pack"]), 1)

    async def test_cannot_sign_before_specialists_review(self):
        item = await self.open_pack()
        with self.assertRaises(BoardError) as cm:
            await self.board.sign(item["id"], "dr_rossi")
        self.assertIn("radiologist_review", str(cm.exception))
        with self.assertRaises(BoardError):  # wrong role
            self.board.review(item["id"], "radiologist_review", "dr_rossi")

    async def test_full_flow_signed_wording_is_what_the_patient_hears(self):
        item = await self.open_pack()
        self.board.review(item["id"], "radiologist_review", "dr_neri")
        self.board.review(item["id"], "nephrologist_review", "dr_rossi")
        draft_codes = [d["code"] for d in item["data"]["draft"]]
        sel = [{"code": c, "selected": c in ("stop_nsaid", "urgent_visit")} for c in draft_codes]
        for s in sel:
            if s["code"] == "stop_nsaid":
                s["patient_text"] = "Stop ibuprofen from today; take paracetamol only."
        decision = await self.board.sign(item["id"], "dr_rossi", sel, note="call him tonight")
        await self.desk.bus.drain()
        self.assertEqual([i.code for i in decision.items], ["urgent_visit", "stop_nsaid"])
        self.assertEqual(decision.signed_by, "dr_rossi")
        self.assertEqual(decision.role, "nephrologist")
        self.assertEqual(self.companion.told_patient[1]["text"], "Stop ibuprofen from today; take paracetamol only.")
        self.assertEqual(self.board.get(item["id"])["status"], "signed")
        with self.assertRaises(BoardError):  # signed once
            await self.board.sign(item["id"], "dr_rossi")

    async def test_new_version_supersedes_and_old_cannot_be_signed(self):
        old = await self.open_pack(with_imaging=False)
        await self.feed(MessageType.SYMPTOM_REPORT, fx("symptom_report"), "companion")
        await self.seat.publish_case_pack(self.rt.context(), patient_id=PT)
        await self.desk.bus.drain()
        self.assertEqual(self.board.get(old["id"])["status"], "superseded")
        with self.assertRaises(BoardError):
            await self.board.sign(old["id"], "dr_rossi")

    async def test_unknown_clinician_and_empty_decision(self):
        item = await self.open_pack(with_imaging=False)
        self.board.review(item["id"], "nephrologist_review", "dr_rossi")
        with self.assertRaises(BoardError):
            await self.board.sign(item["id"], "someone_else")
        with self.assertRaises(BoardError):
            await self.board.sign(item["id"], "dr_rossi", [{"code": "stop_nsaid", "selected": False}])
        with self.assertRaises(BoardError):
            await self.board.sign(item["id"], "dr_rossi", [{"code": "made_up"}])

    async def test_rule_confirmation_and_events(self):
        events = []
        self.board.subscribe(events.append)
        rid = self.board.create_item({"kind": "rule", "gate": "rule_confirmation", "patient_id": PT, "case_id": "c1",
                                      "title": "Confirm trial rule", "artifact_id": "rule_1",
                                      "artifact_hash": "sha256:" + "a" * 64, "data": {"key": "avoid_drug_class:NSAID"}})
        self.board.confirm_rule(rid, "dr_rossi")
        self.assertEqual(self.confirmed[0][0], "rule_1")
        self.assertEqual(self.confirmed[0][3].clinician_id, "dr_rossi")
        self.assertEqual([e["op"] for e in events], ["create", "update"])
        with self.assertRaises(BoardError):
            self.board.confirm_rule(rid, "dr_rossi")  # already confirmed

    async def test_open_gate_is_idempotent(self):
        item = await self.open_pack(with_imaging=False)
        env, pack = self.board.opened[0]
        await self.board.open_gate(env, pack)
        self.assertEqual(len([i for i in self.board.items if i["kind"] == "case_pack"]), 1)
        self.assertEqual(item["status"], "open")


if __name__ == "__main__":
    unittest.main()
