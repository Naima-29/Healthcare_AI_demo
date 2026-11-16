"""Safety Verifier: checks on text that goes to the patient (patient_content_classes, guard_patient_text).

The detector must stay quiet on everything the desk says to patients today (the Companion's templates) and must
find the release-gated content classes (diagnosis, imaging result, dose instruction, prescription, genetic
result) in English and Italian. The guard hands each class found to the Companion's PEP, which lets it through
only with a valid clinician release for that artifact and that class.
"""

import unittest
from datetime import UTC, datetime

from kcd_core.pep import PolicyViolation
from kcd_core.runtime import Seat

from kcd.desk import Desk
from kcd.knowledge import KnowledgeModel
from kcd.seats.case_briefer import DRAFT
from kcd.seats.companion import TEXT
from kcd.seats.safety_verifier import guard_patient_text, patient_content_classes

KM = KnowledgeModel.load()
DRUG_NAMES = [n for c in KM.data["drug_classes"] for n in c["names"]]
NOW = datetime(2026, 10, 12, 19, 40, tzinfo=UTC)
HASH = "sha256:" + "a" * 64

UNSAFE = {
    "Your CT shows a 3 cm mass on the left kidney.": "imaging_result",
    "La TAC mostra una lesione di 2,5 cm.": "imaging_result",
    "The model gives an 87% probability.": "imaging_result",
    "You have kidney cancer.": "diagnosis_claim",
    "Diagnosis: renal cell carcinoma": "diagnosis_claim",
    "Le è stato diagnosticato un carcinoma renale.": "diagnosis_claim",
    "Take 400 mg twice a day.": "dose_instruction",
    "Prenda 2 compresse ogni 8 ore.": "dose_instruction",
    "Increase the dose of your medicine.": "dose_instruction",
    "Stop taking your sunitinib.": "dose_instruction",  # a drug the Knowledge Model does not know
    "Smetta di prendere il sunitinib.": "dose_instruction",
    "Avoid ibuprofen from now on.": "dose_instruction",
    "I will prescribe you a new painkiller.": "prescription",
    "You carry a pathogenic VHL variant.": "genetic_result",
    "Lei è portatrice di una mutazione.": "genetic_result",
}
SAFE = [
    "When did the pain start?",
    "Did you take anything for the pain?",
    "Ha preso qualcosa per il dolore?",
    "Do you have a fever?",
    "It is not a diagnosis.",
    "I cannot interpret or disclose imaging results.",
    "Non posso comunicare i risultati delle immagini.",
]


class PatientContentTest(unittest.TestCase):
    def test_companion_templates_need_no_release(self):
        # Everything the Companion says on its own must pass, or the guard would silence it.
        for lang, texts in TEXT.items():
            for key, text in texts.items():
                self.assertEqual(patient_content_classes(text, DRUG_NAMES), set(), f"{lang}.{key}: {text}")

    def test_case_briefer_drafts(self):
        # Only the medicine instruction needs the clinician's release; the other drafts are safe as written.
        for code, texts in DRAFT.items():
            for lang, text in texts.items():
                expected = {"dose_instruction"} if code == "stop_nsaid" else set()
                self.assertEqual(patient_content_classes(text, DRUG_NAMES), expected, f"{code}.{lang}: {text}")

    def test_unsafe_texts_are_found(self):
        for text, cls in UNSAFE.items():
            self.assertIn(cls, patient_content_classes(text, DRUG_NAMES), text)

    def test_safe_texts_pass(self):
        for text in SAFE:
            self.assertEqual(patient_content_classes(text, DRUG_NAMES), set(), text)

    def test_empty_text(self):
        self.assertEqual(patient_content_classes(None), set())
        self.assertEqual(patient_content_classes(""), set())


class _Companion(Seat):
    """Only the Companion's manifest and PEP are needed here, not its behaviour."""

    seat_id = "companion"

    async def handle(self, env, payload, ctx):
        return None


class GuardPatientTextTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.desk = await Desk.create(start=NOW)
        self.ctx = (await self.desk.add_seat(_Companion())).context()

    async def asyncTearDown(self):
        await self.desk.close()

    def test_safe_text_needs_no_release(self):
        self.assertEqual(guard_patient_text(self.ctx, TEXT["en"]["lab_ask"]), set())

    def test_gated_text_is_blocked_without_a_release(self):
        with self.assertRaises(PolicyViolation) as cm:
            guard_patient_text(self.ctx, "Your CT shows a 3 cm mass.")
        self.assertEqual(cm.exception.refusal, "disclose_imaging_before_review")

    def test_release_lets_through_exactly_what_it_covers(self):
        text = DRAFT["stop_nsaid"]["en"]
        release = self.desk.release("case-1", HASH, classes=["dose_instruction"])
        found = guard_patient_text(self.ctx, text, artifact_hash=HASH, release=release, drug_names=DRUG_NAMES)
        self.assertEqual(found, {"dose_instruction"})
        # The clinician released a medicine instruction, not an imaging result.
        with self.assertRaises(PolicyViolation):
            guard_patient_text(self.ctx, text + " Your CT shows a 3 cm mass.", artifact_hash=HASH, release=release)

    def test_release_for_another_artifact_does_not_count(self):
        release = self.desk.release("case-1", HASH, classes=["dose_instruction"])
        with self.assertRaises(PolicyViolation):
            guard_patient_text(self.ctx, DRAFT["stop_nsaid"]["en"], artifact_hash="sha256:" + "b" * 64,
                               release=release)


if __name__ == "__main__":
    unittest.main()
