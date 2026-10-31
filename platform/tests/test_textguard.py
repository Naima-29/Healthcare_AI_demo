import unittest

from kcd.seats.imaging_reader import NOTE
from kcd.textguard import is_diagnosis_claim


class DiagnosisGuardTest(unittest.TestCase):
    def test_allowed(self):
        for text in (NOTE, "No diagnosis can be made.", "Signal: tumor (p=0.94), for radiologist review", "", None):
            self.assertFalse(is_diagnosis_claim(text), text)

    def test_blocked(self):
        for text in ("Diagnosis: RCC", "diagnosed with renal cancer", "This confirms the tumour",
                     "The lesion is malignant", "consistent with malignancy", "definitive finding",
                     "It is not a diagnosis, but the lesion is malignant"):
            self.assertTrue(is_diagnosis_claim(text), text)


if __name__ == "__main__":
    unittest.main()
