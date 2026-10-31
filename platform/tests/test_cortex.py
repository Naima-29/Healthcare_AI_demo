import tempfile
import threading
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from kcd_core.audit import InMemoryAudit
from kcd_core.clock import FakeClock
from kcd_core.contracts import PolicyCatalog, Release
from kcd_core.pep import PolicyEnforcementPoint, PolicyViolation
from kcd_core.registry import ManifestRegistry
from kcd_core.signing import KeyRing, Signer, sign_release
from sqlalchemy import text, update

from kcd.cortex import (
    CoreMemory,
    Cortex,
    EvidenceRequired,
    FileBlobStore,
    HumanActor,
    HumanRequired,
    InvalidTransition,
    NotFound,
    schema,
)
from kcd.cortex.auth import release_verifier
from kcd.desk import CATALOG_PATH, MANIFEST_DIR

DR = HumanActor("dr-rossi", "nephrologist")
DPO = HumanActor("dpo-1", "dpo")


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 12, 21, 40, tzinfo=UTC)

    def __call__(self):
        return self.now


def make(**kw) -> Cortex:
    return Cortex(**kw)


class EvidenceTest(unittest.TestCase):
    def setUp(self):
        self.c = make()

    def test_chain_appends_and_verifies(self):
        a = self.c.append_evidence(3, "p1", kind="x", source="t", content={"a": 1})
        b = self.c.append_evidence(3, "p1", kind="y", source="t", content={"b": 2}, blob=b"img")
        self.assertEqual((a["seq"], b["seq"]), (1, 2))
        self.assertEqual(b["prev_hash"], a["chain_hash"])
        self.assertEqual(self.c.verify_chain(3, "p1"), {"ok": True, "length": 2, "broken_at": None, "seq": None,
                                                         "reason": None})
        self.assertEqual(self.c.read_blob(3, "p1", b["blob_sha256"]), b"img")

    def test_partitions_have_independent_chains(self):
        self.c.append_evidence(3, "p1", kind="x", source="t", content={})
        e = self.c.append_evidence(3, "p2", kind="x", source="t", content={})
        self.assertEqual(e["seq"], 1)
        self.assertEqual(self.c.list_evidence(3, "p1")[0]["partition"], "p1")

    def _tamper(self, **values):
        with self.c.engine.begin() as conn:
            conn.execute(update(schema.evidence).where(schema.evidence.c.seq == 1).values(**values))

    def test_detects_edited_content(self):
        self.c.append_evidence(3, "p1", kind="x", source="t", content={"egfr": 58})
        self.c.append_evidence(3, "p1", kind="x", source="t", content={"egfr": 57})
        self._tamper(content='{"egfr":90}')
        r = self.c.verify_chain(3, "p1")
        self.assertFalse(r["ok"])
        self.assertEqual(r["seq"], 1)
        self.assertIn("content", r["reason"])

    def test_detects_edited_metadata_and_deleted_rows(self):
        self.c.append_evidence(3, "p1", kind="x", source="t", content={})
        self.c.append_evidence(3, "p1", kind="x", source="t", content={})
        self._tamper(kind="forged")
        self.assertFalse(self.c.verify_chain(3, "p1")["ok"])
        c2 = make()
        c2.append_evidence(3, "p1", kind="x", source="t", content={})
        c2.append_evidence(3, "p1", kind="x", source="t", content={})
        with c2.engine.begin() as conn:
            conn.execute(schema.evidence.delete().where(schema.evidence.c.seq == 1))
        self.assertIn("gap", c2.verify_chain(3, "p1")["reason"])

    def test_detects_missing_blob(self):
        e = self.c.append_evidence(3, "p1", kind="x", source="t", content={}, blob=b"img")
        self.c.blobs.delete_partition(3, "p1")
        r = self.c.verify_chain(3, "p1")
        self.assertFalse(r["ok"])
        self.assertEqual(r["broken_at"], e["id"])

    def test_rejects_bad_input(self):
        with self.assertRaises(ValueError):
            self.c.append_evidence(4, "p1", kind="x", source="t", content={})
        with self.assertRaises(ValueError):
            self.c.append_evidence(3, " ", kind="x", source="t", content={})
        with self.assertRaises(ValueError):
            self.c.append_evidence(3, "p1", kind="x", source="t", content=["not", "a", "dict"])
        with self.assertRaises(ValueError):
            self.c.append_evidence(3, "p1", kind="x", source="t", content={"x": float("nan")})
        with self.assertRaises(NotFound):
            self.c.get_evidence(3, "p1", "ev_missing")

    def test_concurrent_appends_keep_one_valid_chain(self):
        with tempfile.TemporaryDirectory() as d:
            c = Cortex(f"sqlite:///{Path(d) / 'cortex.db'}")
            errors = []

            def worker(n):
                try:
                    for i in range(10):
                        c.append_evidence(3, "p1", kind="x", source=f"w{n}", content={"i": i})
                except Exception as e:  # pragma: no cover - reported below
                    errors.append(e)

            threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(errors, [])
            r = c.verify_chain(3, "p1")
            self.assertTrue(r["ok"], r)
            self.assertEqual(r["length"], 40)
            c.engine.dispose()


class KnowledgeTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.c = make(clock=self.clock)
        self.ev = self.c.append_evidence(3, "p1", kind="clinician_decision", source="dr", content={"x": 1})

    def propose(self, key="avoid:NSAID", **kw):
        args = dict(key=key, value={"instruction": "avoid"}, source="dr-rossi", evidence_ids=[self.ev["id"]],
                    proposed_by="seat:archivist")
        args.update(kw)
        return self.c.propose(3, "p1", **args)

    def test_new_rule_is_trial_and_not_active(self):
        k = self.propose()
        self.assertEqual((k["status"], k["version"], k["confidence_effective"]), ("shadow", 1, 0.0))
        self.assertEqual(self.c.active_rules([(3, "p1")]), [])

    def test_rule_needs_existing_evidence_in_same_partition(self):
        with self.assertRaises(EvidenceRequired):
            self.propose(evidence_ids=[])
        with self.assertRaises(EvidenceRequired):
            self.propose(evidence_ids=["ev_does_not_exist"])
        other = self.c.append_evidence(3, "p2", kind="x", source="t", content={})
        with self.assertRaises(EvidenceRequired):
            self.propose(evidence_ids=[other["id"]])

    def test_only_verified_humans_confirm(self):
        k = self.propose()
        for actor in (HumanActor("seat:archivist", "clinician"), HumanActor("", "clinician"),
                      HumanActor("dr-x", "robot"), "dr-rossi"):
            with self.assertRaises(HumanRequired):
                self.c.confirm(k["id"], actor)
        self.assertEqual(self.c.get_knowledge(k["id"])["status"], "shadow")
        done = self.c.confirm(k["id"], DR)
        self.assertEqual((done["status"], done["decided_by"]), ("confirmed", "dr-rossi"))
        self.assertEqual(len(self.c.active_rules([(3, "p1")])), 1)

    def test_transitions(self):
        k = self.propose()
        self.c.confirm(k["id"], DR)
        with self.assertRaises(InvalidTransition):
            self.c.confirm(k["id"], DR)
        with self.assertRaises(InvalidTransition):
            self.c.reject(k["id"], DR, "no")
        with self.assertRaises(ValueError):
            self.c.retire(k["id"], DR, " ")
        self.assertEqual(self.c.retire(k["id"], DR, "therapy changed")["status"], "retired")
        with self.assertRaises(InvalidTransition):
            self.c.reverify(k["id"], DR)
        r = self.propose(key="other")
        self.assertEqual(self.c.reject(r["id"], DR, "not relevant")["status"], "rejected")
        with self.assertRaises(NotFound):
            self.c.confirm("ko_missing", DR)

    def test_new_version_supersedes_old_on_confirm(self):
        v1 = self.c.confirm(self.propose()["id"], DR)
        v2 = self.propose(value={"instruction": "avoid, use paracetamol"})
        self.assertEqual(v2["version"], 2)
        self.assertEqual(self.c.get_knowledge(v1["id"])["status"], "confirmed")  # until v2 is confirmed
        self.c.confirm(v2["id"], DR)
        self.assertEqual(self.c.get_knowledge(v1["id"])["status"], "superseded")
        active = self.c.active_rules([(3, "p1")])
        self.assertEqual([a["id"] for a in active], [v2["id"]])

    def test_confidence_decays_and_reverify_restores(self):
        k = self.c.confirm(self.propose(confidence=0.8, half_life_days=100)["id"], DR)
        self.assertAlmostEqual(k["confidence_effective"], 0.8)
        self.clock.now += timedelta(days=100)
        self.assertAlmostEqual(self.c.get_knowledge(k["id"])["confidence_effective"], 0.4)
        self.assertEqual(self.c.active_rules([(3, "p1")]), [])  # 0.4 < 0.5
        self.c.reverify(k["id"], DR)
        self.assertAlmostEqual(self.c.get_knowledge(k["id"])["confidence_effective"], 0.8)
        self.assertEqual(len(self.c.active_rules([(3, "p1")])), 1)

    def test_validation(self):
        with self.assertRaises(ValueError):
            self.propose(confidence=0)
        with self.assertRaises(ValueError):
            self.propose(confidence=1.5)
        with self.assertRaises(ValueError):
            self.propose(half_life_days=0)
        with self.assertRaises(ValueError):
            self.c.list_knowledge(3, "p1", status="maybe")


class ErasureTest(unittest.TestCase):
    def test_erase_patient_removes_everything_and_keeps_anonymous_receipt(self):
        with tempfile.TemporaryDirectory() as d:
            c = Cortex(blobs=FileBlobStore(d))
            hooked = []
            c.register_erase_hook(hooked.append)
            ev = c.append_evidence(3, "marco-r", kind="ct", source="pacs", content={"a": 1}, blob=b"image")
            k = c.propose(3, "marco-r", key="k", value=1, source="dr", evidence_ids=[ev["id"]], proposed_by="seat:a")
            c.confirm(k["id"], DR)
            c.mark_processed("study:1", 3, "marco-r", {"x": 1})
            other = c.append_evidence(3, "anna-b", kind="ct", source="pacs", content={}, blob=b"other")
            hosp = c.append_evidence(2, "hospital", kind="routing_precedent", source="t", content={})

            with self.assertRaises(HumanRequired):
                c.erase_patient("marco-r", HumanActor("seat:archivist", "clinician"), "x")
            with self.assertRaises(ValueError):
                c.erase_patient("marco-r", DPO, "")
            receipt = c.erase_patient("marco-r", DPO, "patient request (GDPR art. 17)")

            self.assertEqual((receipt["evidence_rows"], receipt["knowledge_rows"], receipt["blobs_deleted"]), (1, 1, 1))
            self.assertTrue(receipt["complete"])
            self.assertEqual(hooked, ["marco-r"])
            self.assertEqual(c.list_evidence(3, "marco-r"), [])
            self.assertEqual(c.list_knowledge(3, "marco-r"), [])
            self.assertIsNone(c.processed_result("study:1"))
            self.assertFalse(any(Path(d).rglob(ev["blob_sha256"])))
            # other partitions untouched
            self.assertEqual(c.read_blob(3, "anna-b", other["blob_sha256"]), b"other")
            self.assertEqual(c.get_evidence(2, "hospital", hosp["id"])["id"], hosp["id"])
            # the receipt never stores the patient id
            with c.engine.connect() as conn:
                dump = " ".join(str(r) for r in conn.execute(text("SELECT * FROM cortex_erasure_receipts")))
            self.assertNotIn("marco-r", dump)

    def test_receipt_hash_is_keyed_when_a_key_is_set(self):
        plain = make().erase_patient("p1", DPO, "r")["subject_hash"]
        keyed = Cortex(receipt_key=b"secret").erase_patient("p1", DPO, "r")["subject_hash"]
        same_key = Cortex(receipt_key=b"secret").erase_patient("p1", DPO, "r")["subject_hash"]
        self.assertNotEqual(plain, keyed)
        self.assertEqual(keyed, same_key)

    def test_failing_hook_is_reported_not_hidden(self):
        c = make()

        def drop_adapter(_):
            raise RuntimeError("adapter store offline")

        c.register_erase_hook(drop_adapter)
        c.append_evidence(3, "p1", kind="x", source="t", content={})
        receipt = c.erase_patient("p1", DPO, "request")
        self.assertFalse(receipt["complete"])
        self.assertIn("adapter store offline", receipt["hook_errors"][0])
        self.assertEqual(c.list_evidence(3, "p1"), [])


CATALOG = PolicyCatalog.load(CATALOG_PATH)
REGISTRY = ManifestRegistry.from_dir(CATALOG, MANIFEST_DIR)


def pep_for(seat: str) -> PolicyEnforcementPoint:
    manifest = REGISTRY.get(seat)
    return PolicyEnforcementPoint(lambda: manifest, CATALOG, InMemoryAudit(), FakeClock(), KeyRing())


class CoreMemoryTest(unittest.TestCase):
    """CoreMemory asks kcd_core's PEP before every access, with the seat's real manifest."""

    def setUp(self):
        self.c = make()
        self.imaging = CoreMemory(self.c, pep_for("imaging_reader"), "imaging_reader", "p1")
        self.archivist = CoreMemory(self.c, pep_for("archivist"), "archivist", "p1")

    def test_layer_scopes_from_manifest(self):
        ev = self.imaging.append_evidence(kind="ct", content={})              # L1 write: allowed
        self.assertEqual(self.c.get_evidence(3, "p1", ev["id"])["source"], "seat:imaging_reader")
        with self.assertRaises(PolicyViolation):
            self.imaging.propose_patient_rule(key="k", value=1, source="s", evidence_ids=[ev["id"]])  # L2 write
        with self.assertRaises(PolicyViolation):
            self.imaging.patient_rules()                                      # L2 read
        with self.assertRaises(PolicyViolation):
            self.imaging.append_hospital_evidence(kind="x", content={})       # L3 write
        with self.assertRaises(PolicyViolation):
            self.imaging.active_rules()
        self.archivist.append_hospital_evidence(kind="x", content={})
        self.assertEqual(len(self.c.list_evidence(2, "hospital")), 1)

    def test_layers_map_to_cortex_partitions(self):
        ev = self.archivist.append_evidence(kind="decision", content={"a": 1})
        rule = self.archivist.propose_patient_rule(key="k", value=1, source="s", evidence_ids=[ev["id"]])
        self.assertEqual((rule["layer"], rule["partition"]), (3, "p1"))
        hev = self.archivist.append_hospital_evidence(kind="precedent", content={})
        hrule = self.archivist.propose_hospital_rule(key="h", value=1, source="s", evidence_ids=[hev["id"]])
        self.assertEqual((hrule["layer"], hrule["partition"]), (2, "hospital"))
        with self.assertRaises(EvidenceRequired):  # a hospital rule can't cite patient evidence
            self.archivist.propose_hospital_rule(key="h2", value=1, source="s", evidence_ids=[ev["id"]])

    def test_has_no_human_operations(self):
        for name in ("confirm", "reject", "retire", "reverify", "erase_patient"):
            self.assertFalse(hasattr(self.archivist, name), name)

    def test_proposer_is_always_the_seat(self):
        ev = self.archivist.append_evidence(kind="x", content={})
        k = self.archivist.propose_patient_rule(key="k", value=1, source="s", evidence_ids=[ev["id"]],
                                                proposed_by="dr-rossi")
        self.assertEqual(k["proposed_by"], "seat:archivist")

    def test_processed_keys_are_namespaced_per_seat(self):
        self.assertTrue(self.imaging.mark_processed("k", {"a": 1}))
        self.assertFalse(self.imaging.mark_processed("k"))
        self.assertTrue(self.archivist.mark_processed("k"))
        self.assertEqual(self.imaging.processed_result("k"), {"a": 1})


class ReleaseVerifierTest(unittest.TestCase):
    """With release_verifier, every human decision must carry the clinician's signed kcd_core release."""

    def setUp(self):
        self.clinician = Signer.generate("dr_rossi")
        self.c = Cortex(human_verifier=release_verifier(KeyRing({"dr_rossi": self.clinician.public_key})))
        ev = self.c.append_evidence(3, "p1", kind="x", source="t", content={})
        self.rule = self.c.propose(3, "p1", key="k", value=1, source="s", evidence_ids=[ev["id"]], proposed_by="seat:a")

    def release(self, artifact_id, signer=None, role="clinician:nephrology", clinician_id="dr_rossi"):
        r = Release(id="rel_9", artifact_id=artifact_id, artifact_hash="sha256:" + "0" * 64, clinician_id=clinician_id,
                    role=role, content_classes=[], signed_at=datetime(2026, 10, 26, tzinfo=UTC))
        return sign_release(r, signer or self.clinician)

    def test_signed_release_for_this_rule_confirms(self):
        actor = HumanActor("dr_rossi", "clinician:nephrology", self.release(self.rule["id"]))
        self.assertEqual(self.c.confirm(self.rule["id"], actor)["status"], "confirmed")

    def test_everything_else_is_refused(self):
        rid = self.rule["id"]
        forger = Signer.generate("dr_rossi")  # same id, different key
        cases = [
            HumanActor("dr_rossi", "clinician:nephrology"),                                        # no release
            HumanActor("dr_rossi", "clinician:nephrology", self.release("ko_other_rule")),         # other target
            HumanActor("dr_rossi", "clinician:nephrology", self.release(rid, signer=forger)),      # bad signature
            HumanActor("dr_rossi", "clinician:radiology", self.release(rid)),                      # role mismatch
            HumanActor("dr_bianchi", "clinician:nephrology", self.release(rid)),                   # not the signer
            HumanActor("seat:archivist", "clinician:nephrology", self.release(rid)),
        ]
        for actor in cases:
            with self.assertRaises(HumanRequired):
                self.c.confirm(rid, actor)
        self.assertEqual(self.c.get_knowledge(rid)["status"], "shadow")

    def test_erasure_needs_a_release_for_that_patient(self):
        with self.assertRaises(HumanRequired):
            self.c.erase_patient("p1", HumanActor("dr_rossi", "clinician:nephrology", self.release("erase:p2")), "r")
        actor = HumanActor("dr_rossi", "clinician:nephrology", self.release("erase:p1"))
        self.assertTrue(self.c.erase_patient("p1", actor, "patient request")["complete"])


if __name__ == "__main__":
    unittest.main()
