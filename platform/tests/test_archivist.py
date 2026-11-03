import unittest

from kcd_core.contracts import MessageType
from kcd_core.pep import PolicyViolation

from kcd.cortex import Cortex, HumanActor, HumanRequired
from kcd.desk import Desk
from kcd.seats.archivist import Archivist, rule_hash
from kcd.tools import board_tools

STOP_NSAID = {"code": "stop_nsaid", "patient_text": "Please stop ibuprofen.", "clinician_text": "NSAID x CKD G3a"}
URGENT = {"code": "urgent_visit", "patient_text": "Go to the emergency department today."}
HASH = "sha256:" + "a" * 64
DR = HumanActor("dr_rossi", "nephrologist")


def decision(items, *, decision_id="dec-1", signed_by="Dr. Rossi", role="treating_clinician", case_id="case-1"):
    return {"case_id": case_id, "decision_id": decision_id, "artifact_hash": HASH, "signed_by": signed_by,
            "role": role, "note": "", "items": items}


class ArchivistTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.desk = await Desk.create()
        self.cortex = Cortex()
        self.seat = Archivist(self.cortex)
        self.rt = await self.desk.add_seat(self.seat, board_tools(self.desk.gates.create_item))

    async def asyncTearDown(self):
        await self.desk.close()

    async def decide(self, payload):
        env = self.desk.envelope(MessageType.CLINICIAN_DECISION, "case_briefer", "archivist", payload)
        await self.desk.deliver(env)
        return env

    def blocked(self):
        return self.desk.audit.find("seat.policy_violation", "blocked")

    async def test_decision_becomes_evidence_trial_rules_and_board_items(self):
        await self.decide(decision([STOP_NSAID, URGENT, {"code": "explain", "patient_text": "..."}]))
        self.assertEqual(self.desk.bus.dead_letters, [])
        patient_rules = self.cortex.list_knowledge(3, "pt_marco")
        self.assertEqual([(k["key"], k["status"]) for k in patient_rules], [("avoid_drug_class:NSAID", "shadow")])
        decision_ev = self.cortex.get_evidence(3, "pt_marco", patient_rules[0]["evidence_ids"][0])
        self.assertEqual((decision_ev["kind"], decision_ev["source"]), ("clinician_decision", "clinician:Dr. Rossi"))

        hospital_rules = self.cortex.list_knowledge(2, "hospital")
        self.assertEqual(hospital_rules[0]["key"], "routing:rcc_red_flag")
        precedent = self.cortex.get_evidence(2, "hospital", hospital_rules[0]["evidence_ids"][0])
        self.assertNotIn("pt_marco", str(precedent["content"]))  # no patient identifier in shared memory

        items = self.desk.gates.items
        self.assertEqual({i["artifact_id"] for i in items}, {patient_rules[0]["id"], hospital_rules[0]["id"]})
        self.assertTrue(all(i["gate"] == "rule_confirmation" for i in items))
        self.assertEqual(items[0]["artifact_hash"], rule_hash(self.cortex.get_knowledge(items[0]["artifact_id"])))

    async def test_rule_reused_only_after_human_confirmation(self):
        await self.decide(decision([STOP_NSAID]))
        ctx = self.rt.context()
        self.assertEqual(self.seat.rules_for_next_episode(ctx, "pt_marco"), [])
        rule_id = self.desk.gates.items[0]["artifact_id"]
        with self.assertRaises(HumanRequired):
            self.cortex.confirm(rule_id, HumanActor("seat:archivist", "clinician"))
        self.cortex.confirm(rule_id, DR)
        self.assertEqual([k["key"] for k in self.seat.rules_for_next_episode(ctx, "pt_marco")],
                         ["avoid_drug_class:NSAID"])

    async def test_archivist_cannot_confirm_or_train(self):
        ctx = self.rt.context()
        with self.assertRaises(PolicyViolation) as cm:
            ctx.act("cortex.confirm_rule")
        self.assertEqual(cm.exception.refusal, "auto_confirm_rule")
        with self.assertRaises(PolicyViolation) as cm:
            ctx.act("model.train_general")
        self.assertEqual(cm.exception.refusal, "write_general_weights")
        self.assertFalse(hasattr(self.seat.memory(ctx, "pt_marco"), "confirm"))

    async def test_unsigned_or_seat_signed_decision_stores_nothing(self):
        for i, (signer, role) in enumerate([(" ", "treating_clinician"), ("seat:case_briefer", "treating_clinician"),
                                            ("Dr. X", "robot"), ("Dr. X", None)]):
            await self.decide(decision([STOP_NSAID], decision_id=f"dec-{i}", signed_by=signer, role=role))
        self.assertEqual(len(self.blocked()), 4)
        self.assertTrue(all("not signed by a clinician" in e.detail["error"] for e in self.blocked()))
        self.assertEqual(self.cortex.list_evidence(3, "pt_marco"), [])
        self.assertEqual(self.cortex.list_knowledge(3, "pt_marco"), [])

    async def test_same_decision_is_processed_once(self):
        env = await self.decide(decision([STOP_NSAID]))
        await self.rt._on_message(env)  # the bus redelivers the same envelope to the seat
        self.assertEqual(len(self.cortex.list_knowledge(3, "pt_marco")), 1)
        self.assertEqual(len(self.cortex.list_evidence(3, "pt_marco")), 1)
        self.assertEqual(len(self.desk.gates.items), 1)

    async def test_new_decision_on_same_rule_makes_a_new_version(self):
        await self.decide(decision([STOP_NSAID], decision_id="dec-1"))
        await self.decide(decision([STOP_NSAID], decision_id="dec-2", case_id="case-2"))
        self.assertEqual([k["version"] for k in self.cortex.list_knowledge(3, "pt_marco")], [1, 2])

    async def test_red_flags_and_lab_reports_are_kept_as_evidence(self):
        flag = {"urgency": "high", "pattern": "p", "evidence": ["visible blood in urine"], "safety_script_sent": True}
        await self.desk.deliver(self.desk.envelope(MessageType.RED_FLAG_ESCALATION, "companion", "archivist", flag))
        lab = {"kind": "lab", "data": {"egfr": 52, "date": "2026-10-08"}}
        await self.desk.deliver(self.desk.envelope(MessageType.DATA_DELIVERED, "companion", "archivist", lab))
        kinds = [e["kind"] for e in self.cortex.list_evidence(3, "pt_marco")]
        self.assertEqual(kinds, ["red_flag_escalation", "data_delivered"])

    async def test_only_the_case_briefer_may_send_decisions(self):
        env = self.desk.envelope(MessageType.CLINICIAN_DECISION, "companion", "archivist", decision([STOP_NSAID]))
        await self.desk.deliver(env)
        self.assertEqual(len(self.desk.bus.dead_letters), 1)  # rejected by the Dispatcher's route check
        self.assertEqual(self.cortex.list_evidence(3, "pt_marco"), [])


if __name__ == "__main__":
    unittest.main()
