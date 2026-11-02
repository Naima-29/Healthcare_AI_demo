import unittest

from kcd_core.audit import content_hash
from kcd_core.contracts import MessageType
from kcd_core.pep import PolicyEnforcementPoint, PolicyViolation

from kcd.cortex import Cortex, HumanActor, HumanRequired
from kcd.desk import Desk
from kcd.seats.archivist import Archivist, rule_hash
from kcd.tools import board_tools

STOP_NSAID = {"code": "stop_nsaid", "patient_text": "Please stop ibuprofen.", "clinician_text": "NSAID x CKD G3a"}
URGENT = {"code": "urgent_visit", "patient_text": "Go to the emergency department today."}
DR = HumanActor("dr_rossi", "clinician:nephrology")


class ArchivistTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.desk = await Desk.create()
        self.cortex = Cortex()
        self.board: dict[str, dict] = {}
        self.seat = Archivist(self.cortex)
        self.rt = await self.desk.add_seat(self.seat, board_tools(lambda aid: self.board[aid]))

    async def asyncTearDown(self):
        await self.desk.close()

    def publish_artifact(self, items, artifact_id="cp_1", role="clinician:nephrology", tamper=None):
        artifact = {"case_id": "case-1", "items": items}
        h = content_hash(artifact)
        release = self.desk.release(artifact_id, h, role=role)
        self.board[artifact_id] = {"artifact": dict(tamper or artifact), "release": release}
        return h

    async def decide(self, items, *, decision="approved", artifact_id="cp_1", role="clinician:nephrology",
                     tamper=None, artifact_hash=None, decision_role=None):
        h = self.publish_artifact(items, artifact_id, role, tamper)
        env = self.desk.envelope(MessageType.CLINICIAN_DECISION, "case_briefer", "archivist", {
            "artifact_id": artifact_id, "artifact_hash": artifact_hash or h, "release_id": "rel_1",
            "decision": decision, "clinician_role": decision_role or role, "patient_explanation_points": []})
        await self.desk.deliver(env)
        return env

    def acks(self):
        return [e for e in self.desk.bus.published if e[1].type == MessageType.ACKNOWLEDGMENT
                and e[1].from_ == "archivist"]

    def blocked(self):
        return self.desk.audit.find("seat.policy_violation", "blocked")

    async def test_decision_becomes_evidence_trial_rules_and_gates(self):
        await self.decide([STOP_NSAID, URGENT, {"code": "explain", "patient_text": "..."}])
        self.assertEqual(self.desk.bus.dead_letters, [])
        patient_rules = self.cortex.list_knowledge(3, "pt_marco")
        self.assertEqual([(k["key"], k["status"]) for k in patient_rules], [("avoid_drug_class:NSAID", "shadow")])
        decision_ev = self.cortex.get_evidence(3, "pt_marco", patient_rules[0]["evidence_ids"][0])
        self.assertEqual(decision_ev["kind"], "clinician_decision")
        self.assertEqual(decision_ev["source"], "clinician:dr_rossi")

        hospital_rules = self.cortex.list_knowledge(2, "hospital")
        self.assertEqual(hospital_rules[0]["key"], "routing:rcc_red_flag")
        precedent = self.cortex.get_evidence(2, "hospital", hospital_rules[0]["evidence_ids"][0])
        self.assertNotIn("pt_marco", str(precedent["content"]))  # no patient identifier in shared memory

        gates = [pack for _, pack in self.desk.gates.opened]
        self.assertEqual({g.artifact_id for g in gates}, {patient_rules[0]["id"], hospital_rules[0]["id"]})
        self.assertTrue(all(g.gate_role == "clinician:nephrology" for g in gates))
        self.assertEqual(gates[0].artifact_hash, rule_hash(self.cortex.get_knowledge(gates[0].artifact_id)))
        self.assertEqual(self.acks()[0][1].payload["status"], "accepted")

    async def test_rule_reused_only_after_human_confirmation(self):
        await self.decide([STOP_NSAID])
        ctx = self.rt.context()
        self.assertEqual(self.seat.rules_for_next_episode(ctx, "pt_marco"), [])
        rule_id = self.desk.gates.opened[0][1].artifact_id
        with self.assertRaises(HumanRequired):
            self.cortex.confirm(rule_id, HumanActor("seat:archivist", "clinician:nephrology"))
        self.cortex.confirm(rule_id, DR)
        self.assertEqual([k["key"] for k in self.seat.rules_for_next_episode(ctx, "pt_marco")],
                         ["avoid_drug_class:NSAID"])

    async def test_archivist_cannot_confirm(self):
        pep: PolicyEnforcementPoint = self.rt.pep
        with self.assertRaises(PolicyViolation) as cm:
            pep.check_tool("cortex.confirm_rule")
        self.assertEqual(cm.exception.refusal, "no_rule_self_confirmation")
        with self.assertRaises(PolicyViolation) as cm:
            pep.check_tool("model.train_on_patient_data")
        self.assertEqual(cm.exception.refusal, "no_patient_data_in_weights")

    async def test_tampered_or_mismatched_decisions_store_nothing(self):
        cases = [
            ({"tamper": {"case_id": "case-1", "items": [URGENT]}}, "artifact hash"),   # artifact changed after signing
            ({"artifact_hash": "sha256:" + "1" * 64}, "artifact hash"),               # decision names another hash
            ({"decision_role": "clinician:radiology"}, "role"),                       # decision claims another role
        ]
        for i, (extra, reason) in enumerate(cases):
            await self.decide([STOP_NSAID], artifact_id=f"cp_{i}", **extra)
            self.assertIn(reason, self.blocked()[i].detail["error"])
        self.assertEqual(len(self.blocked()), 3)
        self.assertEqual(self.cortex.list_evidence(3, "pt_marco"), [])
        self.assertEqual(self.cortex.list_knowledge(3, "pt_marco"), [])
        self.assertEqual(self.desk.gates.opened, [])

    async def test_release_signed_with_another_key_is_refused(self):
        from kcd_core.signing import Signer, sign_release
        h = self.publish_artifact([STOP_NSAID])
        forged = sign_release(self.board["cp_1"]["release"].model_copy(update={"sig": None}),
                              Signer.generate("dr_rossi"))
        self.board["cp_1"]["release"] = forged
        env = self.desk.envelope(MessageType.CLINICIAN_DECISION, "case_briefer", "archivist", {
            "artifact_id": "cp_1", "artifact_hash": h, "release_id": "rel_1", "decision": "approved",
            "clinician_role": "clinician:nephrology", "patient_explanation_points": []})
        await self.desk.deliver(env)
        self.assertIn("invalid clinician release", self.blocked()[0].detail["error"])
        self.assertEqual(self.cortex.list_evidence(3, "pt_marco"), [])

    async def test_rejected_decision_is_kept_as_evidence_without_rules(self):
        await self.decide([STOP_NSAID], decision="rejected")
        self.assertEqual(len(self.cortex.list_evidence(3, "pt_marco")), 1)
        self.assertEqual(self.cortex.list_knowledge(3, "pt_marco"), [])
        self.assertEqual(self.acks()[0][1].payload["status"], "accepted")

    async def test_malformed_items_are_rejected_without_writing(self):
        await self.decide([STOP_NSAID, {"code": "x", "patient_text": "y", "rule": {"value": 1}}])
        self.assertEqual(self.acks()[0][1].payload["status"], "rejected")
        self.assertIn("rule needs 'key'", self.acks()[0][1].payload["reason"])
        self.assertEqual(self.cortex.list_evidence(3, "pt_marco"), [])
        self.assertEqual(self.cortex.list_knowledge(3, "pt_marco"), [])

    async def test_explicit_rules(self):
        await self.decide([{"code": "custom", "patient_text": "x",
                            "rule": {"key": "follow_up:ct_interval_months", "value": 3, "confidence": 0.8}}])
        k = self.cortex.list_knowledge(3, "pt_marco")[0]
        self.assertEqual((k["key"], k["value"], k["confidence"]), ("follow_up:ct_interval_months", 3, 0.8))

    async def test_redelivered_message_is_processed_once(self):
        env = await self.decide([STOP_NSAID])
        # The bus redelivers the same envelope straight to the seat (the Dispatcher would dedupe it first).
        await self.rt._on_message(env)
        self.assertEqual(len(self.cortex.list_knowledge(3, "pt_marco")), 1)
        self.assertEqual(len(self.cortex.list_evidence(3, "pt_marco")), 1)
        self.assertEqual(len(self.desk.gates.opened), 1)

    async def test_new_decision_on_same_rule_makes_a_new_version(self):
        await self.decide([STOP_NSAID], artifact_id="cp_1")
        await self.decide([STOP_NSAID, {"code": "note", "patient_text": "second visit"}], artifact_id="cp_2")
        self.assertEqual([k["version"] for k in self.cortex.list_knowledge(3, "pt_marco")], [1, 2])


if __name__ == "__main__":
    unittest.main()
