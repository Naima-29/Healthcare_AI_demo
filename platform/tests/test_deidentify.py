"""De-identification and the Safety Verifier's signed egress approval, end to end through Core's egress gate.

Synthetic data only (the Marco R. fixture plus made-up contact details).
"""

import copy
import json
import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from kcd_core.audit import content_hash
from kcd_core.egress import EgressBlocked, EgressGate, InMemoryApprovalUses, verify_approval
from kcd_core.signing import SignatureError

from kcd.deidentify import Identifiers, age_band, deidentify, residual_findings
from kcd.desk import Desk
from kcd.fhir import FolderFhir
from kcd.seats.safety_verifier import SafetyVerifier

FIXTURES = Path(__file__).resolve().parents[1] / "kcd" / "fixtures"
NOW = datetime(2026, 11, 24, 10, 0, tzinfo=UTC)
PATIENT = FolderFhir(FIXTURES).patient("marco-r")  # {"id": "marco-r", "name": "Marco R.", "birthDate": ...}
IDS = Identifiers.from_patient(PATIENT, extra=["MRN-0048213"])
SEAT, TARGET = "case_briefer", "kimi"

CASE = {
    "case_id": "case-marco-r-1",
    "patient": {"name": "Marco R.", "birthDate": "1978-03-14", "telecom": "+39 333 123 4567"},
    "summary": "Marco R. (born 14/03/1978, MRN-0048213), Via Roma 12, Milano. Phone 333 123 4567, "
               "marco.r@example.com, CF RSSMRC78C14F205X. Seen by Dr. G. Ferri on 2026-10-08. "
               "eGFR 38 and falling, 3.1 cm renal mass, takes ibuprofen 600 mg.",
    "egfr_series": [{"date": "2026-10-08", "value": 52}, {"date": "2026-06-12", "value": 58}],
    "note": "Marco reports flank pain 8/10 since 12 March 2026; see https://pacs.local/study/123",
}


class DeidentifyTest(unittest.TestCase):
    def setUp(self):
        self.clean, self.removed = deidentify(CASE, IDS, today=NOW)
        self.text = json.dumps(self.clean, ensure_ascii=False)

    def test_identifiers_come_from_the_record(self):
        self.assertEqual(IDS.values, ("marco-r", "Marco R.", "MRN-0048213"))
        self.assertEqual(IDS.names, ("Marco",))  # "R." is too short to replace safely as a word

    def test_nothing_identifying_is_left(self):
        for leaked in ("Marco", "marco-r", "0048213", "1978-03-14", "14/03/1978", "Via Roma", "333 123 4567",
                       "example.com", "RSSMRC78C14F205X", "Ferri", "2026-10-08", "12 March", "pacs.local"):
            self.assertNotIn(leaked, self.text, leaked)
        self.assertEqual(residual_findings(self.clean, IDS), set())

    def test_clinical_content_is_kept(self):
        for kept in ("eGFR 38 and falling", "3.1 cm renal mass", "ibuprofen 600 mg", "flank pain 8/10"):
            self.assertIn(kept, self.text, kept)
        self.assertEqual([x["value"] for x in self.clean["egfr_series"]], [52, 58])

    def test_birth_date_becomes_an_age_band_and_dates_keep_their_year(self):
        self.assertEqual(self.clean["patient"], {"age_band": "40-49"})
        self.assertEqual([x["date"] for x in self.clean["egfr_series"]], ["2026", "2026"])
        self.assertIn("since 2026", self.clean["note"])

    def test_age_bands(self):
        self.assertEqual(age_band("1978-03-14", date(2026, 3, 13)), "40-49")  # 47, the day before the birthday
        self.assertEqual(age_band("1930-01-01", date(2026, 11, 24)), "90+")
        self.assertIsNone(age_band("not a date", date(2026, 11, 24)))

    def test_reports_what_was_removed_without_values(self):
        self.assertEqual(self.removed, {"patient_identifier", "name", "date", "phone", "address", "email",
                                        "fiscal_code", "clinician", "url"})

    def test_the_original_payload_is_not_changed(self):
        before = copy.deepcopy(CASE)
        deidentify(CASE, IDS, today=NOW)
        self.assertEqual(CASE, before)

    def test_an_email_holding_the_name_is_replaced_whole(self):
        clean, _ = deidentify({"t": "Write to marco.r@example.com"}, IDS, today=NOW)
        self.assertEqual(clean["t"], "Write to [EMAIL]")

    def test_italian_formats(self):
        clean, _ = deidentify({"t": "Nato il 14 marzo 1978, tel. 02 1234 5678, Viale Monza 120."}, IDS, today=NOW)
        self.assertEqual(clean["t"], "Nato il 1978, tel. [PHONE], [ADDRESS].")

    def test_residual_check_sees_field_names_too(self):
        # De-identification rewrites values, not field names: a payload keyed by the patient id is not clean.
        clean, _ = deidentify({"marco-r": {"egfr": 38}}, IDS, today=NOW)
        self.assertEqual(residual_findings(clean, IDS), {"patient_identifier", "name"})  # "marco-r" holds "Marco"


class EgressApprovalTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.desk = await Desk.create(start=NOW)
        self.verifier = SafetyVerifier(now=self.desk.clock.now)
        self.cloud = _Cloud()
        self.gate = EgressGate(registry=self.desk.registry, keyring=self.desk.seat_keys, client=self.cloud,
                               uses=InMemoryApprovalUses(), audit=self.desk.audit, clock=self.desk.clock)

    async def asyncTearDown(self):
        await self.desk.close()

    def approve(self, payload, **kw):
        args = {"identifiers": IDS, "target": TARGET, "requesting_seat": SEAT,
                "manifest_version": self.desk.registry.get(SEAT).ref, "signer": self.desk.signers["safety_verifier"]}
        return self.verifier.approve_egress(payload, **{**args, **kw})

    async def send(self, payload, approval):
        return await self.gate.send(seat=SEAT, target=TARGET, payload=payload, approval=approval,
                                    known_identifiers=IDS.all())

    async def test_deidentified_payload_reaches_the_cloud(self):
        clean, approval = self.approve(CASE)
        self.assertEqual((approval.decision, approval.payload_hash), ("approved", content_hash(clean)))
        self.assertEqual(approval.expires_at - approval.issued_at, timedelta(minutes=5))
        verify_approval(approval, self.desk.seat_keys)  # signed with the Safety Verifier's key
        self.assertEqual(await self.send(clean, approval), {"ok": True})
        self.assertEqual(self.cloud.sent, [(TARGET, clean)])
        self.assertNotIn("Marco", json.dumps(self.cloud.sent))
        self.assertEqual(len(self.desk.audit.find("egress.sent", "allowed")), 1)
        self.assertEqual(self.verifier.results[-1]["passed"], True)

    async def test_the_original_payload_is_blocked_by_the_gate(self):
        _, approval = self.approve(CASE)  # approval is for the de-identified copy, not for the original
        with self.assertRaises(EgressBlocked) as cm:
            await self.send(CASE, approval)
        self.assertEqual(cm.exception.reason, "payload_changed")
        self.assertEqual(self.cloud.sent, [])

    async def test_something_left_is_rejected_with_reasons_and_blocked(self):
        clean, approval = self.approve({"marco-r": {"egfr": 38}})
        self.assertEqual(approval.decision, "rejected")
        self.assertIn("patient_identifier", approval.reason)
        verify_approval(approval, self.desk.seat_keys)  # a rejection is signed too: never silence
        with self.assertRaises(EgressBlocked) as cm:
            await self.send(clean, approval)
        self.assertEqual(cm.exception.reason, "rejected_by_verifier")
        self.assertEqual(self.cloud.sent, [])
        self.assertEqual(self.verifier.results[-1]["left"], ["name", "patient_identifier"])

    async def test_an_approval_works_once_and_expires(self):
        clean, approval = self.approve(CASE)
        await self.send(clean, approval)
        with self.assertRaises(EgressBlocked) as cm:
            await self.send(clean, approval)
        self.assertEqual(cm.exception.reason, "already_used")
        clean, late = self.approve(CASE)
        self.desk.clock.advance(timedelta(minutes=6))
        with self.assertRaises(EgressBlocked) as cm:
            await self.send(clean, late)
        self.assertEqual(cm.exception.reason, "expired")

    async def test_only_the_safety_verifier_can_sign(self):
        with self.assertRaises(SignatureError):
            self.approve(CASE, signer=self.desk.signers["case_briefer"])

    async def test_results_never_hold_the_payload(self):
        self.approve(CASE)
        self.assertNotIn("Marco", json.dumps(self.verifier.results))
        self.assertNotIn("eGFR", json.dumps(self.verifier.results))


class _Cloud:
    def __init__(self):
        self.sent = []

    async def send(self, target, payload):
        self.sent.append((target, payload))
        return {"ok": True}


if __name__ == "__main__":
    unittest.main()
