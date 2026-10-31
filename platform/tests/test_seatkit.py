import unittest

from kcd import contracts
from kcd.seatkit import InMemoryOutbox, Manifest, RefusalError, Seat, is_diagnosis_claim


class ManifestTest(unittest.TestCase):
    def test_all_manifests_load(self):
        for seat in ("imaging_reader", "genomics_interpreter", "archivist"):
            m = Manifest.load(seat)
            self.assertEqual(m.seat, seat)
            self.assertEqual(m.a2a_peers, ("dispatcher",))
            self.assertTrue(m.gates)

    def test_invalid_manifests(self):
        base = Manifest.load("archivist").raw
        for change in ({"refuses": ["not_a_refusal"]}, {"memory": {"read": [5], "write": []}}):
            with self.assertRaises(ValueError):
                Manifest.from_dict({**base, **change})
        broken = dict(base)
        del broken["gates"]
        with self.assertRaises(ValueError):
            Manifest.from_dict(broken)


class SeatTest(unittest.TestCase):
    def setUp(self):
        self.outbox = InMemoryOutbox()
        self.seat = Seat(Manifest.load("imaging_reader"), self.outbox)

    def test_tool_and_gate_checks_are_audited(self):
        with self.assertRaises(RefusalError):
            self.seat.require_tool("fhir.write")
        with self.assertRaises(RuntimeError):
            self.seat.open_gate("radiologist_review", "p", "t", {})  # no gate service: fail loudly
        self.assertEqual(self.seat.audit_events[0]["refusal"], "tool_not_in_manifest")
        self.assertEqual(self.seat.audit_events[-1]["decision"], "failed")

    def test_send_validates_contract(self):
        with self.assertRaises(contracts.ContractError):
            self.seat.send("imaging_signal", "p", {"label": "tumor"})
        with self.assertRaises(contracts.ContractError):
            self.seat.send("not_a_type", "p", {})
        self.assertEqual(self.outbox.sent, [])

    def test_diagnosis_guard(self):
        allowed = ["Preliminary model signal. It is not a diagnosis.", "No diagnosis can be made.",
                   "Signal: tumor (p=0.94), for radiologist review"]
        blocked = ["Diagnosis: RCC", "diagnosed with renal cancer", "This confirms the tumour",
                   "The lesion is malignant", "consistent with malignancy", "definitive finding"]
        self.assertEqual([is_diagnosis_claim(t) for t in allowed], [False] * 3)
        self.assertEqual([is_diagnosis_claim(t) for t in blocked], [True] * 6)


class ContractsTest(unittest.TestCase):
    def ok_signal(self, **kw):
        p = {"status": "ok", "label": "tumor", "p_calibrated": 0.9, "quality": {"ok": True},
             "provenance": {"study_uid": "1"}}
        p.update(kw)
        return p

    def test_imaging_signal(self):
        contracts.validate_imaging_signal(self.ok_signal())
        contracts.validate_imaging_signal(self.ok_signal(status="unreadable", label="indeterminate", p_calibrated=None))
        for bad in (self.ok_signal(label="mass"), self.ok_signal(p_calibrated=1.2), self.ok_signal(p_calibrated=True),
                    self.ok_signal(status="unreadable"), self.ok_signal(status="maybe"),
                    self.ok_signal(provenance={}), self.ok_signal(quality=[])):
            with self.assertRaises(contracts.ContractError):
                contracts.validate_imaging_signal(bad)

    def test_envelope_dict(self):
        env = contracts.Envelope(type="acknowledgment", sender="a", to="dispatcher", patient="p", payload={})
        d = env.as_dict()
        self.assertEqual((d["from"], d["to"]), ("a", "dispatcher"))
        self.assertTrue(d["id"].startswith("msg_"))


if __name__ == "__main__":
    unittest.main()
