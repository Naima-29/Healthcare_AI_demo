import unittest

from kcd.contracts import ContractError, Envelope
from kcd.cortex import Cortex, HumanActor, HumanRequired
from kcd.seatkit import InMemoryGates, InMemoryOutbox, RefusalError
from kcd.seats.archivist import Archivist

DR = HumanActor("dr-rossi", "nephrologist")


def decision(items, signed_by="dr-rossi", role="nephrologist", case_id="case-1", msg_id=None):
    env = Envelope(type="clinician_decision", sender="case_briefer", to="archivist", patient="marco-r",
                   payload={"case_id": case_id, "signed_by": signed_by, "role": role, "items": items})
    if msg_id:
        env.id = msg_id
    return env


STOP_NSAID = {"code": "stop_nsaid", "patient_text": "Please stop ibuprofen.", "clinician_text": "NSAID x CKD G3a"}
URGENT = {"code": "urgent_visit", "patient_text": "Go to the emergency department today."}


class ArchivistTest(unittest.TestCase):
    def setUp(self):
        self.cortex = Cortex()
        self.gates = InMemoryGates()
        self.a = Archivist(self.cortex, InMemoryOutbox(), self.gates)

    def test_decision_becomes_evidence_and_trial_rules_with_gates(self):
        r = self.a.handle(decision([STOP_NSAID, URGENT, {"code": "explain", "patient_text": "..."}]))
        self.assertEqual(len(r["proposals"]), 2)
        self.assertEqual(len(self.gates.opened), 2)
        self.assertTrue(all(g["gate"] == "rule_confirmation" for g in self.gates.opened))

        l3 = self.cortex.list_knowledge(3, "marco-r")
        self.assertEqual([(k["key"], k["status"]) for k in l3], [("avoid_drug_class:NSAID", "shadow")])
        self.assertEqual(l3[0]["evidence_ids"], [r["evidence_id"]])
        decision_ev = self.cortex.get_evidence(3, "marco-r", r["evidence_id"])
        self.assertEqual(decision_ev["kind"], "clinician_decision")

        l2 = self.cortex.list_knowledge(2, "hospital")
        self.assertEqual(l2[0]["key"], "routing:rcc_red_flag")
        precedent = self.cortex.get_evidence(2, "hospital", l2[0]["evidence_ids"][0])
        self.assertNotIn("marco-r", str(precedent["content"]))  # no patient identifier at L2

    def test_learning_loop_rule_reused_only_after_human_confirmation(self):
        self.a.handle(decision([STOP_NSAID]))
        self.assertEqual(self.a.rules_for_next_episode("marco-r"), [])
        rule_id = self.gates.opened[0]["artifact"]["knowledge_id"]
        with self.assertRaises(HumanRequired):
            self.cortex.confirm(rule_id, HumanActor("seat:archivist", "clinician"))
        self.cortex.confirm(rule_id, DR)
        rules = self.a.rules_for_next_episode("marco-r")
        self.assertEqual([k["key"] for k in rules], ["avoid_drug_class:NSAID"])

    def test_archivist_cannot_confirm(self):
        self.assertFalse(hasattr(self.a.memory, "confirm"))
        self.assertNotIn("cortex.confirm_rule", self.a.manifest.tools)
        with self.assertRaises(RefusalError) as cm:
            self.a.act("cortex.confirm_rule")
        self.assertEqual(cm.exception.refusal, "auto_confirm_rule")

    def test_unsigned_or_seat_signed_decision_stores_nothing(self):
        for signer, role in (("", "nephrologist"), ("seat:case_briefer", "nephrologist"), ("dr-x", "robot")):
            with self.assertRaises(RefusalError) as cm:
                self.a.handle(decision([STOP_NSAID], signed_by=signer, role=role))
            self.assertEqual(cm.exception.refusal, "store_without_evidence")
        self.assertEqual(self.cortex.list_evidence(3, "marco-r"), [])
        self.assertEqual(self.cortex.list_knowledge(3, "marco-r"), [])

    def test_malformed_decision_is_rejected(self):
        env = decision([{"code": "stop_nsaid"}])  # missing patient_text
        with self.assertRaises(ContractError):
            self.a.handle(env)

    def test_redelivered_message_is_processed_once(self):
        first = self.a.handle(decision([STOP_NSAID], msg_id="msg_1"))
        again = self.a.handle(decision([STOP_NSAID], msg_id="msg_1"))
        self.assertTrue(again["duplicate"])
        self.assertEqual(again["proposals"], first["proposals"])
        self.assertEqual(len(self.cortex.list_knowledge(3, "marco-r")), 1)
        self.assertEqual(len(self.cortex.list_evidence(3, "marco-r")), 1)

    def test_new_decision_on_same_rule_makes_a_new_version(self):
        self.a.handle(decision([STOP_NSAID], msg_id="msg_1"))
        self.a.handle(decision([STOP_NSAID], msg_id="msg_2", case_id="case-2"))
        versions = [k["version"] for k in self.cortex.list_knowledge(3, "marco-r")]
        self.assertEqual(versions, [1, 2])

    def test_explicit_rule_items(self):
        r = self.a.handle(decision([{"code": "custom", "patient_text": "x",
                                     "rule": {"key": "follow_up:ct_interval_months", "value": 3, "confidence": 0.8}}]))
        k = self.cortex.get_knowledge(r["proposals"][0])
        self.assertEqual((k["key"], k["value"], k["confidence"]), ("follow_up:ct_interval_months", 3, 0.8))
        with self.assertRaises(ValueError):
            self.a.handle(decision([{"code": "bad", "patient_text": "x", "rule": {"value": 1}}], msg_id="m3"))
        with self.assertRaises(RefusalError):
            self.a.handle(decision([STOP_NSAID, {"code": "l0", "patient_text": "x",
                                                 "rule": {"layer": 0, "key": "k", "value": 1}}], msg_id="m4"))
        # the rejected decisions left nothing behind: one decision, one rule
        self.assertEqual(len(self.cortex.list_evidence(3, "marco-r")), 1)
        self.assertEqual(len(self.cortex.list_knowledge(3, "marco-r")), 1)

    def test_other_messages_are_kept_as_evidence(self):
        env = Envelope(type="red_flag_escalation", sender="companion", to="archivist", patient="marco-r",
                       payload={"urgency": "high"})
        r = self.a.handle(env)
        self.assertEqual(self.cortex.get_evidence(3, "marco-r", r["evidence_id"])["kind"], "red_flag_escalation")
        ignored = Envelope(type="symptom_report", sender="companion", to="archivist", patient="marco-r", payload={})
        self.assertIsNone(self.a.handle(ignored))


if __name__ == "__main__":
    unittest.main()
