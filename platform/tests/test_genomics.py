import json
import tempfile
import unittest
from pathlib import Path

from kcd.cortex import Cortex
from kcd.genomics.panel import PanelError, normalize_classification, parse_panel_json, parse_vcf, zygosity_from_gt
from kcd.genomics.rules import RulesError, VariantRules, interpret
from kcd.genomics.source import FolderPanelSource, NoPanel
from kcd.seatkit import InMemoryGates, InMemoryOutbox, RefusalError
from kcd.seats.genomics_interpreter import GenomicsInterpreter
from tests.helpers import DEMO_PANEL, write_panel

RCC = {"history": {"rcc": True}}

VCF = """##fileformat=VCFv4.2
##fileDate=20240515
##panel_id=NGS-TEST-1
##patient_id=anna-b
#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tSAMPLE
chr3\t10183812\trs5030827\tC\tT\t.\tPASS\tGENEINFO=VHL:7428;CLNSIG=Likely_pathogenic;CLNDN=Von_Hippel-Lindau_syndrome\tGT\t0/1
chr22\t36265860\trs73885319\tA\tG\t.\tPASS\tGENEINFO=APOL1:8542;CLNSIG=risk_factor\tGT:DP\t1/1:40
chr2\t1\t.\tG\tA\t.\tPASS\tGENE=FH;CLNSIG=Pathogenic\tGT\t0/0
chr1\t2\t.\tG\tA\t.\tPASS\tGENE=XYZ1;CLNSIG=Pathogenic/Likely_pathogenic\tGT\t0|1
"""


def panel(variants, patient="p1"):
    return parse_panel_json({"patient_id": patient, "genomic_profile_id": "G1", "variants": variants})


def v(gene, sig, zyg="heterozygous", vid=""):
    return {"gene": gene, "clinical_significance": sig, "zygosity": zyg, "variant_id": vid}


class PanelParsingTest(unittest.TestCase):
    def test_demo_panel(self):
        p = parse_panel_json(json.loads(DEMO_PANEL.read_text()))
        self.assertEqual(p.patient_id, "marco-r")
        self.assertEqual([x.classification for x in p.variants], ["pathogenic", "risk_allele", "vus", "benign"])
        self.assertEqual(p.variants[2].classification_raw, "VUS")  # verbatim

    def test_normalisation_never_guesses(self):
        self.assertEqual(normalize_classification("Uncertain significance"), "vus")
        self.assertEqual(normalize_classification("Likely-Pathogenic"), "likely_pathogenic")
        self.assertEqual(normalize_classification("Pathogenic/Likely_pathogenic"), "unrecognized")
        self.assertEqual(normalize_classification("drug response"), "unrecognized")

    def test_genotypes(self):
        cases = {"0/1": "heterozygous", "1|0": "heterozygous", "1/1": "homozygous", "1/2": "compound_heterozygous",
                 "1": "hemizygous", "0/0": "absent", "./.": "unknown", "./1": "unknown"}
        for gt, expected in cases.items():
            self.assertEqual(zygosity_from_gt(gt), expected, gt)

    def test_vcf(self):
        p = parse_vcf(VCF, "anna-b")
        self.assertEqual((p.panel_id, p.date, p.source_format), ("NGS-TEST-1", "2024-05-15", "vcf"))
        self.assertEqual([x.gene for x in p.variants], ["VHL", "APOL1", "XYZ1"])  # FH 0/0 is not carried
        self.assertEqual(p.variants[0].condition, "Von Hippel-Lindau syndrome")
        self.assertEqual(p.variants[1].zygosity, "homozygous")
        self.assertEqual(p.variants[2].hgvs, "chr1:2G>A")
        with self.assertRaises(PanelError):
            parse_vcf(VCF, "marco-r")  # file says it belongs to anna-b

    def test_bad_inputs(self):
        for bad in ({}, {"patient_id": "p", "variants": [{"gene": "VHL"}]}, {"variants": []}):
            with self.assertRaises(PanelError):
                parse_panel_json(bad)
        for bad in ("chr1\t1\t.\tA\tG\t.\tPASS\tGENE=X;CLNSIG=Benign", "#CHROM\tPOS\n",
                    "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\nchr1\t1\t.\tA\tG\t.\tPASS\tCLNSIG=Benign",
                    "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\nchr1\t1", "##only meta"):
            with self.assertRaises(PanelError, msg=bad[:40]):
                parse_vcf(bad, "p")


class RulesTest(unittest.TestCase):
    def setUp(self):
        self.rules = VariantRules.load()

    def by_gene(self, result):
        return {f["gene"]: f for f in result["findings"]}

    def test_marco(self):
        r = interpret(parse_panel_json(json.loads(DEMO_PANEL.read_text())), self.rules, RCC)
        f = self.by_gene(r)
        self.assertEqual({g: x["relevance"] for g, x in f.items()}, {"VHL": "high", "APOL1": "low", "PBRM1": "none"})
        self.assertEqual([x["gene"] for x in r["findings"]], ["VHL", "APOL1", "PBRM1"])  # most relevant first
        self.assertIn("kidney-sparing", " ".join(r["case_implications"]))
        self.assertEqual(r["not_reported"][0]["gene"], "BAP1")
        self.assertEqual(r["hereditary_syndromes"], ["von Hippel-Lindau disease"])
        self.assertTrue(all(x["citations"] for x in r["findings"] if x["gene"] != "APOL1") and f["APOL1"]["citations"])

    def test_history_dependent_implication(self):
        r = interpret(panel([v("VHL", "pathogenic")]), self.rules, {"history": {}})
        self.assertNotIn("kidney-sparing", " ".join(r["case_implications"]))
        self.assertIn("Family counselling", " ".join(r["case_implications"]))

    def test_apol1_counts_alleles(self):
        one = self.by_gene(interpret(panel([v("APOL1", "risk_allele")]), self.rules))["APOL1"]
        hom = self.by_gene(interpret(panel([v("APOL1", "risk_allele", "homozygous")]), self.rules))["APOL1"]
        compound = interpret(panel([v("APOL1", "risk_allele", vid="G1"), v("APOL1", "risk_allele", vid="G2")]),
                             self.rules)
        self.assertEqual((one["relevance"], one["risk_alleles_in_gene"]), ("low", 1))
        self.assertEqual((hom["relevance"], hom["risk_alleles_in_gene"]), ("moderate", 2))
        self.assertEqual({f["relevance"] for f in compound["findings"]}, {"moderate"})

    def test_unknown_genes_and_classes_go_to_a_geneticist(self):
        r = interpret(panel([v("ABC9", "pathogenic"), v("VHL", "drug response"), v("FH", "likely pathogenic")]),
                      self.rules)
        f = self.by_gene(r)
        self.assertEqual(f["ABC9"]["relevance"], "review")
        self.assertEqual(f["VHL"]["relevance"], "unknown")
        self.assertEqual(f["FH"]["relevance"], "high")
        self.assertTrue(r["needs_geneticist_review"])

    def test_lab_class_is_never_changed(self):
        p = panel([v("VHL", "Likely_pathogenic"), v("PBRM1", "Uncertain significance"), v("X", "weird")])
        r = interpret(p, self.rules)
        self.assertEqual([f["lab_classification"] for f in r["findings"]],
                         ["Likely_pathogenic", "weird", "Uncertain significance"])

    def test_rule_file_validation(self):
        good = json.loads(Path(VariantRules.load.__globals__["RULES_PATH"]).read_text())
        broken = json.loads(json.dumps(good))
        broken["gene_rules"][0]["relevance"] = "urgent"
        with self.assertRaises(RulesError):
            VariantRules.from_dict(broken)
        broken = json.loads(json.dumps(good))
        broken["gene_rules"][0]["citations"] = ["NOPE"]
        with self.assertRaises(RulesError):
            VariantRules.from_dict(broken)
        broken = json.loads(json.dumps(good))
        del broken["class_defaults"]["vus"]
        with self.assertRaises(RulesError):
            VariantRules.from_dict(broken)


class InterpreterSeatTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        write_panel(self.root, "marco-r")
        self.cortex = Cortex()
        self.outbox = InMemoryOutbox()
        self.gates = InMemoryGates()
        self.seat = GenomicsInterpreter(self.cortex, self.outbox, self.gates, FolderPanelSource(self.root))

    def tearDown(self):
        self.tmp.cleanup()

    def test_data_request_to_genomic_context(self):
        from kcd.contracts import Envelope
        req = Envelope(type="data_request", sender="dispatcher", to="genomics_interpreter", patient="marco-r",
                       payload={"what": "genomic_context", "why": "case pack", "context": RCC})
        out = self.seat.handle(req)
        env = self.outbox.sent[0]
        self.assertEqual((env.type, env.to, env.correlation), ("data_delivered", "dispatcher", req.id))
        self.assertEqual(env.payload["kind"], "genomic_context")
        self.assertEqual(env.payload["evidence_id"], out["panel_evidence_id"])
        self.assertEqual(self.gates.opened[0]["gate"], "genetic_disclosure")
        ev = self.cortex.get_evidence(3, "marco-r", out["panel_evidence_id"])
        self.assertEqual(self.cortex.read_blob(3, "marco-r", ev["blob_sha256"]), DEMO_PANEL.read_bytes())
        other = Envelope(type="data_request", sender="dispatcher", to="x", patient="marco-r",
                         payload={"what": "recent eGFR", "why": "x"})
        self.assertIsNone(self.seat.handle(other))

    def test_idempotent_per_panel_and_context(self):
        a = self.seat.interpret("marco-r", context=RCC)
        self.assertTrue(self.seat.interpret("marco-r", context=RCC)["duplicate"])
        b = self.seat.interpret("marco-r", context={"history": {}})  # different context: new interpretation
        self.assertFalse(b["duplicate"])
        self.assertNotEqual(a["message_id"], b["message_id"])
        self.assertTrue(self.seat.interpret("marco-r", context={"history": None})["duplicate"])  # same as {}
        self.assertTrue(self.seat.interpret("marco-r")["duplicate"])

    def test_wrong_patient_panel_is_blocked(self):
        (self.root / "anna-b").mkdir()
        (self.root / "anna-b" / "panel.json").write_text(DEMO_PANEL.read_text())  # filed under the wrong patient
        with self.assertRaises(PanelError):
            self.seat.interpret("anna-b")
        self.assertEqual(self.outbox.sent, [])

    def test_missing_panel(self):
        with self.assertRaises(NoPanel):
            self.seat.interpret("nobody")
        with self.assertRaises(PanelError):
            self.seat.interpret("../x")

    def test_refusals(self):
        with self.assertRaises(RefusalError) as cm:
            self.seat.send("data_delivered", "marco-r", {"kind": "genomic_context", "data": {}}, to="patient")
        self.assertEqual(cm.exception.refusal, "send_to_patient")
        with self.assertRaises(RefusalError) as cm:
            self.seat.act("genomics.reclassify")
        self.assertEqual(cm.exception.refusal, "reclassify_variants")
        with self.assertRaises(RefusalError) as cm:
            self.seat.act("patient.disclose_genetics")
        self.assertEqual(cm.exception.refusal, "disclose_genetics_to_patient")

    def test_reclassification_guard(self):
        import kcd.seats.genomics_interpreter as mod
        real = mod.interpret

        def tampering(panel, rules, context=None):
            out = real(panel, rules, context)
            out["findings"][0]["lab_classification"] = "benign"
            return out

        mod.interpret = tampering
        try:
            with self.assertRaises(RefusalError) as cm:
                self.seat.interpret("marco-r", context=RCC)
            self.assertEqual(cm.exception.refusal, "reclassify_variants")
            self.assertEqual(self.outbox.sent, [])
        finally:
            mod.interpret = real


if __name__ == "__main__":
    unittest.main()
