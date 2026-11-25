import copy
import json
import unittest

from kcd.genomics.rules import RULES_PATH as VARIANT_RULES_PATH
from kcd.knowledge import KNOWLEDGE_DIR, RULES_PATH, KnowledgeError, KnowledgeModel

DEMO_RULES = KNOWLEDGE_DIR.parent / "demo" / "kidney-care-desk" / "backend" / "knowledge" / "rules.json"
KM = KnowledgeModel.load()


class KnowledgeModelTest(unittest.TestCase):
    """Same behaviour as Diana's km.mjs (diana/agent-4); the expected values match the Node version."""

    def test_marco_conflict(self):
        out = KM.check_conflicts(drug_class="NSAID", egfr=52, sbp=148, conditions=["Essential (primary) hypertension"],
                                 medications=["Sunitinib (TKI)"])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["severity"], "high")
        self.assertEqual(out[0]["conditions"], ["CKD G3a (eGFR 52)", "hypertension (home SBP ~148 mmHg)",
                                                "TKI therapy (hypertension, proteinuria)"])
        self.assertEqual(len(out[0]["citations"]), 3)

    def test_one_reason_is_moderate_and_no_reason_is_nothing(self):
        self.assertEqual(KM.check_conflicts(drug_class="NSAID", egfr=56)[0]["severity"], "moderate")
        self.assertEqual(KM.check_conflicts(drug_class="NSAID", egfr=95, sbp=120), [])
        self.assertEqual(KM.check_conflicts(drug_class="non_NSAID_analgesic", egfr=40, sbp=160), [])

    def test_ckd_stages(self):
        self.assertEqual([KM.ckd_stage(e)["stage"] for e in (95, 90, 60, 59.9, 45, 30, 15, 3)],
                         ["G1", "G1", "G2", "G3a", "G3a", "G3b", "G4", "G5"])
        self.assertIsNone(KM.ckd_stage(None))
        self.assertIn("KDIGO", KM.ckd_stage(50)["citation"])

    def test_drugs(self):
        self.assertEqual(KM.classify_drug("I take Brufen daily"), {"drug": "brufen", "class": "NSAID"})
        self.assertEqual(KM.classify_drug("ibuprofene 600")["class"], "NSAID")
        self.assertEqual(KM.classify_drug("tachipirina")["class"], "non_NSAID_analgesic")
        self.assertIsNone(KM.classify_drug("nothing here"))
        self.assertEqual(KM.classify_drug("I take NSAIDs sometimes"), {"drug": "nsaids", "class": "NSAID"})

    def test_red_flags(self):
        high = KM.red_flags(symptoms=["visible_hematuria", "flank_pain"], pain_score=8, history_rcc=True)
        self.assertEqual(high["urgency"], "high")
        self.assertEqual(KM.red_flags(symptoms=["visible_hematuria"])["urgency"], "medium")
        self.assertIsNone(KM.red_flags(symptoms=["fatigue"]))

    def test_citations_exact_match_only(self):
        kdigo = KM.citations["KDIGO2024"]
        self.assertTrue(KM.has_citation(kdigo))
        self.assertFalse(KM.has_citation(kdigo[:20]))
        self.assertFalse(KM.has_citation(kdigo + " (2025 update)"))
        self.assertIn(kdigo, [h["citation"] for h in KM.query("kdigo")])

    def test_data_file_mistakes_fail_early(self):
        good = json.loads(RULES_PATH.read_text(encoding="utf-8"))
        mistakes = [
            lambda d: d.update(ckd_stage_citation="NOPE"),
            lambda d: d["conflict_rules"][0].update(drug_class="opioid"),
            lambda d: d["conflict_rules"][0]["triggers"][0].update(citation="NOPE"),
            lambda d: d["conflict_rules"][0]["triggers"][0].update(type="weather"),
            lambda d: d["ckd_stages"].pop(),                             # no catch-all stage
            lambda d: d["ckd_stages"].reverse(),                         # wrong order
        ]
        for make_mistake in mistakes:
            broken = copy.deepcopy(good)
            make_mistake(broken)
            with self.assertRaises(KnowledgeError):
                KnowledgeModel(broken)


class KnowledgeFilesTest(unittest.TestCase):
    def test_demo_copy_of_rules_is_in_sync(self):
        self.assertEqual(json.loads(DEMO_RULES.read_text(encoding="utf-8")),
                         json.loads(RULES_PATH.read_text(encoding="utf-8")),
                         "knowledge/rules.json and the demo's copy differ: update both")

    def test_every_variant_rule_citation_is_in_the_knowledge_model(self):
        """Otherwise the Safety Verifier rejects every genomic context."""
        variant_rules = json.loads(VARIANT_RULES_PATH.read_text(encoding="utf-8"))
        for cid, text in variant_rules["citations"].items():
            self.assertTrue(KM.has_citation(text), f"{cid} is not in knowledge/rules.json")


if __name__ == "__main__":
    unittest.main()